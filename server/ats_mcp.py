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

See README.md for provisioning, TLS and the claude.ai authentication caveat.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from typing import Annotated, Any, Dict, Optional

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core  # noqa: E402

SERVER_NAME = "ats_mcp"
MAX_MB = core.MAX_FILE_BYTES // (1024 * 1024)

mcp = MCPServer(
    name=SERVER_NAME,
    title="ATS CV validator",
    version="1.0.0",
    instructions=(
        "Validates CV deliverables against a canonical ATS structure and reviews "
        "resumes for machine parseability and job-description fit.\n\n"
        "Pass a document either as base64 or as a public HTTPS URL "
        "(the *_url fields). Prefer the URL when one exists: a model cannot "
        "reliably emit a megabyte of base64 for an attached file. The server "
        "stores nothing and keeps no copy.\n\n"
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
        ))
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


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

    path = os.environ.get("MCP_PATH", "")
    if not path or path == "/mcp":
        # claude.ai connectors cannot send a static bearer token, so an
        # unguessable path is the practical access control. Refuse to start on
        # a default path rather than quietly exposing an open endpoint.
        suggestion = f"/mcp/{secrets.token_urlsafe(24)}"
        print(
            "error: MCP_PATH must be set to an unguessable path before serving "
            "over HTTP.\n"
            f"       Suggested: MCP_PATH={suggestion}\n"
            "       See README.md — claude.ai custom connectors support OAuth "
            "only, with no field for an API key, so the secret path is what "
            "keeps this endpoint private.",
            file=sys.stderr,
        )
        return 2

    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8080"))

    # The transport caps request bodies at 4 MiB by default, which is smaller
    # than the documents we accept: a 12 MB file is ~16 MB of base64. Left
    # alone, a large upload would be rejected by the transport with an error
    # that says nothing about size limits.
    max_body = int(core.MAX_FILE_BYTES * 4 / 3) + (2 * 1024 * 1024)

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
