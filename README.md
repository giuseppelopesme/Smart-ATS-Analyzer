# ATS validator + checker — a Claude Skill

Two things, one skill, no browser and no API key:

1. **Validate** an ATS CV deliverable against a canonical structure — the gate
   that runs before a tailored CV is archived or submitted.
2. **Review** any resume for parseability and job-description fit.

Everything is **standard library only** — no `pip install`, no network — so the
scripts run unchanged in a sandbox, on a Mac, or in CI.

## Mode 1 — Validate (the gate)

```bash
# an archive folder — the deliverables resolve from its name
python3 .claude/skills/ats-check/scripts/ats_validate.py "…/20260811_Giuseppe LOPES_Campari Global AI Director"

# the newest folder in the local Resume archive
python3 .claude/skills/ats-check/scripts/ats_validate.py --latest --jd jd.txt

# or the files directly
python3 .claude/skills/ats-check/scripts/ats_validate.py CV.docx --pdf CV.pdf --jd jd.txt
```

Given a folder, the ATS docx and PDF are found by naming convention and the
design PDF is checked for presence only — never validated as the ATS PDF. The
archive root is configured in the spec and overridable with `--archive-root`;
where it is not mounted, `--latest` exits 2 and says so instead of pretending.

Scope of validation is the two ATS deliverables — the rebuilt single-column `.docx` and the
PDF generated from it. The Canva design export has its own Design QA gate and
is not inspected here.

```
# ATS validation — CV.docx

**PASS · 100/100** · 42 passed, 0 failed, 0 skipped · 738 words
```

Up to 45 checks against `references/canonical_spec.json`:

| Group | Checks |
|---|---|
| Page setup | A4, margins, single column |
| Forbidden | tables, text boxes, images, header text, footer text |
| Bullets | real Word numbering definitions, not typed glyphs; one level only |
| Typography | Calibri throughout; name 18pt bold; headline 12pt gray; headings 12pt bold caps with a paragraph bottom rule; job titles 11pt bold; company/date 10pt italic; body 10pt |
| Structure | all canonical sections, in order; 3 summary paragraphs; 3 pipe-separated competency lines; 4 quantified achievements; 4–5 bullets per role; reverse chronological; `Title, Company (YYYY - YYYY)` line shapes |
| Metadata | author set; title in `Name - Role - CV` form |
| Round trip | every block of the docx has a counterpart in the PDF |
| ATS PDF | genuinely single column, ≤ 2 pages, no parseability blockers |
| Keywords | JD required terms and hard gates |
| Archive | folder named `YYYYMMDD_Owner_Title`, all three files present and named after it, nothing stray |

**A check that cannot run reports `skip`, never `pass`.** Validating with only
the docx is not a green light for the PDF.

Exit status `0` / `1` / `2` — pass / fail / could not run, so it drops into a
script or a pre-archive hook.

### Two checks worth explaining

**The round trip is block-level, not a word-count ratio.** A ratio is too
blunt: an entire bullet can vanish from a 750-word CV and still leave ~98% of
the words, which sails past any sane threshold. Each docx block is matched
against its best counterpart anywhere in the PDF, so a dropped bullet is named
exactly:

```
❌ ATS PDF text matches the docx   parity.docx_pdf
2 block(s) of the docx have no counterpart in the PDF:
"Chaired the internal AI governance board covering model risk, validation and the";
"required for supervisory review."
```

Matching tolerates line re-wrapping and reordering, so it reports content that
is genuinely gone rather than cosmetic layout differences.

**Bullets.** Typed `•` and a real numbering definition look identical on screen
and parse completely differently. The validator reads `w:numPr`, so it can tell.

### Personal values stay out of git

The committed spec checks the *shape* of the contact line
(`City, Country | +phone | email`) and that an author is set — not literal
values, because this repo is public. Copy
`references/spec.local.example.json` → `spec.local.json` (gitignored) to pin
exact strings; it is merged automatically.

