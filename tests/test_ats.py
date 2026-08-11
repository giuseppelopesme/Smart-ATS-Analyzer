#!/usr/bin/env python3
"""Test suite for the ats-check skill. Standard library only.

    python3 tests/test_ats.py

Every PDF is built byte by byte in ``fixtures.py`` so the suite needs no
sample files and no third-party PDF writer. That also means the fixtures
exercise the exact structures that break real parsers -- xref streams, object
streams, CID fonts with no ToUnicode -- rather than whatever one generator
happens to emit.
"""

from __future__ import annotations

import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".claude", "skills", "ats-check", "scripts"))
sys.path.insert(0, HERE)

import ats_lint  # noqa: E402
import fixtures  # noqa: E402
import jd_match  # noqa: E402
import layout as layout_mod  # noqa: E402
import pdfmini  # noqa: E402
from readers import UnsupportedFormat, load_any  # noqa: E402

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


def write(tmp: str, name: str, data: bytes) -> str:
    path = os.path.join(tmp, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


GOOD_RESUME_LINES = [
    (72, 730, 20, "Giuseppe Lopes"),
    (72, 712, 10, "giuseppe@lopes.me | +39 333 123 4567 | Bologna, Italy"),
    (72, 684, 13, "SUMMARY"),
    (72, 668, 10, "Platform engineer with 9 years building distributed systems on AWS."),
    (72, 640, 13, "WORK EXPERIENCE"),
    (72, 622, 11, "Senior Platform Engineer, Acme Payments"),
    (430, 622, 10, "2020 - Present"),
    (72, 606, 10, "Built a Kubernetes payment platform handling 4M requests per day."),
    (72, 592, 10, "Cut p99 latency 40% by redesigning the Kafka consumer topology."),
    (72, 578, 10, "Led a team of six engineers and owned the on-call rotation."),
    (72, 556, 11, "Backend Engineer, Globex"),
    (430, 556, 10, "2017 - 2020"),
    (72, 540, 10, "Designed event-driven ingestion in Python and Go with Terraform."),
    (72, 526, 10, "Introduced CI/CD with GitHub Actions, cutting release time to hours."),
    (72, 498, 13, "EDUCATION"),
    (72, 482, 10, "BSc Computer Science, University of Bologna, 2013 - 2016"),
    (72, 454, 13, "SKILLS"),
    (72, 438, 10, "Python, Go, Kubernetes, Docker, AWS, Terraform, Kafka, PostgreSQL"),
]

JD = """Senior Backend Engineer - Payments

About us
We are a fast-paced fintech scale-up. We offer equity and great benefits.

Requirements
- 7+ years of experience building backend services in production
- Strong Python or Go
- Deep knowledge of Kubernetes and Docker in production
- Experience with distributed systems and event streaming (Kafka)
- Hands-on with Terraform and infrastructure as code
- Solid grasp of PostgreSQL
- Bachelor's degree in Computer Science or equivalent

Nice to have
- Experience with gRPC
- Exposure to Rust
- GraphQL API design

Benefits
- Stock options, learning budget, 30 days holiday
"""


def test_pdf_structures(tmp: str) -> None:
    section("PDF container structures")
    variants = {
        "xref table": dict(),
        "flate content": dict(compress=True),
        "xref stream": dict(use_xref_stream=True),
        "object stream": dict(use_objstm=True),
        "corrupt xref (rebuild)": dict(break_xref=True),
    }
    for name, kwargs in variants.items():
        data = fixtures.simple_text_pdf(GOOD_RESUME_LINES, **kwargs)
        doc = pdfmini.Document(data)
        doc.load_pages()
        text = " ".join(s.text for p in doc.pages for s in p.spans)
        check(name, "Giuseppe Lopes" in text and "Kubernetes" in text, repr(text[:60]))


def test_fonts(tmp: str) -> None:
    section("Font decoding")
    doc = pdfmini.Document(fixtures.type0_with_tounicode_pdf("Senior Engineer"))
    doc.load_pages()
    text = "".join(s.text for p in doc.pages for s in p.spans)
    check("ToUnicode CMap decodes", text == "Senior Engineer", repr(text))

    path = write(tmp, "garbled.pdf", fixtures.type0_no_tounicode_pdf("Senior Engineer"))
    ex = load_any(path)
    ids = {f.id for f in ats_lint.lint(ex)}
    check("missing ToUnicode is flagged", "unmapped_glyphs" in ids, str(sorted(ids)))


def test_layout(tmp: str) -> None:
    section("Layout and reading order")
    doc = pdfmini.Document(fixtures.two_column_pdf())
    doc.load_pages()
    lay = layout_mod.analyse_page(doc.pages[0])
    check("two-column gutter found", lay.multi_column and len(lay.columns) == 2,
          f"columns={len(lay.columns)}")

    col_text = layout_mod.layout_text(lay)
    check("columns read separately",
          "Senior Backend Engineer\n" in col_text and "SKILLS\nPython" in col_text,
          repr(col_text[:80]))

    naive = layout_mod.naive_text(doc.pages[0])
    check("stream order interleaves columns", "EXPERIENCE SKILLS" in naive, repr(naive[:60]))

    # A right-aligned date column must NOT be mistaken for a second column.
    doc2 = pdfmini.Document(fixtures.simple_text_pdf(GOOD_RESUME_LINES))
    doc2.load_pages()
    lay2 = layout_mod.analyse_page(doc2.pages[0])
    check("right-aligned dates are not a column", not lay2.multi_column,
          f"gutters={lay2.gutters}")


def test_lint(tmp: str) -> None:
    section("Parseability findings")
    good = write(tmp, "good.pdf", fixtures.simple_text_pdf(GOOD_RESUME_LINES, compress=True))
    ex = load_any(good)
    findings = ats_lint.lint(ex)
    ids = {f.id for f in findings}
    check("clean resume has no blockers",
          not any(f.severity == "blocker" for f in findings), str(sorted(ids)))
    check("email detected", "no_email" not in ids)
    check("phone detected", "no_phone" not in ids)
    check("sections detected", "missing_sections" not in ids, str(sorted(ids)))
    check("dates detected", "no_dates" not in ids and "no_date_ranges" not in ids)
    check("score is high", ats_lint.parseability_score(findings) >= 85,
          str(ats_lint.parseability_score(findings)))

    scanned = write(tmp, "scanned.pdf", fixtures.scanned_pdf())
    ids = {f.id for f in ats_lint.lint(load_any(scanned))}
    check("image-only resume flagged as blocker", "scanned_no_text" in ids, str(sorted(ids)))

    twocol = write(tmp, "twocol.pdf", fixtures.two_column_pdf())
    ids = {f.id for f in ats_lint.lint(load_any(twocol))}
    check("multi-column flagged", "multi_column" in ids, str(sorted(ids)))
    check("order dependence flagged", "order_dependent" in ids, str(sorted(ids)))


def test_docx(tmp: str) -> None:
    section("DOCX")
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

    def para(t: str) -> str:
        return f'<w:p><w:r><w:t xml:space="preserve">{t}</w:t></w:r></w:p>'

    body = "".join(para(t) for t in [
        "Giuseppe Lopes", "WORK EXPERIENCE",
        "Senior Platform Engineer, Acme 2020 - Present",
        "Built a Kubernetes payment platform in Python and Go on AWS.",
        "EDUCATION", "BSc Computer Science, Bologna 2013 - 2016",
    ])
    table = f"<w:tbl><w:tr><w:tc>{para('Skills')}</w:tc><w:tc>{para('Kafka, Docker')}</w:tc></w:tr></w:tbl>"
    tbox = ('<w:p><w:r><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml"><v:textbox>'
            f"<w:txbxContent>{para('Certified Kubernetes Administrator')}</w:txbxContent>"
            "</v:textbox></v:shape></w:pict></w:r></w:p>")
    doc_xml = f'<?xml version="1.0"?><w:document {W}><w:body>{body}{table}{tbox}</w:body></w:document>'
    hdr = f'<?xml version="1.0"?><w:hdr {W}>{para("giuseppe@lopes.me | +39 333 123 4567")}</w:hdr>'

    path = os.path.join(tmp, "resume.docx")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("word/document.xml", doc_xml)
        z.writestr("word/header1.xml", hdr)

    ex = load_any(path)
    check("docx body text extracted", "Kubernetes payment platform" in ex.text, repr(ex.text[:60]))
    check("docx table cells extracted", "Kafka" in ex.text)
    check("table counted", ex.tables == 1, str(ex.tables))
    check("text box counted", ex.textboxes == 1, str(ex.textboxes))

    ids = {f.id for f in ats_lint.lint(ex)}
    check("email in docx header flagged", "contact_in_docx_header" in ids, str(sorted(ids)))
    check("text box flagged", "docx_textboxes" in ids, str(sorted(ids)))

    bad = os.path.join(tmp, "notreally.docx")
    with open(bad, "wb") as fh:
        fh.write(b"this is not a zip")
    try:
        load_any(bad)
        check("corrupt docx raises", False, "no exception")
    except UnsupportedFormat:
        check("corrupt docx raises", True)
    except Exception as exc:  # noqa: BLE001
        check("corrupt docx raises", False, f"wrong type: {type(exc).__name__}")


def test_matching(tmp: str) -> None:
    section("Job description matching")
    ex = load_any(write(tmp, "good2.pdf", fixtures.simple_text_pdf(GOOD_RESUME_LINES, compress=True)))
    report = jd_match.match(ex.text, JD)

    matched = {t.canonical for t in report.matched}
    missing = {t.canonical for t in report.missing}

    check("kubernetes matched", any("kubernete" in m for m in matched), str(sorted(matched)))
    check("terraform matched", any("terraform" in m for m in matched))
    check("rust correctly missing", any("rust" in m for m in missing), str(sorted(missing)))
    check("graphql correctly missing", any("graphql" in m for m in missing))

    display = {t.term for t in report.matched} | {t.term for t in report.missing}
    check("display spelling is not singularised", "kubernete" not in display,
          str(sorted(d for d in display if "kub" in d)))

    filler = {"deep", "solid", "strong", "exposure", "fluent", "hands-on", "proven"}
    check("filler adjectives are not treated as requirements",
          not (filler & {t.canonical for t in report.missing}),
          str(sorted(filler & {t.canonical for t in report.missing})))

    check("required coverage is plausible", 0.5 <= report.coverage_required <= 1.0,
          str(report.coverage_required))

    years = [g for g in report.gates if g.kind == "years"]
    check("years gate extracted", years and years[0].value == 7.0, str(years))
    degree = [g for g in report.gates if g.kind == "degree"]
    check("degree gate satisfied", degree and degree[0].satisfied, str(degree))

    # Aliases: a resume written with abbreviations must still match.
    alias_resume = ("Giuseppe Lopes giuseppe@lopes.me\nWORK EXPERIENCE\n"
                    "Ran K8s clusters 2018 - 2024, wrote JS and TS, managed Postgres.\n"
                    "EDUCATION BSc 2013 - 2016\n")
    rep2 = jd_match.match(alias_resume, "Requirements\n- Kubernetes\n- JavaScript\n- PostgreSQL\n")
    m2 = {t.canonical for t in rep2.matched}
    check("K8s satisfies Kubernetes", any("kubernete" in m for m in m2), str(sorted(m2)))
    check("JS satisfies JavaScript", any("javascript" in m for m in m2), str(sorted(m2)))
    check("Postgres satisfies PostgreSQL", any("postgresql" in m for m in m2), str(sorted(m2)))

    est = jd_match._estimate_resume_years("Acme 2020 - 2024\nGlobex 2017 - 2020\n")
    check("overlapping tenure merged", est == 7.0, str(est))
    est2 = jd_match._estimate_resume_years("Acme 2018 - 2024\nGlobex 2017 - 2020\n")
    check("overlapping ranges not double counted", est2 == 7.0, str(est2))


def test_cli(tmp: str) -> None:
    section("CLI")
    import json
    import subprocess

    resume = write(tmp, "cli.pdf", fixtures.simple_text_pdf(GOOD_RESUME_LINES, compress=True))
    jd_path = os.path.join(tmp, "jd.txt")
    with open(jd_path, "w", encoding="utf-8") as fh:
        fh.write(JD)
    script = os.path.join(ROOT, ".claude", "skills", "ats-check", "scripts", "ats_check.py")

    r = subprocess.run([sys.executable, script, resume, "--jd", jd_path, "--json"],
                       capture_output=True, text=True, timeout=90)
    check("json run exits 0", r.returncode == 0, r.stderr[:200])
    try:
        data = json.loads(r.stdout)
        check("json parses", True)
        check("json has parseability", "parseability" in data and "score" in data["parseability"])
        check("json has jd_match", "jd_match" in data)
        check("json carries extracted text", "Kubernetes" in data.get("extracted_text", ""))
    except json.JSONDecodeError as exc:
        check("json parses", False, str(exc))

    r = subprocess.run([sys.executable, script, resume, "--jd", jd_path],
                       capture_output=True, text=True, timeout=90)
    check("markdown run exits 0", r.returncode == 0, r.stderr[:200])
    check("markdown has a heading", r.stdout.startswith("# ATS parse report"))

    r = subprocess.run([sys.executable, script, resume, "--text-only"],
                       capture_output=True, text=True, timeout=90)
    check("text-only works", r.returncode == 0 and "Giuseppe Lopes" in r.stdout)

    r = subprocess.run([sys.executable, script, os.path.join(tmp, "nope.pdf")],
                       capture_output=True, text=True, timeout=90)
    check("missing file exits non-zero", r.returncode != 0)

    r = subprocess.run([sys.executable, script, resume], capture_output=True, text=True, timeout=90)
    check("runs with no JD", r.returncode == 0 and "Job description overlap" not in r.stdout)


def test_robustness(tmp: str) -> None:
    section("Robustness")
    junk = write(tmp, "junk.pdf", b"%PDF-1.4\nnot really a pdf at all\n%%EOF")
    try:
        ex = load_any(junk)
        check("garbage PDF does not crash", True, f"words={ex.word_count}")
    except pdfmini.PdfError:
        check("garbage PDF does not crash", True, "raised PdfError (acceptable)")
    except Exception as exc:  # noqa: BLE001
        check("garbage PDF does not crash", False, f"{type(exc).__name__}: {exc}")

    empty = write(tmp, "empty.pdf", b"")
    try:
        load_any(empty)
        check("empty file rejected", False, "no exception")
    except (pdfmini.PdfError, UnsupportedFormat):
        check("empty file rejected", True)
    except Exception as exc:  # noqa: BLE001
        check("empty file rejected", False, f"{type(exc).__name__}")

    pages_file = write(tmp, "cv.pages", b"PK\x03\x04garbage")
    try:
        load_any(pages_file)
        check(".pages rejected with advice", False, "no exception")
    except UnsupportedFormat as exc:
        check(".pages rejected with advice", "Export to PDF" in str(exc), str(exc))
    except Exception as exc:  # noqa: BLE001
        check(".pages rejected with advice", False, f"{type(exc).__name__}")

    # Pathological input must not hang the regex engine.
    import time
    nasty = ("Skills\n" + "a " * 4000 + "\n") * 5 + "x" * 20000
    t0 = time.time()
    jd_match.match(nasty, JD)
    elapsed = time.time() - t0
    check("pathological input completes quickly", elapsed < 10, f"{elapsed:.1f}s")

    txt = os.path.join(tmp, "resume.txt")
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write("Giuseppe Lopes\ngiuseppe@lopes.me\nWORK EXPERIENCE\nEngineer 2020 - 2024\n")
    ex = load_any(txt)
    check("plain text loads", ex.kind == "text" and "Giuseppe" in ex.text)


def test_validator(tmp: str) -> None:
    section("Canonical structure validator")
    import copy

    import docx_fixtures as DF
    import validate as V
    from docx_inspect import DocxError, inspect_docx

    spec = V.load_spec()

    good = DF.build_docx(os.path.join(tmp, "good.docx"))
    model = inspect_docx(good)
    checks = V.validate_structure(model, spec)
    fails = {c.id for c in checks if c.ok is False}
    check("canonical docx passes every structure check", not fails, str(sorted(fails)))
    check("canonical docx scores 100", V.score(checks) == 100, str(V.score(checks)))

    broken = DF.build_broken_docx(os.path.join(tmp, "broken.docx"))
    bmodel = inspect_docx(broken)
    bfails = {c.id for c in V.validate_structure(bmodel, spec) if c.ok is False}
    for cid, why in [
        ("page.margins", "wrong margins"),
        ("page.columns", "two columns"),
        ("no.tables", "layout table"),
        ("no.images", "inline image"),
        ("no.header", "text in the Word header"),
        ("bullets.real", "typed bullet glyphs"),
        ("header.name", "name not 18pt bold"),
        ("header.contact", "malformed contact line"),
        ("header.linkedin", "LinkedIn not a hyperlink"),
        ("sections.present", "missing sections"),
        ("sections.order", "EDUCATION after LANGUAGES"),
        ("sections.formatting", "heading without a bottom rule"),
        ("summary.shape", "summary not 3 paragraphs"),
        ("achievements.count", "wrong bullet count"),
        ("achievements.quantified", "unquantified achievement"),
        ("experience.roles", "malformed company/date line"),
        ("meta.author", "empty author"),
        ("meta.title", "title not 'Name - Role - CV'"),
    ]:
        check(f"broken docx flags {cid} ({why})", cid in bfails, str(sorted(bfails)))

    # A check with nothing to inspect must skip, never silently pass.
    bchecks = {c.id: c for c in V.validate_structure(bmodel, spec)}
    check("company_format skips when no company lines were found",
          bchecks["experience.company_format"].ok is None,
          str(bchecks["experience.company_format"].ok))

    # ---- round trip ------------------------------------------------------
    pdf_path = os.path.join(tmp, "ats.pdf")
    with open(pdf_path, "wb") as fh:
        fh.write(fixtures.multi_page_text_pdf(DF.plain_text()))
    pdf = load_any(pdf_path)
    cmp_ = V.compare_text(DF.plain_text(), pdf.text)
    check("docx/PDF round trip is exact", cmp_["similarity"] >= 0.99,
          str(cmp_["similarity"]))
    check("round-trip PDF paginates to 2 pages", len(pdf.pages) == 2, str(len(pdf.pages)))

    other = load_any(os.path.join(tmp, "twocol.pdf")) if os.path.exists(
        os.path.join(tmp, "twocol.pdf")) else None
    if other is not None:
        cmp_bad = V.compare_text(DF.plain_text(), other.text)
        check("round trip detects a mismatched PDF", cmp_bad["similarity"] < 0.5,
              str(cmp_bad["similarity"]))

    # A PDF that dropped a bullet must be named, not just scored.
    orig = copy.deepcopy(DF.ROLES)
    try:
        DF.ROLES[1] = (DF.ROLES[1][0], DF.ROLES[1][1], DF.ROLES[1][2][:-1])
        short_text = DF.plain_text()
    finally:
        DF.ROLES[:] = orig
    full_text = DF.plain_text()

    short_pdf = os.path.join(tmp, "short.pdf")
    with open(short_pdf, "wb") as fh:
        fh.write(fixtures.multi_page_text_pdf(short_text))

    blocks = V.compare_blocks(full_text, load_any(short_pdf).text)
    check("a bullet missing from the PDF is detected as a missing block",
          blocks["missing_count"] >= 1, str(blocks["missing_count"]))
    check("the missing block names the dropped text",
          any("governance board" in b["text"] for b in blocks["missing_blocks"]),
          str(blocks["missing_blocks"][:2]))

    blocks_ok = V.compare_blocks(full_text, pdf.text)
    check("a faithful PDF reports no missing blocks",
          blocks_ok["missing_count"] == 0, str(blocks_ok["missing_blocks"][:3]))

    # Reordering alone must not be reported as dropped content.
    shuffled = "\n".join(reversed(full_text.split("\n")))
    blocks_shuf = V.compare_blocks(shuffled, pdf.text)
    check("reordering is not treated as missing content",
          blocks_shuf["missing_count"] == 0, str(blocks_shuf["missing_count"]))

    # The ATS PDF must be single column; a Canva-style export must be caught.
    r_single = load_any(pdf_path)
    check("ATS PDF is detected as single column",
          not any(p.multi_column for p in r_single.pages),
          str([p.number for p in r_single.pages if p.multi_column]))
    twocol_path = write(tmp, "design_like.pdf", fixtures.two_column_pdf())
    check("a two-column export is caught as multi column",
          any(p.multi_column for p in load_any(twocol_path).pages))

    # ---- CLI ---------------------------------------------------------------
    import subprocess

    script = os.path.join(ROOT, ".claude", "skills", "ats-check", "scripts", "ats_validate.py")
    r = subprocess.run([sys.executable, script, good, "--pdf", pdf_path, "--json"],
                       capture_output=True, text=True, timeout=120)
    check("validator CLI exits 0 on a clean build", r.returncode == 0, r.stderr[:200])
    try:
        import json as _json
        data = _json.loads(r.stdout)
        check("validator JSON has a verdict",
              data["validation"]["passed"] is True, str(data["validation"]["counts"]))
        check("validator JSON scores 100", data["validation"]["score"] == 100,
              str(data["validation"]["score"]))
    except (ValueError, KeyError) as exc:
        check("validator JSON has a verdict", False, str(exc))

    r = subprocess.run([sys.executable, script, broken], capture_output=True,
                       text=True, timeout=120)
    check("validator CLI exits 1 on a failing build", r.returncode == 1, str(r.returncode))

    notdocx = os.path.join(tmp, "nope.docx")
    with open(notdocx, "wb") as fh:
        fh.write(b"not a zip at all")
    r = subprocess.run([sys.executable, script, notdocx], capture_output=True,
                       text=True, timeout=120)
    check("validator CLI exits 2 on unreadable input", r.returncode == 2, str(r.returncode))
    try:
        inspect_docx(notdocx)
        check("inspect_docx raises on non-docx", False, "no exception")
    except DocxError:
        check("inspect_docx raises on non-docx", True)


def main() -> int:
    import tempfile

    print("ats-check test suite")
    with tempfile.TemporaryDirectory() as tmp:
        for fn in (test_pdf_structures, test_fonts, test_layout, test_lint,
                   test_docx, test_matching, test_cli, test_robustness,
                   test_validator):
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
