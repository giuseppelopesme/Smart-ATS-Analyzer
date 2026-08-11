#!/usr/bin/env python3
"""MCP server exposing the ATS validator and checker over Streamable HTTP.

Stateless by construction: a document arrives as base64 in the tool call or as
a public HTTPS URL the server fetches, is written to a temp directory removed
before the call returns, and is never logged. Nothing persists between
requests.

Run locally over stdio:

    python3 ats_mcp.py

Run on a VPS behind a reverse proxy:

    MCP_TRANSPORT=http MCP_PATH=/mcp/<secret> python3 ats_mcp.py

An upload page is mounted at ``<MCP_PATH>/upload`` so a phone or iPad can hand
the server a file and get a single-use id back.

See README.md for provisioning, TLS and the claude.ai authentication caveat.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from typing import Annotated, Any, Dict, Optional

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core  # noqa: E402
import oauth  # noqa: E402
import uploads  # noqa: E402

SERVER_NAME = "ats_mcp"
MAX_MB = core.MAX_FILE_BYTES // (1024 * 1024)
REQUIRED_SCOPE = "ats:validate"


def _build_auth() -> tuple:
    """Configure OAuth from the environment, or return (None, None).

    Returns (provider, AuthSettings). Both are None when OAuth is not
    configured, in which case the secret-path deployment applies.
    """
    public_url = os.environ.get("MCP_PUBLIC_URL", "").rstrip("/")
    password_hash = os.environ.get("ATS_OAUTH_PASSWORD_HASH", "").strip()
    if not public_url or not password_hash:
        return None, None

    provider = oauth.AtsAuthProvider(
        base_url=public_url,
        password_hash=password_hash,
        state_path=os.environ.get("ATS_OAUTH_STATE", "/var/lib/ats-mcp/oauth.json"),
    )
    settings = AuthSettings(
        issuer_url=AnyHttpUrl(public_url),
        resource_server_url=AnyHttpUrl(public_url),
        required_scopes=[REQUIRED_SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,                       # claude.ai registers itself
            valid_scopes=[REQUIRED_SCOPE],
            default_scopes=[REQUIRED_SCOPE],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    return provider, settings


AUTH_PROVIDER, AUTH_SETTINGS = _build_auth()

mcp = MCPServer(
    name=SERVER_NAME,
    title="ATS CV validator",
    version="1.0.0",
    auth_server_provider=AUTH_PROVIDER,
    auth=AUTH_SETTINGS,
    instructions=(
        "Validates CV deliverables against a canonical ATS structure and reviews "
        "resumes for machine parseability and job-description fit.\n\n"
        "A document reaches this server three ways: an upload_id from the "
        "upload page (the route that works from an iPhone or iPad), a public "
        "HTTPS URL, or base64. Give exactly one. Prefer upload_id or a URL — a "
        "model cannot reliably emit a megabyte of base64 for an attached file. "
        "Upload ids are single-use and expire.\n\n"
        "Use ats_validate_deliverables before archiving or submitting a tailored CV "
        "— it is a gate, and its target is 100/100 with zero failures. Use "
        "ats_check_resume for any other resume, including ones you did not build. "
        "Use ats_extract_text to see the text an ATS actually recovers; read it, "
        "because if it looks wrong to you it is wrong."
    ),
)


# --------------------------------------------------------------------------
# Input models
# --------------------------------------------------------------------------

Base64Doc = Annotated[str, Field(
    description=f"File content, base64-encoded. Maximum {MAX_MB} MB decoded.",
    min_length=4,
)]


class ValidateInput(BaseModel):
    """Input for the pre-archive gate."""

    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")

    docx_base64: Optional[Base64Doc] = Field(
        default=None, description=f"The ATS .docx, base64-encoded. Max {MAX_MB} MB. "
                                  f"Give this OR docx_url, not both.")
    docx_url: Optional[str] = Field(
        default=None, max_length=2000,
        description="Public HTTPS URL to fetch the ATS .docx from — an iCloud or "
                    "Dropbox share link, for instance. Use this rather than base64 "
                    "when you have a link: a model cannot reliably emit a megabyte "
                    "of base64. The server fetches it, checks it and keeps nothing.")
    docx_upload_id: Optional[str] = Field(
        default=None, max_length=64,
        description="One-time id from the upload page. The way to validate from "
                    "an iPad or iPhone: upload the file there, paste the id here. "
                    "Consumed on use.")
    docx_filename: str = Field(
        default="cv.docx",
        description="Original filename, used for display only. Only the extension is "
                    "honoured; the rest never touches the filesystem.",
        max_length=300,
    )
    pdf_base64: Optional[Base64Doc] = Field(
        default=None,
        description="The ATS PDF generated from that exact docx. Omit it and the "
                    "round-trip, single-column and PDF parseability checks report "
                    "'skip' rather than 'pass'.",
    )
    pdf_url: Optional[str] = Field(
        default=None, max_length=2000,
        description="Public HTTPS URL for the ATS PDF. Alternative to pdf_base64.")
    pdf_upload_id: Optional[str] = Field(
        default=None, max_length=64,
        description="One-time upload id for the ATS PDF.")
    pdf_filename: str = Field(default="cv.pdf", max_length=300)
    job_description: Optional[str] = Field(
        default=None,
        description="Job ad text. Enables the required-terms and hard-gate checks.",
        max_length=core.MAX_JD_CHARS,
    )
    response_format: str = Field(
        default="markdown",
        description="'markdown' for a readable verdict, 'json' for the full report "
                    "including every check's evidence.",
        pattern="^(markdown|json)$",
    )


class CheckInput(BaseModel):
    """Input for a general resume review."""

    model_config = ConfigDict(extra="forbid")

    content_base64: Optional[Base64Doc] = Field(
        default=None, description=f"Resume content, base64-encoded. Max {MAX_MB} MB. "
                                  f"Give this OR content_url, not both.")
    content_url: Optional[str] = Field(
        default=None, max_length=2000,
        description="Public HTTPS URL to fetch the resume from. Preferred over "
                    "base64 whenever a link exists.")
    upload_id: Optional[str] = Field(
        default=None, max_length=64,
        description="One-time id from the upload page. Consumed on use.")
    filename: str = Field(
        default="resume.pdf",
        description="Original filename. Determines the reader: .pdf, .docx, .rtf, "
                    ".txt, .md. Only the extension is used.",
        max_length=300,
    )
    job_description: Optional[str] = Field(
        default=None, max_length=core.MAX_JD_CHARS,
        description="Job ad text. Omit for a parseability-only review.",
    )
    response_format: str = Field(default="markdown", pattern="^(markdown|json)$")


class ExtractInput(BaseModel):
    """Input for raw text extraction."""

    model_config = ConfigDict(extra="forbid")

    content_base64: Optional[Base64Doc] = Field(default=None)
    content_url: Optional[str] = Field(
        default=None, max_length=2000,
        description="Public HTTPS URL to fetch the document from.")
    upload_id: Optional[str] = Field(
        default=None, max_length=64,
        description="One-time id from the upload page. Consumed on use.")
    filename: str = Field(default="resume.pdf", max_length=300)
    stream_order: bool = Field(
        default=False,
        description="False gives the layout-aware read (column-aware). True gives raw "
                    "content-stream order, which is what a naive parser sees. On a "
                    "multi-column document the two differ, and that difference is the "
                    "damage.",
    )


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _error(exc: Exception) -> str:
    """Actionable, non-leaking error text."""
    if isinstance(exc, core.InputTooLarge):
        return (f"Error: {exc}. Send the CV itself rather than a scan or an "
                f"image-heavy export.")
    if isinstance(exc, core.BadUpload):
        return f"Error: {exc}"
    if isinstance(exc, FileNotFoundError):
        return "Error: the uploaded content could not be read."
    # Never surface a raw traceback: it can echo document content.
    return (f"Error: the document could not be processed ({type(exc).__name__}). "
            f"If it opens correctly in Word or a PDF viewer, this is a bug in the "
            f"analyser rather than a problem with the file.")


def _dump(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@mcp.tool(
    name="ats_validate_deliverables",
    title="Validate ATS CV deliverables",
    annotations={
        "title": "Validate ATS CV deliverables",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def ats_validate_deliverables(params: ValidateInput) -> str:
    """Run the pre-archive gate over an ATS CV .docx and the PDF built from it.

    Checks the canonical structure (A4, margins, single column, Calibri, point
    sizes, real Word numbering definitions rather than typed bullet glyphs,
    section order and per-section content rules, document metadata), then the
    docx-to-PDF round trip block by block, that the PDF is genuinely single
    column, and job-description coverage when a JD is supplied.

    This is a gate, not advice. The target is 100/100 with zero failures. A
    check that could not be performed reports "skip", never "pass" — so
    validating without the PDF is not a green light for the PDF.

    Args:
        params (ValidateInput): validated input containing:
            - docx_base64 (str): the ATS .docx, base64-encoded
            - docx_filename (str): display name; only the extension is used
            - pdf_base64 (Optional[str]): the ATS PDF generated from that docx
            - pdf_filename (str): display name for the PDF
            - job_description (Optional[str]): job ad text
            - response_format (str): "markdown" or "json"

    Returns:
        str: Markdown verdict, or JSON with this shape:
        {
          "docx": {"filename": str, "words": int, "fonts": [str],
                   "tables": int, "textboxes": int, "images": int,
                   "metadata": {...}},
          "pdf": {"pages": int, "words": int, "comparison": {...},
                  "missing_blocks": [{"text": str, "best_ratio": float}],
                  "parseability": {"score": int, "findings": [...]}},
          "jd_match": {"coverage_required": float, "missing": [...],
                       "gates": [...]},
          "validation": {
             "score": int, "passed": bool,
             "counts": {"pass": int, "fail": int, "skip": int},
             "checks": [{"id": str, "status": "pass"|"fail"|"skip",
                         "title": str, "detail": str, "fix": str,
                         "severity": str, "evidence": {...}}]
          }
        }

        On failure: "Error: <what went wrong and what to do about it>".

    Examples:
        - Use when: about to archive or submit a tailored CV.
        - Use when: confirming a regenerated PDF still matches its docx.
        - Don't use when: the file is a resume you did not build to this
          structure — use ats_check_resume, which does not expect the canonical
          sections and will not drown you in irrelevant failures.
        - Don't pass the Canva design export here. It is multi-column by
          construction and has its own design QA gate.
    """
    try:
        report = core.validate_deliverables(
            docx_b64=params.docx_base64,
            docx_filename=params.docx_filename,
            pdf_b64=params.pdf_base64,
            pdf_filename=params.pdf_filename,
            jd_text=params.job_description,
            docx_url=params.docx_url,
            pdf_url=params.pdf_url,
            docx_upload_id=params.docx_upload_id,
            pdf_upload_id=params.pdf_upload_id,
        )
    except Exception as exc:  # noqa: BLE001 - converted to actionable text
        return _error(exc)

    if params.response_format == "json":
        return _dump(report)
    return ats_validate_render(report)


def ats_validate_render(report: Dict[str, Any]) -> str:
    import ats_validate  # local import: keeps stdio start-up cheap

    return ats_validate.render(report)


@mcp.tool(
    name="ats_check_resume",
    title="Check a resume against ATS screening",
    annotations={
        "title": "Check a resume against ATS screening",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def ats_check_resume(params: CheckInput) -> str:
    """Review any resume for machine parseability and job-description fit.

    Answers two questions in order. First, can an ATS read the file at all:
    text layer, fonts with no character map, multi-column layouts, contact
    details stranded in a Word header or a mailto: annotation, dates, hidden
    text. Second, how the content lines up with a job ad: requirement terms
    weighted by JD section, alias-aware so "K8s" satisfies "Kubernetes", hard
    gates, and skills listed with no supporting bullet.

    The parseability score is defensible. The JD coverage percentage is
    lexical overlap only and is not a score of the candidate — read the term
    lists and the evidence, and form your own verdict.

    Args:
        params (CheckInput): validated input containing:
            - content_base64 (str): the resume, base64-encoded
            - filename (str): determines the reader via its extension
            - job_description (Optional[str]): job ad text; omit for
              parseability only
            - response_format (str): "markdown" or "json"

    Returns:
        str: Markdown report, or JSON with this shape:
        {
          "resume": {"filename": str, "format": str, "pages": int,
                     "words": int, "fonts": [...]},
          "parseability": {"score": int, "blockers": int, "high": int,
                           "findings": [{"id": str, "severity": str,
                                         "title": str, "detail": str,
                                         "fix": str}]},
          "extracted_text": str,
          "extracted_text_stream_order": str,
          "pages": [{"number": int, "multi_column": bool, ...}],
          "jd_match": {"coverage_required": float, "matched": [...],
                       "missing": [...], "gates": [...]}
        }

        On failure: "Error: <what went wrong and what to do about it>".

    Examples:
        - Use when: asked whether a resume will pass ATS screening.
        - Use when: asked why an application is not getting interviews.
        - Don't use when: validating a CV built to the canonical structure —
          use ats_validate_deliverables, which enforces it.
    """
    try:
        report = core.check_resume(
            content_b64=params.content_base64,
            filename=params.filename,
            jd_text=params.job_description,
            content_url=params.content_url,
            upload_id=params.upload_id,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)

    if params.response_format == "json":
        return _dump(report)
    import ats_check

    return ats_check.render_markdown(report)


@mcp.tool(
    name="ats_extract_text",
    title="Extract the text an ATS actually sees",
    annotations={
        "title": "Extract the text an ATS actually sees",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def ats_extract_text(params: ExtractInput) -> str:
    """Return the text an ATS recovers from a document, in either reading order.

    The layout-aware read groups lines and respects column boundaries. The
    content-stream read replays the drawing operators in file order, which is
    what a naive parser gets. On a single-column document the two agree. On a
    multi-column one they diverge sharply, and the divergence is precisely the
    damage the layout does.

    Args:
        params (ExtractInput): validated input containing:
            - content_base64 (str): the document, base64-encoded
            - filename (str): determines the reader via its extension
            - stream_order (bool): False for layout-aware, True for raw
              content-stream order

    Returns:
        str: JSON with this shape:
        {
          "filename": str,
          "format": str,          # pdf | docx | text | markdown | rtf
          "pages": int,
          "words": int,
          "multi_column_pages": [int],
          "text": str,
          "order": "layout-aware" | "content-stream"
        }

        On failure: "Error: <what went wrong and what to do about it>".

    Examples:
        - Use when: showing someone why their two-column template breaks —
          call it twice, once with stream_order true, and quote the difference.
        - Use when: a parseability score looks fine but you want to read the
          extraction yourself before trusting it.
    """
    try:
        return _dump(core.extract_text(
            content_b64=params.content_base64,
            filename=params.filename,
            stream_order=params.stream_order,
            content_url=params.content_url,
            upload_id=params.upload_id,
        ))
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


# --------------------------------------------------------------------------
# Upload page — the route that works from a phone or an iPad
# --------------------------------------------------------------------------


def register_upload_routes(base_path: str, store: "uploads.UploadStore") -> None:
    """Mount the upload form under the same secret prefix as the MCP endpoint.

    Sitting beneath ``MCP_PATH`` means the reverse-proxy rule and the secret
    that protect the MCP endpoint protect this too, with no extra config and
    no second secret to keep track of.
    """
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse

    ttl_minutes = max(1, store.ttl // 60)
    route = base_path.rstrip("/") + "/upload"

    @mcp.custom_route(route, methods=["GET"], include_in_schema=False)
    async def upload_form(request: Request) -> HTMLResponse:  # noqa: ARG001
        return HTMLResponse(uploads.form_page(ttl_minutes))

    @mcp.custom_route(route, methods=["POST"], include_in_schema=False)
    async def upload_receive(request: Request):
        wants_json = "application/json" in (request.headers.get("accept") or "")
        try:
            form = await request.form()
        except Exception:  # noqa: BLE001
            return _upload_error("could not read the upload", wants_json, ttl_minutes)

        item = form.get("file")
        filename = getattr(item, "filename", None)
        if item is None or not filename:
            return _upload_error("no file was selected", wants_json, ttl_minutes)

        try:
            content = await item.read()
        except Exception:  # noqa: BLE001
            return _upload_error("could not read the file", wants_json, ttl_minutes)
        finally:
            close = getattr(item, "close", None)
            if close is not None:
                await close()

        try:
            code = store.put(content, os.path.basename(str(filename)))
        except uploads.UploadError as exc:
            return _upload_error(str(exc), wants_json, ttl_minutes)

        if wants_json:
            return JSONResponse({
                "upload_id": code,
                "filename": os.path.basename(str(filename)),
                "expires_in_seconds": store.ttl,
                "single_use": True,
            })
        return HTMLResponse(uploads.result_page(code, str(filename), ttl_minutes))

    def _upload_error(message: str, wants_json: bool, ttl: int):
        if wants_json:
            return JSONResponse({"error": message}, status_code=400)
        return HTMLResponse(uploads.form_page(ttl, error=message), status_code=400)


def register_login_routes(provider: "oauth.AtsAuthProvider") -> None:
    """Mount the login page the authorization endpoint redirects to.

    These live at a fixed, public path rather than under the secret one: the
    browser arrives here from Claude's OAuth redirect, and with OAuth in place
    the passphrase is the access control, not the URL.
    """
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, RedirectResponse

    @mcp.custom_route("/oauth/login", methods=["GET"], include_in_schema=False)
    async def login_form(request: Request) -> HTMLResponse:
        txn = request.query_params.get("txn", "")
        name = provider.pending_client_name(txn)
        if name is None:
            return HTMLResponse(
                oauth.login_page(txn, "An application",
                                 "This login link has expired. Start again from Claude."),
                status_code=400)
        return HTMLResponse(oauth.login_page(txn, name))

    @mcp.custom_route("/oauth/login", methods=["POST"], include_in_schema=False)
    async def login_submit(request: Request):
        form = await request.form()
        txn = str(form.get("txn", ""))
        password = str(form.get("password", ""))
        try:
            redirect_to = provider.complete_login(txn, password)
        except ValueError as exc:
            name = provider.pending_client_name(txn) or "An application"
            return HTMLResponse(oauth.login_page(txn, name, str(exc)), status_code=400)
        # 303 so the browser reissues as GET and the passphrase leaves the
        # form submission behind.
        return RedirectResponse(redirect_to, status_code=303)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> int:
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport in ("stdio", ""):
        mcp.run()
        return 0

    if transport not in ("http", "streamable-http"):
        print(f"error: unknown MCP_TRANSPORT '{transport}' "
              f"(use 'stdio' or 'http')", file=sys.stderr)
        return 2

    path = os.environ.get("MCP_PATH", "/mcp")
    if AUTH_PROVIDER is None and (not path or path == "/mcp"):
        # Without OAuth the only thing protecting the endpoint is the URL, so
        # refuse a guessable one rather than quietly serving an open endpoint.
        suggestion = f"/mcp/{secrets.token_urlsafe(24)}"
        print(
            "error: this server is unauthenticated, so MCP_PATH must be an "
            "unguessable path.\n"
            f"       Either set MCP_PATH={suggestion}\n"
            "       or configure OAuth by setting MCP_PUBLIC_URL and "
            "ATS_OAUTH_PASSWORD_HASH\n"
            "       (generate the hash with: python3 server/oauth.py "
            "--hash-password).",
            file=sys.stderr,
        )
        return 2

    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8080"))

    # Staging lets an iPhone or iPad reach the validator: upload there, paste
    # the id into Claude. Memory-only, single-use and expiring — see uploads.py.
    ttl = int(os.environ.get("MCP_UPLOAD_TTL", "1800"))
    if ttl > 0:
        store = uploads.UploadStore(ttl_seconds=ttl, max_file_bytes=core.MAX_FILE_BYTES)
        core.set_upload_store(store)
        register_upload_routes(path, store)
        print(f"  upload page: http://{host}:{port}{path.rstrip('/')}/upload "
              f"(ids single-use, {ttl // 60} min TTL)", file=sys.stderr)
    else:
        print("  upload staging disabled (MCP_UPLOAD_TTL=0)", file=sys.stderr)

    # The transport caps request bodies at 4 MiB by default, which is smaller
    # than the documents we accept: a 12 MB file is ~16 MB of base64. Left
    # alone, a large upload would be rejected by the transport with an error
    # that says nothing about size limits.
    max_body = int(core.MAX_FILE_BYTES * 4 / 3) + (2 * 1024 * 1024)

    if AUTH_PROVIDER is not None:
        register_login_routes(AUTH_PROVIDER)
        print(f"  OAuth 2.1 enabled — issuer {AUTH_PROVIDER.base_url}, "
              f"scope {REQUIRED_SCOPE}, dynamic client registration on",
              file=sys.stderr)
        print(f"  {len(AUTH_PROVIDER.clients)} client(s) already registered",
              file=sys.stderr)
    else:
        print("  OAuth NOT configured — access control is the secret path alone. "
              "Set MCP_PUBLIC_URL and ATS_OAUTH_PASSWORD_HASH to enable it.",
              file=sys.stderr)

    print(f"{SERVER_NAME} listening on http://{host}:{port}{path}", file=sys.stderr)
    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path=path,
        # Nothing is kept between calls, so there is no session to maintain.
        stateless_http=True,
        json_response=True,
        max_request_body_size=max_body,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