## Mode 2 — Review (any resume)

```bash
python3 .claude/skills/ats-check/scripts/ats_check.py resume.pdf --jd jd.txt
python3 .claude/skills/ats-check/scripts/ats_check.py resume.pdf --text-only
```

Parseability first — a resume that extracts as garbage scores zero against every
job:

- scans and image-only pages
- fonts with no `ToUnicode` map: the page looks perfect and extracts as `���`
- multi-column layouts, detected geometrically
- contact details stranded in a Word header, a PDF header band, or a `mailto:`
  annotation
- DOCX text boxes and layout tables, non-standard headings, unparseable dates
- hidden text and keyword stuffing

Then JD fit: requirement terms weighted by section, alias-aware (`K8s` satisfies
`Kubernetes`), hard gates, and skills listed with nothing behind them.

`--text-only --stream-order` shows the raw content-stream read. On a two-column
resume it differs dramatically from the layout-aware read, which is the clearest
possible demonstration of why one column matters:

```
EXPERIENCE SKILLS
Senior Backend Engineer Python
Acme Corp 2020-2024 Kubernetes
```

## Use it from your phone

```bash
python3 tools/package_skill.py     # -> dist/ats-check.zip
```

Upload at **claude.ai → Settings → Capabilities → Skills**, enable code
execution. Then attach a CV in the Claude iOS app and ask for a check — it runs
server-side, no browser, no laptop.

The packager refuses to build if `SKILL.md` frontmatter is malformed, a script
fails to compile, or anything imports a third-party module.

## Layout

```
.claude/skills/ats-check/
  SKILL.md
  scripts/
    pdfmini.py          dependency-free PDF parser
    layout.py           line grouping, column detection, reading order
    readers.py          PDF / DOCX / TXT / MD / RTF -> one shape
    docx_inspect.py     DOCX formatting: fonts, sizes, numbering, borders, sectPr
    validate.py         canonical-structure checks + text comparison
    ats_lint.py         parseability findings
    jd_match.py         JD overlap and hard gates
    ats_validate.py     CLI — the gate
    ats_check.py        CLI — the review
  references/
    canonical_spec.json      the structure the gate enforces (editable data)
    spec.local.example.json  template for pinning personal values
    ats-parseability.md      why each parseability check exists
    aliases.json             synonym table
tests/test_ats.py       107 tests, no sample files on disk
tools/package_skill.py  build + validate the uploadable zip
```

### Why hand-written parsers

`pypdf` and `python-docx` are not installable in a sandbox with no network. So
`pdfmini.py` implements what is needed directly — xref tables and xref streams,
object streams, Flate/LZW/ASCII85/ASCIIHex with PNG predictors, `ToUnicode`
CMaps, form XObject recursion, and xref reconstruction by scanning — and
`docx_inspect.py` resolves run properties the way Word does: direct `rPr`, then
the paragraph style up its `basedOn` chain, then `docDefaults`. Reading only
direct properties reports "no font set" on a document that renders perfectly.

`pdfmini` also exposes something general-purpose libraries hide: the **position
of every show-text operation**. Column detection and the naive-vs-layout diff
are built on that.

## Tests

```bash
python3 tests/test_ats.py     # 107 passed, 0 failed
```

Every PDF and DOCX is constructed byte by byte in `tests/fixtures.py` and
`tests/docx_fixtures.py`, so the suite needs no sample files and can exercise
structures a normal generator will not emit: CID fonts with no `ToUnicode`,
corrupted xref offsets, image-only pages, and a deliberately broken CV that
trips 18 named checks at once.

## Privacy

No resume, job ad or report is stored in this repository — it holds the analyser
only. Documents stay wherever your own workflow keeps them, and the analysis
runs locally in the session.

## Credit

The original [Smart ATS Analyzer](https://github.com/Anubhx/Smart-ATS-Analyzer)
by Anubhav Raj (Streamlit + Gemini Pro) is the idea this started from. No code
is shared with it.
