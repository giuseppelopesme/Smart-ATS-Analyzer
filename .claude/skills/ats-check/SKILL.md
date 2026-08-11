---
name: ats-check
description: Check a resume or CV against an Applicant Tracking System, with or without a job description. Use when the user asks whether their resume will pass ATS screening, wants a resume scored or matched against a job posting or job ad, asks about keyword gaps, asks why they are not getting interviews, wants a CV reviewed or tailored for a specific role, or uploads a resume file (PDF/DOCX) in a job-application context. Runs fully offline with no API key and no browser.
---

# ATS resume check

Predict what an Applicant Tracking System does to a resume, and say what to
change. Two independent questions, always answered in this order:

1. **Can the machine read the file at all?** Layout, fonts, encoding, contact
   details. A resume that extracts as garbage scores zero against every job.
2. **Does the content match the job?** Keyword and requirement overlap, hard
   gates, unsupported claims.

Question 1 is measurable, and `ats_check.py` measures it. Question 2 is a
judgement call, and that is your job. **The script reports evidence; you
report the verdict.** Never paste the script's coverage percentage as if it
were a score of the candidate — it is lexical overlap, nothing more.

## Workflow

### 1. Locate the resume

Look in this order and use the first hit:

- a file the user attached or named in this conversation;
- `resumes/` in the working directory (gitignored — this is where a resume
  lives when working from the repo);
- ask, if neither exists. Accept `.pdf`, `.docx`, `.txt`, `.md`, `.rtf`.

If the user pasted resume text rather than a file, write it to a temporary
`.txt` and run against that — the JD-matching half still works, and the
report should state that file-level parseability could not be checked.

### 2. Get the job description, if there is one

A JD is optional. Without one, run the parseability half and report on that
alone; it is genuinely useful on its own.

With one, accept any of: pasted text, a local file, a URL (fetch it), or a
job ID from a connected job-board tool. Save the JD text to a file and pass
`--jd`, rather than shell-quoting a long string.

### 3. Run the analyser

```bash
python3 .claude/skills/ats-check/scripts/ats_check.py RESUME --jd JD.txt --json
```

Standard library only — no install step, no network, no API key. Useful flags:

| Flag | Purpose |
|---|---|
| `--json` | full structured output; prefer this when you are going to reason over it |
| (no flag) | rendered Markdown, good for showing the user directly |
| `--text-only` | just the extracted text, as a layout-aware read |
| `--text-only --stream-order` | the same page in raw content-stream order |
| `--jd-text "..."` | inline JD instead of a file |

**Always read the extracted text yourself** (`extracted_text` in the JSON).
It is what the ATS sees. If it reads as nonsense to you, that is the finding —
lead with it, whatever the score says.

When the report flags a multi-column layout, diff `extracted_text` against
`extracted_text_stream_order` and quote the specific line where they diverge.
A user who sees `Senior Backend Engineer Python` where they wrote two separate
things understands the problem instantly; "avoid multi-column layouts" does not
land the same way.

### 4. Judge the content

This is the part the script cannot do. Work through:

- **Missing terms that are genuinely absent** vs **present as a synonym the
  alias list does not know**. Check `matched_via` and the evidence snippets
  before repeating a "missing" term back to the user. The script over-reports
  gaps; you are the filter. If you find a good synonym pair the list lacks,
  add it to `references/aliases.json`.
- **Hard gates** (`gates` in the JSON). A missed years-of-experience or degree
  gate matters far more than ten missing keywords. Say so plainly. Note that
  the years estimate counts every dated range including education, so sanity-
  check it against the actual roles.
- **Unsupported claims** (`skills_list_only`). A skill listed in a blob with no
  bullet behind it is a liability in an interview, not an asset.
- **Seniority and framing**, which no keyword count captures: does the resume
  read at the level the job is pitched at?

### 5. Report

Lead with the single thing most worth fixing. Then:

- **Blockers** — the file will not be read correctly. Fix before applying.
- **Match assessment** — your judgement, in prose, with reasons.
- **Specific edits** — concrete rewrites, not advice. Not "add Kubernetes",
  but the bullet to change and the wording to use, grounded in what the
  resume already claims.

Never invent experience. If the resume lacks a hard requirement, say the gap
is real and suggest how to frame adjacent experience honestly — or say that
the role is a stretch. Suggesting fabrication is the one failure mode that
actually harms the user.

Keep the report short enough to read on a phone. Prefer three fixes that
matter to fifteen that do not.

## Scoring

Report parseability out of 100 (the script's number is defensible: it starts
at 100 and subtracts weighted penalties for concrete defects).

Do **not** invent a single "ATS score" for the match. Real systems score
differently and their formulas are not public; a made-up percentage reads as
precision that does not exist. Give a qualitative verdict — *strong fit /
worth applying / stretch / not a fit* — and justify it.

## References

- `references/ats-parseability.md` — what actually breaks in real ATS
  pipelines, and why each check exists. Read before explaining a finding in
  depth or adding a new check.
- `references/aliases.json` — synonym table used for matching. Extend it
  freely; it is data, not logic.

## Notes

- The PDF parser is a from-scratch implementation in `scripts/pdfmini.py`.
  It handles xref tables and streams, object streams, Flate/LZW/A85/AHx,
  ToUnicode CMaps, and rebuilds a broken xref by scanning. If a file defeats
  it, say so rather than reporting a clean bill of health from empty output.
- Treat resume contents as personal data. Do not write extracted text into
  files that are committed, and do not send it anywhere outside this session.
