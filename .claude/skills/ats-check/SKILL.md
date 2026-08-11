---
name: ats-check
description: Validate ATS CV deliverables against a canonical structure, and check any resume against an Applicant Tracking System. Use before archiving or submitting a tailored CV, to verify a single-column ATS .docx and the PDF generated from it are bulletproof, to run the machine-parse round trip between them, or when asked whether a resume will pass ATS screening, how it scores against a job description, or what keywords it is missing. Runs offline, no API key, no browser.
---

# ATS check

Two modes. Pick by what you are given.

| You have | Mode | Command |
|---|---|---|
| An ATS `.docx` built to the canonical structure | **Validate** — the pre-archive gate | `ats_validate.py` |
| Any resume, or a resume plus a job ad | **Review** — parseability and JD fit | `ats_check.py` |

Both are standard library only: no install, no network, no API key.

---

## Mode 1 — Validate (the gate)

This runs **before a tailored CV is archived or submitted**. It is the
"bulletproof check" step: nothing gets archived until it passes.

```bash
# an archive folder — resolves the deliverables from its name
python3 .claude/skills/ats-check/scripts/ats_validate.py "…/YYYYMMDD_Owner_Title" --jd jd.txt --json

# the newest folder in the local Resume archive
python3 .claude/skills/ats-check/scripts/ats_validate.py --latest --jd jd.txt

# or the files directly
python3 .claude/skills/ats-check/scripts/ats_validate.py CV.docx --pdf CV.pdf --jd jd.txt
```

### Working from the local Resume archive

The archive keeps one folder per tailored CV, three files inside, all named
after the folder:

```
YYYYMMDD_Giuseppe LOPES_[Title]/
  YYYYMMDD_Giuseppe LOPES_[Title].docx          ← ATS docx      (validated)
  YYYYMMDD_Giuseppe LOPES_[Title].pdf           ← ATS PDF       (validated)
  YYYYMMDD_Giuseppe LOPES_[Title]_Design.pdf    ← Canva export  (presence only)
```

Pass the **folder** and the two ATS files are found automatically; the design
PDF is checked for presence and never validated as the ATS PDF. Three extra
checks come with it: `archive.folder_name`, `archive.files`,
`archive.extra_files`.

The archive root is `archive.root` in the spec; override with `--archive-root`.
**It lives on the Mac.** In a sandbox — the phone, claude.ai, a cloud session —
that path does not exist, and `--latest` exits 2 saying so rather than
pretending. There, pass the files directly or have them attached.

**Scope is the two ATS deliverables only** — the rebuilt single-column `.docx`
and the PDF generated from it. The Canva design export has its own Design QA
gate and is never passed to this tool: it is multi-column by construction, so
comparing it against a single-column ATS build would only produce noise.

Each argument unlocks a check, and **a check that cannot run is reported as
`skip`, never as a pass** — so a validation run with only the docx is not a
green light for the PDF.

| Argument | What it verifies |
|---|---|
| a `.docx`, or an archive folder | the canonical structure: page setup, single column, Calibri throughout, point sizes, real numbering definitions rather than typed bullets, section order and per-section content rules, metadata |
| `--pdf` | round trip — the PDF's text matches the docx block for block, proving it was generated from that docx and dropped nothing; that it is genuinely single column; and full PDF parseability |
| `--jd` | the JD's required terms and hard gates |
| `--latest` | validate the newest dated folder in the archive |

Exit status: `0` everything passed, `1` something failed, `2` could not run.

### Reading the result

The target is **100/100 with zero failures**. Report the score, then the
failing checks with their `id` and fix. Do not soften a failure into a
suggestion — this is a gate.

Three failures deserve more than their one-line fix:

- **`parity.docx_pdf`** names the exact blocks present in the docx and absent
  from the PDF. Quote them verbatim. It failing means the PDF was not generated
  from this docx, or something was lost on export — regenerate before
  investigating anything else.
- **`pdf.single_column`** failing almost always means the Canva design export
  was submitted as the ATS PDF by mistake. The ATS PDF must come from the ATS
  docx.
- **`bullets.real`** means bullets were typed as characters instead of applied
  as Word list formatting. It looks identical on screen and parses completely
  differently.

If a check fails because the *spec* is wrong rather than the document,
say so plainly rather than making the CV wrong to satisfy it. The spec lives
in `references/canonical_spec.json` and is meant to be edited.

### Personal values

`canonical_spec.json` checks the **shape** of the contact line
(`City, Country | +phone | email`) and the author name, not literal values,
because this repository is public. To pin exact strings, copy
`references/spec.local.example.json` to `references/spec.local.json` — it is
gitignored and picked up automatically.

---

## Mode 2 — Review (any resume)

```bash
python3 .claude/skills/ats-check/scripts/ats_check.py RESUME --jd JD.txt --json
```

Answers two questions in order:

1. **Can the machine read the file at all?** Text layer, fonts with no
   character map, multi-column layouts, contact details stranded in a header
   or a `mailto:` annotation, dates, hidden text. Measured, and scored out of 100.
2. **Does the content match the job?** Requirement terms weighted by JD
   section, alias-aware (`K8s` satisfies `Kubernetes`), hard gates, and skills
   claimed in a list with nothing behind them.

Useful flags: `--text-only` prints exactly what an ATS extracts; add
`--stream-order` for the raw content-stream read. On a two-column resume the
two differ dramatically, and quoting that difference is the most convincing
thing you can show.

**The script reports evidence; you report the verdict.** Never repeat the
coverage percentage as a score of the candidate — it is lexical overlap.
Check `matched_via` and the evidence snippets before echoing a "missing" term:
the matcher over-reports gaps and you are the filter. A missed hard gate
matters more than ten missing keywords.

Do not invent a single "ATS score" for job fit. Real systems score
differently and most compute no percentage at all. Give a qualitative
verdict — *strong fit / worth applying / stretch / not a fit* — with reasons.

---

## Both modes

- **Read the extracted text yourself** (`extracted_text` in the JSON). If it
  reads as nonsense to you, that is the finding, whatever the score says.
- **Never invent experience.** If a requirement is genuinely unmet, say the gap
  is real and suggest honest framing of adjacent experience, or say the role is
  a stretch. Keep every figure exactly as the source has it.
- **Report; do not file.** Hand the result back. Write it to a vault, an
  archive or anywhere else only when asked, and follow whatever convention that
  destination already uses — read a neighbouring file first rather than
  inventing frontmatter or a filename scheme.
- Treat resume contents as personal data: no copying into the repository, no
  sending anywhere outside the session.

## References

- `references/canonical_spec.json` — the structure Mode 1 enforces. Editable data.
- `references/ats-parseability.md` — why each parseability check exists.
- `references/aliases.json` — synonym table for matching. Extend freely.
