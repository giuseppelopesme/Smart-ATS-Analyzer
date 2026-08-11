#!/usr/bin/env python3
"""Run the deterministic half of an ATS review.

    python3 ats_check.py RESUME [--jd JD_FILE | --jd-text "..."] [--json] [--text-only]

Outputs a Markdown briefing by default, or JSON with --json. Nothing here
talks to a network or to a model: it reports measurable facts about the file
and its overlap with a job description, and leaves the judgement to the
caller.

Exit status is 0 unless the resume could not be read at all.
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
from readers import UnsupportedFormat, load_any  # noqa: E402

SEVERITY_ICON = {
    "blocker": "🛑", "high": "⚠️ ", "medium": "•", "low": "·", "info": "ℹ️ ",
}


def build_report(
    resume_path: str,
    jd_text: Optional[str] = None,
    max_pages: int = 30,
) -> Dict[str, Any]:
    ex = load_any(resume_path, max_pages=max_pages)
    findings = ats_lint.lint(ex, jd_text)
    score = ats_lint.parseability_score(findings)

    report: Dict[str, Any] = {
        "resume": {
            "path": os.path.abspath(resume_path),
            "filename": os.path.basename(resume_path),
            "format": ex.kind,
            "pages": len(ex.pages),
            "words": ex.word_count,
            "encrypted": ex.encrypted,
            "producer": ex.meta.get("producer", ""),
            "fonts": ex.fonts,
            "tables": ex.tables,
            "textboxes": ex.textboxes,
        },
        "parseability": {
            "score": score,
            "findings": ats_lint.findings_as_dicts(findings),
            "blockers": sum(1 for f in findings if f.severity == "blocker"),
            "high": sum(1 for f in findings if f.severity == "high"),
        },
        "extracted_text": ex.text,
        "extracted_text_stream_order": ex.naive_text,
        "pages": [
            {
                "number": p.number,
                "multi_column": p.multi_column,
                "gutters": p.gutters,
                "images": p.n_images,
                "image_area_ratio": p.image_area_ratio,
                "body_size": p.body_size,
                "links": p.link_uris,
            }
            for p in ex.pages
        ],
        "warnings": ex.warnings,
    }

    if jd_text and jd_text.strip():
        m = jd_match.match(ex.text, jd_text)
        report["jd_match"] = m.to_dict()
    return report


# --------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------


def render_markdown(report: Dict[str, Any]) -> str:
    r = report["resume"]
    p = report["parseability"]
    out: List[str] = []
    out.append(f"# ATS parse report — {r['filename']}")
    out.append("")
    out.append(
        f"**Parseability {p['score']}/100** · {r['format'].upper()} · "
        f"{r['pages']} page(s) · {r['words']} words"
        + (f" · produced by {r['producer']}" if r.get("producer") else "")
    )
    out.append("")

    if p["findings"]:
        out.append("## Parseability findings")
        out.append("")
        for f in p["findings"]:
            if f["severity"] == "info":
                continue
            icon = SEVERITY_ICON.get(f["severity"], "•")
            out.append(f"### {icon} [{f['severity'].upper()}] {f['title']}")
            out.append("")
            out.append(f["detail"])
            if f.get("fix"):
                out.append("")
                out.append(f"**Fix:** {f['fix']}")
            out.append("")
    else:
        out.append("No parseability problems detected. The file extracts cleanly.")
        out.append("")

    jd = report.get("jd_match")
    if jd:
        out.append("## Job description overlap")
        out.append("")
        out.append(
            f"Weighted coverage of *required* terms: **{jd['coverage_required'] * 100:.0f}%** · "
            f"across all terms: **{jd['coverage_all'] * 100:.0f}%**"
        )
        out.append("")
        out.append(
            "_These percentages are lexical overlap only — they say nothing about whether the "
            "underlying experience actually fits. Read the lists below, not the number._"
        )
        out.append("")

        if jd["gates"]:
            out.append("### Hard requirements")
            out.append("")
            for g in jd["gates"]:
                mark = {True: "✅", False: "❌", None: "❓"}[g["satisfied"]]
                subject = f" — {g['subject']}" if g.get("subject") else ""
                note = f" ({g['note']})" if g.get("note") else ""
                out.append(f"- {mark} `{g['text']}`{subject}{note}")
            out.append("")

        missing = jd["missing"]
        if missing:
            out.append("### Terms in the JD, absent from the resume")
            out.append("")
            req = [t for t in missing if t["section"] == "required"]
            other = [t for t in missing if t["section"] != "required"]
            if req:
                out.append("From the requirements section:")
                out.append("")
                for t in req[:20]:
                    out.append(f"- **{t['term']}** (JD mentions: {t['jd_count']})")
                out.append("")
            if other:
                out.append("Elsewhere in the posting:")
                out.append("")
                out.append(", ".join(f"`{t['term']}`" for t in other[:25]))
                out.append("")

        if jd["skills_list_only"]:
            out.append("### Listed as a skill but never demonstrated")
            out.append("")
            out.append(
                "These appear in a skills list and nowhere else. An interviewer will ask for a "
                "story and there is none in the document:"
            )
            out.append("")
            out.append(", ".join(f"`{t['term']}`" for t in jd["skills_list_only"][:20]))
            out.append("")

        matched = jd["matched"]
        if matched:
            out.append("### Matched")
            out.append("")
            out.append(", ".join(f"`{t['term']}`" for t in matched[:35]))
            out.append("")

    return "\n".join(out).rstrip() + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic ATS parseability and job-description overlap check.",
    )
    ap.add_argument("resume", help="path to a .pdf, .docx, .txt or .md resume")
    ap.add_argument("--jd", help="path to a file containing the job description")
    ap.add_argument("--jd-text", help="job description as a literal string")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    ap.add_argument("--text-only", action="store_true",
                    help="print only the extracted resume text and exit")
    ap.add_argument("--stream-order", action="store_true",
                    help="with --text-only, show raw content-stream order instead")
    ap.add_argument("--max-pages", type=int, default=30)
    args = ap.parse_args(argv)

    jd_text = args.jd_text or ""
    if args.jd:
        try:
            jd_ex = load_any(args.jd)
            jd_text = jd_ex.text
        except (UnsupportedFormat, FileNotFoundError, OSError):
            try:
                with open(args.jd, "r", encoding="utf-8", errors="replace") as fh:
                    jd_text = fh.read()
            except OSError as exc:
                print(f"error: cannot read job description: {exc}", file=sys.stderr)
                return 2

    try:
        if args.text_only:
            ex = load_any(args.resume, max_pages=args.max_pages)
            sys.stdout.write(ex.naive_text if args.stream_order else ex.text)
            sys.stdout.write("\n")
            return 0
        report = build_report(args.resume, jd_text, max_pages=args.max_pages)
    except FileNotFoundError:
        print(f"error: no such file: {args.resume}", file=sys.stderr)
        return 2
    except UnsupportedFormat as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"error: could not process {args.resume}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
