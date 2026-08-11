"""Run the ATS analyser over uploaded bytes, statelessly.

The VPS deployment stores nothing: a document arrives either as base64 in the
tool call or as a public HTTPS URL the server fetches, is written to a
temporary directory deleted before the call returns, and is never logged.

Both input routes exist because neither covers every caller. Base64 suits a
client that already holds the bytes; a URL suits a phone, where a model cannot
realistically emit a megabyte of base64 for an attached PDF.

This module is deliberately transport-agnostic and standard-library only, so
the whole analysis path can be tested without installing the MCP SDK. The MCP
layer in ``ats_mcp.py`` is a thin wrapper over these three functions.
"""

from __future__ import annotations

import base64
import binascii
import os
import sys
import tempfile
from typing import Any, Dict, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), ".claude", "skills", "ats-check", "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import ats_check  # noqa: E402
import ats_validate  # noqa: E402
from docx_inspect import DocxError  # noqa: E402
from readers import UnsupportedFormat, load_any  # noqa: E402

from fetching import FetchError, fetch_document, guess_filename  # noqa: E402

__all__ = ["validate_deliverables", "check_resume", "extract_text",
           "InputTooLarge", "BadUpload", "MAX_FILE_BYTES", "MAX_JD_CHARS"]

# A two-page CV is tens of KB; 12 MB is generous and still bounds memory.
MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_JD_CHARS = 60_000

ALLOWED_SUFFIXES = {".pdf", ".docx", ".doc", ".rtf", ".txt", ".md", ".markdown", ".html", ".htm"}


class InputTooLarge(ValueError):
    pass


class BadUpload(ValueError):
    pass


def _suffix_of(filename: str, default: str) -> str:
    """Take only the extension from a caller-supplied name.

    The rest of the name is discarded: it is untrusted input that would
    otherwise reach the filesystem, and nothing downstream needs it.
    """
    suffix = os.path.splitext(os.path.basename(filename or ""))[1].lower()
    if suffix not in ALLOWED_SUFFIXES:
        return default
    return suffix


def _decode(content_b64: str, label: str) -> bytes:
    if not content_b64 or not content_b64.strip():
        raise BadUpload(f"{label} is empty")
    # Reject before allocating: base64 inflates by 4/3.
    if len(content_b64) > MAX_FILE_BYTES * 4 // 3 + 1024:
        raise InputTooLarge(
            f"{label} exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB limit"
        )
    try:
        raw = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BadUpload(f"{label} is not valid base64: {exc}") from exc
    if not raw:
        raise BadUpload(f"{label} decoded to zero bytes")
    if len(raw) > MAX_FILE_BYTES:
        raise InputTooLarge(
            f"{label} is {len(raw) // 1024} KB, over the "
            f"{MAX_FILE_BYTES // (1024 * 1024)} MB limit"
        )
    return raw


def _obtain(content_b64: Optional[str], url: Optional[str], label: str,
            default_name: str) -> tuple:
    """Get bytes from base64 or a URL. Returns (content, filename_hint).

    Exactly one source must be given: silently preferring one over the other
    would make a caller that supplied both think it validated the wrong file.
    """
    has_b64 = bool(content_b64 and content_b64.strip())
    has_url = bool(url and url.strip())
    if has_b64 and has_url:
        raise BadUpload(
            f"give either {label}_base64 or {label}_url, not both — "
            f"otherwise which one was checked is ambiguous"
        )
    if not has_b64 and not has_url:
        raise BadUpload(f"no {label} supplied: pass {label}_base64 or {label}_url")
    if has_b64:
        return _decode(content_b64, label), default_name
    try:
        content, final_url = fetch_document(url.strip(), MAX_FILE_BYTES)
    except FetchError as exc:
        raise BadUpload(f"could not fetch the {label}: {exc}") from exc
    return content, guess_filename(final_url, default_name)


