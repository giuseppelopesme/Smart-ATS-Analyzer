#!/usr/bin/env python3
"""Validate ATS deliverables before they are archived or submitted.

    python3 ats_validate.py CV.docx [--pdf CV.pdf] [--jd JD.txt] [--json]
                            [--spec-local spec.local.json]

Runs the gate:

  1. structure    -- the docx against the canonical spec
  2. round trip   -- docx text vs ATS PDF text, proving the PDF was generated
                     from this docx and dropped nothing
  3. parseability -- the ATS PDF through the same checks any resume gets,
                     including that it really is single column
  4. keywords     -- JD overlap, when a JD is supplied

Scope is the two ATS deliverables only. The Canva design export is a separate
artefact with its own design QA gate and is deliberately not inspected here:
it is multi-column by construction, so measuring it against a single-column
ATS build would only ever produce noise.

Exit status: 0 all checks passed, 1 one or more failed, 2 could not run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ats_lint  # noqa: E402
import jd_match  # noqa: E402
import validate as V  # noqa: E402
from docx_inspect import DocxError, inspect_docx  # noqa: E402
from readers import UnsupportedFormat, load_any  # noqa: E402

ICON = {"pass": "✅", "fail": "❌", "skip": "⚪"}


def run(
    docx_path: str,
    pdf_path: Optional[str] = None,
    jd_text: str = "",
    spec_local: Optional[str] = None,
) -> Dict[str, Any]:
    spec = V.load_spec(local=spec_local)
    model = inspect_docx(docx_path)

    checks: List[V.Check] = V.validate_structure(model, spec)
    docx_text = model.text
    out: Dict[str, Any] = {
        "docx": {
            "path": os.path.abspath(docx_path),
            "filename": os.path.basename(docx_path),
            "words": model.word_count,
            "paragraphs": len(model.body_paragraphs),
            "fonts": sorted(model.fonts_used),
            "tables": model.tables,
            "textboxes": model.textboxes,
            "images": model.images,
            "metadata": model.metadata,
            "warnings": model.warnings,
        },
        "extracted_text": docx_text,
    }

    # ---- round trip against the ATS PDF ---------------------------------
    thresholds = spec.get("parity", {})
    if pdf_path:
        try:
            pdf = load_any(pdf_path)
            cmp_ = V.compare_text(docx_text, pdf.text)
            # A similarity number says something is wrong; block matching says
            # which paragraph went missing, which is what actually gets fixed.
            blocks = V.compare_blocks(docx_text, pdf.text)
            want = thresholds.get("docx_pdf_min_similarity", 0.98)
            ok = cmp_["similarity"] >= want and blocks["missing_count"] == 0
            dropped = "; ".join(f'"{b["text"][:80]}"'
                                for b in blocks["missing_blocks"][:5])
            checks.append(V.Check(
                "parity.docx_pdf", ok,
                "ATS PDF text matches the docx",
                (f"{blocks['missing_count']} block(s) of the docx have no counterpart "
                 f"in the PDF: {dropped}. " if blocks["missing_count"] else "")
                + f"Word-sequence similarity {cmp_['similarity']:.3f} (need ≥ {want}); "
                f"{cmp_['only_in_a_count']} word(s) only in the docx, "
                f"{cmp_['only_in_b_count']} only in the PDF",
                "Regenerate the PDF from this exact docx. If they still differ, "
                "something is being dropped on export — check for content in "
                "shapes or fields.",
                severity="blocker",
                evidence={**{k: cmp_[k] for k in
                             ("similarity", "only_in_a", "only_in_b", "words_a", "words_b")},
                          **blocks},
            ))
            out["pdf"] = {
                "path": os.path.abspath(pdf_path),
                "pages": len(pdf.pages),
                "words": pdf.word_count,
                "comparison": cmp_,
                "missing_blocks": blocks["missing_blocks"],
            }

            # Single column is the whole point of the ATS build, so name it as
            # its own check rather than leaving it inside the parseability blob.
            multi = [p.number for p in pdf.pages if p.multi_column]
            checks.append(V.Check(
                "pdf.single_column", not multi,
                "ATS PDF is single column",
                f"a column gutter was detected on page(s) {multi}" if multi
                else f"single column across {len(pdf.pages)} page(s)",
                "The ATS PDF must be a single-column rebuild. If a gutter is "
                "detected, the PDF was exported from the Canva design rather "
                "than generated from the ATS docx.",
                severity="blocker",
                evidence={"pages": multi},
            ))

            pdf_findings = ats_lint.lint(pdf, jd_text or None)
            blockers = [f for f in pdf_findings if f.severity in ("blocker", "high")]
            checks.append(V.Check(
                "pdf.parseability", not blockers,
                "ATS PDF has no parseability blockers",
                "; ".join(f"{f.title}" for f in blockers) if blockers else "clean",
                "Fix the PDF issues listed in the parseability section.",
                severity="high",
                evidence={"findings": [f.id for f in blockers]},
            ))
            max_pages = spec["page"].get("max_pages")
            if max_pages:
                checks.append(V.Check(
                    "pdf.pages", len(pdf.pages) <= max_pages,
                    f"ATS PDF is at most {max_pages} pages", f"{len(pdf.pages)} page(s)",
                    "Trim content to fit two pages.", severity="medium",
                ))
            out["pdf"]["parseability"] = {
                "score": ats_lint.parseability_score(pdf_findings),
                "findings": ats_lint.findings_as_dicts(pdf_findings),
            }
        except (UnsupportedFormat, FileNotFoundError, OSError) as exc:
            checks.append(V.Check("parity.docx_pdf", None, "ATS PDF text matches the docx",
                                  f"could not read the PDF: {exc}"))
    else:
        checks.append(V.Check("parity.docx_pdf", None, "ATS PDF text matches the docx",
                              "no --pdf supplied; the round trip could not be run"))
        checks.append(V.Check("pdf.parseability", None,
                              "ATS PDF has no parseability blockers", "no --pdf supplied"))
        checks.append(V.Check("pdf.single_column", None,
                              "ATS PDF is single column", "no --pdf supplied"))

    # ---- keyword check ------------------------------------------------------
    if jd_text.strip():
        m = jd_match.match(docx_text, jd_text)
        out["jd_match"] = m.to_dict()
        required_missing = [t for t in m.missing if t.section == "required"]
        checks.append(V.Check(
            "jd.keywords", not required_missing,
            "JD required terms appear in the CV",
            f"{len(required_missing)} required term(s) absent: "
            f"{', '.join(t.term for t in required_missing[:8])}"
            if required_missing else "all required terms present",
            "Work the missing terms in naturally where they are true. Do not "
            "stuff keywords that are not backed by real experience.",
            severity="medium",
            evidence={"missing": [t.term for t in required_missing[:15]],
                      "coverage_required": round(m.coverage_required, 3)},
        ))
        failed_gates = [g for g in m.gates if g.satisfied is False]
        checks.append(V.Check(
            "jd.gates", not failed_gates, "JD hard requirements are met",
            "; ".join(f"{g.text}" for g in failed_gates) if failed_gates else "all met",
            "A missed hard gate matters more than any keyword. Decide whether to "
            "apply anyway, and say so in the application note.",
            severity="low",
            evidence={"failed": [g.text for g in failed_gates]},
        ))
    else:
        checks.append(V.Check("jd.keywords", None, "JD required terms appear in the CV",
                              "no --jd supplied"))

    out["validation"] = V.ValidationReport(checks=checks, score=V.score(checks)).to_dict()
    return out


def render(out: Dict[str, Any]) -> str:
    v = out["validation"]
    d = out["docx"]
    lines: List[str] = []
    verdict = "PASS" if v["passed"] else "FAIL"
    lines.append(f"# ATS validation — {d['filename']}")
    lines.append("")
    lines.append(f"**{verdict} · {v['score']}/100** · "
                 f"{v['counts']['pass']} passed, {v['counts']['fail']} failed, "
                 f"{v['counts']['skip']} skipped · {d['words']} words")
    lines.append("")

    fails = [c for c in v["checks"] if c["status"] == "fail"]
    if fails:
        lines.append("## Must fix")
        lines.append("")
        for c in fails:
            lines.append(f"### {ICON['fail']} {c['title']}  `{c['id']}`")
            lines.append("")
            lines.append(c["detail"])
            if c.get("fix"):
                lines.append("")
                lines.append(f"**Fix:** {c['fix']}")
            lines.append("")
    else:
        lines.append("Every check that could be run passed.")
        lines.append("")

    skips = [c for c in v["checks"] if c["status"] == "skip"]
    if skips:
        lines.append("## Not verified")
        lines.append("")
        for c in skips:
            lines.append(f"- {ICON['skip']} **{c['title']}** — {c['detail']}")
        lines.append("")

    lines.append("## All checks")
    lines.append("")
    for c in v["checks"]:
        lines.append(f"- {ICON[c['status']]} `{c['id']}` {c['title']}")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Validate ATS deliverables against the canonical structure.")
    ap.add_argument("docx", help="the ATS .docx")
    ap.add_argument("--pdf", help="the ATS PDF generated from that docx")
    ap.add_argument("--jd", help="job description file")
    ap.add_argument("--jd-text", default="")
    ap.add_argument("--spec-local", help="local spec overrides (gitignored)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    jd_text = args.jd_text
    if args.jd:
        try:
            jd_text = load_any(args.jd).text
        except Exception:  # noqa: BLE001
            try:
                with open(args.jd, "r", encoding="utf-8", errors="replace") as fh:
                    jd_text = fh.read()
            except OSError as exc:
                print(f"error: cannot read JD: {exc}", file=sys.stderr)
                return 2

    default_local = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "references", "spec.local.json")
    spec_local = args.spec_local or (default_local if os.path.exists(default_local) else None)

    try:
        out = run(args.docx, args.pdf, jd_text, spec_local)
    except DocxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: no such file: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render(out))
    return 0 if out["validation"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
