#!/usr/bin/env python3
"""Tests for the MCP server's transport-agnostic core.

    python3 tests/test_server.py

Deliberately does not import the MCP SDK: the analysis path, the upload
guards and the SSRF guards are all reachable without it, so they stay
testable on a machine that has not installed the transport.
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, HERE)

import core  # noqa: E402
import docx_fixtures as DF  # noqa: E402
import fetching  # noqa: E402
import fixtures as PF  # noqa: E402

PASS = FAIL = 0
FAILURES: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  pass  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


def section(title: str) -> None:
    print(f"\n{title}")


def b64_of(path: str) -> str:
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


def test_validate(tmp: str) -> None:
    section("Server: validate over uploaded bytes")
    docx = DF.build_docx(os.path.join(tmp, "cv.docx"))
    pdf = os.path.join(tmp, "cv.pdf")
    with open(pdf, "wb") as fh:
        fh.write(PF.multi_page_text_pdf(DF.plain_text()))

    report = core.validate_deliverables(
        docx_b64=b64_of(docx), docx_filename="20260811_CV.docx",
        pdf_b64=b64_of(pdf), pdf_filename="20260811_CV.pdf")
    v = report["validation"]
    check("clean deliverables pass", v["passed"] is True, str(v["counts"]))
    check("score is 100", v["score"] == 100, str(v["score"]))
    check("caller filename is echoed back",
          report["docx"]["filename"] == "20260811_CV.docx", report["docx"]["filename"])
    check("server paths are not leaked",
          report["docx"]["path"] is None and report["pdf"]["path"] is None,
          str(report["docx"]["path"]))

    # Without the PDF the PDF-dependent checks must skip, not pass.
    only_docx = core.validate_deliverables(docx_b64=b64_of(docx))
    statuses = {c["id"]: c["status"] for c in only_docx["validation"]["checks"]}
    check("round trip skips without a PDF",
          statuses.get("parity.docx_pdf") == "skip", str(statuses.get("parity.docx_pdf")))
    check("single-column check skips without a PDF",
          statuses.get("pdf.single_column") == "skip",
          str(statuses.get("pdf.single_column")))

    # The design export must be caught if sent as the ATS PDF.
    design = os.path.join(tmp, "design.pdf")
    with open(design, "wb") as fh:
        fh.write(PF.two_column_pdf())
    wrong = core.validate_deliverables(docx_b64=b64_of(docx), pdf_b64=b64_of(design))
    st = {c["id"]: c["status"] for c in wrong["validation"]["checks"]}
    check("two-column PDF fails the single-column check",
          st.get("pdf.single_column") == "fail", str(st.get("pdf.single_column")))

    # A PDF sent where the docx belongs must be a clear error.
    try:
        core.validate_deliverables(docx_b64=b64_of(pdf))
        check("PDF sent as the docx is rejected", False, "no exception")
    except core.BadUpload as exc:
        check("PDF sent as the docx is rejected", "not a zip" in str(exc).lower(), str(exc)[:80])


def test_check_and_extract(tmp: str) -> None:
    section("Server: check and extract")
    twocol = os.path.join(tmp, "twocol.pdf")
    with open(twocol, "wb") as fh:
        fh.write(PF.two_column_pdf())

    report = core.check_resume(content_b64=b64_of(twocol), filename="cv.pdf")
    ids = {f["id"] for f in report["parseability"]["findings"]}
    check("multi-column resume is flagged", "multi_column" in ids, str(sorted(ids)))
    check("resume path is not leaked", report["resume"]["path"] is None)

    layout = core.extract_text(content_b64=b64_of(twocol))
    stream = core.extract_text(content_b64=b64_of(twocol), stream_order=True)
    check("extract reports the reading order",
          layout["order"] == "layout-aware" and stream["order"] == "content-stream")
    check("the two orders differ on a two-column page",
          layout["text"] != stream["text"])
    check("multi-column pages are reported",
          layout["multi_column_pages"] == [1], str(layout["multi_column_pages"]))


def test_upload_guards(tmp: str) -> None:
    section("Server: upload guards")
    try:
        core.check_resume(content_b64="!!! not base64 !!!")
        check("invalid base64 rejected", False, "no exception")
    except core.BadUpload as exc:
        check("invalid base64 rejected", "base64" in str(exc), str(exc)[:60])

    oversize = base64.b64encode(b"x" * (core.MAX_FILE_BYTES + 1024)).decode()
    try:
        core.check_resume(content_b64=oversize)
        check("oversize upload rejected", False, "no exception")
    except core.InputTooLarge as exc:
        check("oversize upload rejected", True, str(exc)[:50])

    try:
        core.check_resume(content_b64="")
        check("empty upload rejected", False, "no exception")
    except core.BadUpload:
        check("empty upload rejected", True)

    try:
        core.check_resume(content_b64="aGk=", content_url="https://example.com/a.pdf")
        check("both sources rejected", False, "no exception")
    except core.BadUpload as exc:
        check("both sources rejected", "not both" in str(exc), str(exc)[:60])

    try:
        core.check_resume()
        check("no source rejected", False, "no exception")
    except core.BadUpload:
        check("no source rejected", True)

    # A hostile filename must not escape the temp directory.
    docx = DF.build_docx(os.path.join(tmp, "safe.docx"))
    report = core.validate_deliverables(
        docx_b64=b64_of(docx), docx_filename="../../../../etc/passwd.docx")
    check("path traversal in the filename is neutralised",
          report["docx"]["filename"] == "passwd.docx", report["docx"]["filename"])
    check("only the extension is honoured",
          core._suffix_of("x.exe", ".pdf") == ".pdf"
          and core._suffix_of("cv.DOCX", ".pdf") == ".docx")

    # A long job description is clipped rather than rejected.
    clipped = core._clip_jd("x" * (core.MAX_JD_CHARS + 5000))
    check("job description is clipped", len(clipped) == core.MAX_JD_CHARS, str(len(clipped)))


def test_ssrf_guards(tmp: str) -> None:
    section("Server: URL fetch guards")
    blocked = [
        ("http://example.com/cv.pdf", "plain http"),
        ("ftp://example.com/cv.pdf", "non-http scheme"),
        ("https://localhost/cv.pdf", "loopback by name"),
        ("https://127.0.0.1/cv.pdf", "loopback by address"),
        ("https://169.254.169.254/latest/meta-data", "cloud metadata"),
        ("https://192.168.1.10/cv.pdf", "private range"),
        ("https://10.0.0.5/cv.pdf", "private range"),
        ("https://[::1]/cv.pdf", "IPv6 loopback"),
    ]
    for url, why in blocked:
        try:
            fetching.fetch_document(url, 1024)
            check(f"blocked: {why}", False, f"{url} was allowed")
        except fetching.FetchError:
            check(f"blocked: {why}", True)
        except Exception as exc:  # noqa: BLE001
            check(f"blocked: {why}", False, f"wrong exception {type(exc).__name__}: {exc}")

    # An HTML landing page must not be validated as if it were a document.
    try:
        fetching._reject_html_page(
            b"<!DOCTYPE html><html><head><title>iCloud</title></head>",
            "https://www.icloud.com/iclouddrive/0abc#CV")
        check("iCloud share page is rejected", False, "no exception")
    except fetching.FetchError as exc:
        check("iCloud share page is rejected", "iCloud" in str(exc), str(exc)[:70])

    try:
        fetching._reject_html_page(b"<html><body>download</body></html>",
                                   "https://files.example.com/share/abc")
        check("generic HTML page is rejected", False, "no exception")
    except fetching.FetchError as exc:
        check("generic HTML page is rejected", "HTML page" in str(exc), str(exc)[:70])

    for magic, label in fetching.DOC_MAGIC:
        try:
            fetching._reject_html_page(magic + b"rest of file", "https://x.example/cv")
            check(f"{label} passes the HTML guard", True)
        except fetching.FetchError as exc:
            check(f"{label} passes the HTML guard", False, str(exc)[:60])

    try:
        fetching._reject_html_page(b"Giuseppe Lopes\nWORK EXPERIENCE\n",
                                   "https://x.example/cv.txt")
        check("plain text passes the HTML guard", True)
    except fetching.FetchError as exc:
        check("plain text passes the HTML guard", False, str(exc)[:60])

    check("filename guessed from the URL path",
          fetching.guess_filename("https://x.example/a/b/My%20CV.docx", "z.pdf") == "My CV.docx",
          fetching.guess_filename("https://x.example/a/b/My%20CV.docx", "z.pdf"))
    check("filename falls back when the path has none",
          fetching.guess_filename("https://x.example/download?id=7", "z.pdf") == "z.pdf")


def test_no_residue(tmp: str) -> None:
    section("Server: statelessness")
    before = set(os.listdir(tempfile.gettempdir()))
    docx = DF.build_docx(os.path.join(tmp, "residue.docx"))
    core.validate_deliverables(docx_b64=b64_of(docx))
    core.check_resume(content_b64=b64_of(docx), filename="cv.docx")
    after = set(os.listdir(tempfile.gettempdir()))
    leaked = [n for n in (after - before) if n.startswith("ats-")]
    check("no temp directory survives the call", not leaked, str(leaked))


def main() -> int:
    print("ats-mcp server test suite")
    with tempfile.TemporaryDirectory() as tmp:
        for fn in (test_validate, test_check_and_extract, test_upload_guards,
                   test_ssrf_guards, test_no_residue):
            try:
                fn(tmp)
            except Exception as exc:  # noqa: BLE001
                global FAIL
                FAIL += 1
                FAILURES.append(f"{fn.__name__} raised {type(exc).__name__}: {exc}")
                print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")

    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("\nFailures:")
        for f in FAILURES:
            print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