def _clip_jd(jd_text: Optional[str]) -> str:
    if not jd_text:
        return ""
    return jd_text[:MAX_JD_CHARS]


def validate_deliverables(
    docx_b64: Optional[str] = None,
    docx_filename: str = "cv.docx",
    pdf_b64: Optional[str] = None,
    pdf_filename: str = "cv.pdf",
    jd_text: Optional[str] = None,
    docx_url: Optional[str] = None,
    pdf_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the canonical-structure gate over an ATS docx (+ PDF)."""
    docx_bytes, docx_hint = _obtain(docx_b64, docx_url, "docx", docx_filename)
    docx_filename = docx_hint or docx_filename
    pdf_bytes = None
    if pdf_b64 or pdf_url:
        pdf_bytes, pdf_hint = _obtain(pdf_b64, pdf_url, "pdf", pdf_filename)
        pdf_filename = pdf_hint or pdf_filename

    with tempfile.TemporaryDirectory(prefix="ats-") as tmp:
        docx_path = os.path.join(tmp, f"upload{_suffix_of(docx_filename, '.docx')}")
        with open(docx_path, "wb") as fh:
            fh.write(docx_bytes)

        pdf_path = None
        if pdf_bytes is not None:
            pdf_path = os.path.join(tmp, f"upload{_suffix_of(pdf_filename, '.pdf')}")
            with open(pdf_path, "wb") as fh:
                fh.write(pdf_bytes)

        try:
            report = ats_validate.run(docx_path, pdf_path, _clip_jd(jd_text))
        except DocxError as exc:
            raise BadUpload(
                f"{exc}. The gate expects the ATS .docx — not the PDF and not "
                f"the Canva design export."
            ) from exc

    # Replace temp paths with the caller's own names: the server's filesystem
    # layout is not the caller's business, and the temp dir no longer exists.
    report["docx"]["path"] = None
    report["docx"]["filename"] = os.path.basename(docx_filename or "cv.docx")
    if "pdf" in report:
        report["pdf"]["path"] = None
        report["pdf"]["filename"] = os.path.basename(pdf_filename or "cv.pdf")
    return report


def check_resume(
    content_b64: Optional[str] = None,
    filename: str = "resume.pdf",
    jd_text: Optional[str] = None,
    content_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Parseability and job-description review for any resume."""
    raw, hint = _obtain(content_b64, content_url, "resume", filename)
    filename = hint or filename
    with tempfile.TemporaryDirectory(prefix="ats-") as tmp:
        path = os.path.join(tmp, f"upload{_suffix_of(filename, '.pdf')}")
        with open(path, "wb") as fh:
            fh.write(raw)
        try:
            report = ats_check.build_report(path, _clip_jd(jd_text))
        except UnsupportedFormat as exc:
            raise BadUpload(str(exc)) from exc

    report["resume"]["path"] = None
    report["resume"]["filename"] = os.path.basename(filename or "resume.pdf")
    return report


def extract_text(
    content_b64: Optional[str] = None,
    filename: str = "resume.pdf",
    stream_order: bool = False,
    content_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return exactly what an ATS extracts, in either reading order."""
    raw, hint = _obtain(content_b64, content_url, "document", filename)
    filename = hint or filename
    with tempfile.TemporaryDirectory(prefix="ats-") as tmp:
        path = os.path.join(tmp, f"upload{_suffix_of(filename, '.pdf')}")
        with open(path, "wb") as fh:
            fh.write(raw)
        try:
            ex = load_any(path)
        except UnsupportedFormat as exc:
            raise BadUpload(str(exc)) from exc

        return {
            "filename": os.path.basename(filename or "resume.pdf"),
            "format": ex.kind,
            "pages": len(ex.pages),
            "words": ex.word_count,
            "multi_column_pages": [p.number for p in ex.pages if p.multi_column],
            "text": ex.naive_text if stream_order else ex.text,
            "order": "content-stream" if stream_order else "layout-aware",
        }
