# Smart ATS Analyzer — as a Claude Skill

Check a resume against an Applicant Tracking System **without a browser, without
an API key, and without a Streamlit app running somewhere**.

This is a rebuild of the Streamlit + Gemini "Smart ATS Analyzer" idea as a
[Claude Skill](https://claude.ai/skills). The difference matters:

|  | Streamlit app | This skill |
|---|---|---|
| Needs a browser | yes | no |
| Needs a Google Gemini API key | yes | no |
| Needs a server running | yes | no |
| Works from an iOS phone | only via the web UI | yes, in the Claude app |
| Cost | Gemini API usage | included in your Claude plan |
| Parseability analysis | none — sends raw text to an LLM | real PDF/DOCX inspection |

The LLM in the loop is Claude itself, so there is no second model to pay for.
The analysis scripts are **standard library only** — no `pip install`, no
network — so they run in the Claude sandbox as-is.

## What it actually checks

Two separate questions, in order:

**1. Can the machine read the file at all?** Measured, not guessed:

- text layer present, or is this a scan pretending to be a document
- fonts with no `ToUnicode` map — the page looks perfect and extracts as `���`
- multi-column layouts, detected geometrically, plus a diff between
  column-aware and stream-order extraction to show what actually gets scrambled
- contact details stranded in a Word header, a PDF header band, or a `mailto:`
  link annotation
- DOCX text boxes and layout tables
- non-standard section headings, unparseable dates, icon-font glyphs
- invisible text and keyword stuffing
- encryption

**2. Does the content match the job?** Requirement terms weighted by JD
section, alias-aware matching (`K8s` satisfies `Kubernetes`), hard gates
(years, degree, language), and skills claimed in a list with no bullet
behind them.

The scripts report **evidence**. Claude reads that evidence, reads the
extracted text, and gives the verdict. There is deliberately no invented
"your ATS score is 72%" — see `references/ats-parseability.md` for why that
number is always fiction.

## Use it from your phone (no Claude Code needed)

```bash
python3 tools/package_skill.py     # -> dist/ats-check.zip
```

Then in **claude.ai → Settings → Capabilities → Skills → Upload skill**, upload
`dist/ats-check.zip`. Enable code execution.

From then on, in the Claude iOS app: attach your resume, paste a job ad, and
say *"check this against ATS"*. It runs server-side in the background. No
browser, no repo, no laptop.

## Use it from this repo

Any Claude Code session opened on this repository picks the skill up
automatically from `.claude/skills/ats-check/`.

```
You: check resumes/cv.pdf against the job description in jd/backend.txt
```

## Use the CLI directly

No Claude involved — just the deterministic half:

```bash
python3 .claude/skills/ats-check/scripts/ats_check.py resumes/cv.pdf --jd jd/role.txt
python3 .claude/skills/ats-check/scripts/ats_check.py resumes/cv.pdf --json
python3 .claude/skills/ats-check/scripts/ats_check.py resumes/cv.pdf --text-only
```

`--text-only` prints exactly what an ATS extracts. Reading that output is the
single most useful thing in this repo — if it looks wrong to you, it is wrong.

Add `--stream-order` to see the raw content-stream read, which is what a naive
parser gets. On a two-column resume the two differ dramatically:

```
$ ats_check.py twocol.pdf --text-only --stream-order
EXPERIENCE SKILLS
Senior Backend Engineer Python
Acme Corp 2020-2024 Kubernetes
```

## Layout

```
.claude/skills/ats-check/
  SKILL.md                     instructions Claude follows
  scripts/
    pdfmini.py                 dependency-free PDF parser
    layout.py                  line grouping, column detection, reading order
    readers.py                 PDF / DOCX / TXT / MD / RTF -> one shape
    ats_lint.py                parseability checks
    jd_match.py                job-description overlap and hard gates
    ats_check.py               CLI entry point
  references/
    ats-parseability.md        why each check exists
    aliases.json               synonym table (extend it freely)
tests/test_ats.py              56 tests, no fixtures on disk
tools/package_skill.py         build + validate the uploadable zip
resumes/                       gitignored — put your resume here
```

### Why a hand-written PDF parser

`pypdf` and friends are not installable in the claude.ai sandbox, which has no
network. Rather than degrade to "upload a .txt", `scripts/pdfmini.py`
implements what is needed directly: xref tables and xref streams, object
streams, Flate/LZW/ASCII85/ASCIIHex with PNG predictors, `ToUnicode` CMaps,
form XObject recursion, and xref reconstruction by scanning when the table is
corrupt.

It also exposes something general-purpose libraries mostly hide: the **position
of every show-text operation**. Column detection and the naive-vs-layout diff
are built on that, and those are the checks that catch the failure most
resume templates actually have.

## Tests

```bash
python3 tests/test_ats.py
```

Every PDF is constructed byte by byte in `tests/fixtures.py`, so the suite
needs no sample files and can exercise structures a normal generator will not
produce — CID fonts with no `ToUnicode`, deliberately corrupted xref offsets,
image-only pages.

## Privacy

Resumes are personal data. `resumes/`, `reports/` and `jd/` are gitignored,
this repository is public, and the analysis runs locally in the session —
nothing is sent to a third-party API.

## Credit

The original [Smart ATS Analyzer](https://github.com/Anubhx/Smart-ATS-Analyzer)
by Anubhav Raj (Streamlit + Gemini Pro) is the idea this reimplements. No code
is shared with it; the approach here is deliberately different.
