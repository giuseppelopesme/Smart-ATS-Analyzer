# What actually breaks in an ATS

Background for explaining findings properly. Every check in `ats_lint.py`
exists because of something in here.

## How an ATS reads a resume

The pipeline is duller than people assume:

1. **Extract text.** For PDF, replay the content stream and collect the show-text
   operators. For DOCX, walk `word/document.xml`.
2. **Segment.** Split the text into sections by matching heading words
   (`EXPERIENCE`, `EDUCATION`, `SKILLS`).
3. **Parse fields.** Regex out email, phone, dates, employers, titles.
4. **Index and score.** Match the recruiter's search terms against the text,
   or against the structured fields.

Nothing in that pipeline looks at the page. It never renders the PDF. This is
the root of almost every failure below: **the resume a human sees and the
resume the machine sees are two different documents.**

## Failure modes, worst first

### The text layer is missing

A scan, a photo, or a "print to image" export has no text at all. The parser
gets an empty document and the application is dead on arrival. Some vendors
run OCR; most do not, and OCR on a two-column design fails again at step 1.

*Detected by:* word count against image coverage.

### Fonts with no character map

A PDF stores glyph indices, not letters. The `/ToUnicode` CMap is what maps
them back. Subsetted fonts from some design tools (and LaTeX with certain
setups) ship without one, or with a broken one. The page looks perfect;
copy-pasting from it yields `` or nothing.

This is the cruellest failure because it is invisible without testing. The
check: select all text in a viewer, paste into a plain text editor.

*Detected by:* proportion of glyphs that decode to `U+FFFD`, plus Type0 fonts
with no ToUnicode.

### Multi-column layouts

The single most common self-inflicted wound, because the templates that sell
best are two-column.

Text is emitted in content-stream order, which for a two-column page is
usually left-cell, right-cell, left-cell, right-cell — row by row, not column
by column. Reading it back linearly produces:

```
EXPERIENCE          SKILLS
Senior Engineer     Python
Acme 2020-2024      Kubernetes
```
→ `EXPERIENCE SKILLS Senior Engineer Python Acme 2020-2024 Kubernetes`

Job titles fuse with skills. Dates attach to the wrong employer. A recruiter
searching "Senior Engineer" may still hit, but the structured profile is
scrambled, and tenure-based filters produce nonsense.

Better parsers do column detection. You cannot know which one is on the other
end. That uncertainty is itself the argument for one column.

*Detected by:* whitespace gutters with text on both sides across many rows,
plus divergence between stream-order and column-aware extraction.

### Contact details the parser cannot reach

Three variants, all common:

- **In the page header/footer band.** Some parsers strip running headers before
  analysis, on the theory that they are page furniture.
- **In a Word header/footer.** `word/header1.xml` is a separate part from
  `word/document.xml`. A parser that only opens the document body never sees it.
  This one is near-total: the contact block simply does not exist.
- **Only as a hyperlink.** `mailto:` in a link annotation with the visible text
  being an icon or the word "Email". Annotations are not text.

An ATS that finds no email either rejects the application or files a record
nobody can contact.

### Text boxes and layout tables (DOCX)

Text box content lives in `txbxContent`, outside the main flow — frequently
skipped entirely. Tables are read cell by cell, and a two-column layout table
flattens the same way a two-column PDF does.

### Non-standard section headings

Segmentation matches known heading words. "Where I've Worked" is charming and
unparseable; the section may be dropped from the structured profile even
though the text was extracted fine. Use `Work Experience`, `Education`,
`Skills` — literally.

Match the language of the posting. An Italian-headed CV against an
English-language ATS config segments badly.

### Unparseable dates

`Mar 2021 – Present` parses. `Spring '21 → now` does not. Without dates the
system cannot compute tenure or total years, and "5+ years experience" filters
drop the candidate silently.

Watch for en-dashes and em-dashes: they are fine, but a dash rendered as an
icon-font glyph is not.

### Icon fonts and private-use characters

Font Awesome and similar map icons into the Unicode private use area. They
extract as `U+F0E0`-style noise, which can weld itself to the adjacent word —
turning an email address into an unmatchable token.

### Hidden text and keyword stuffing

White-on-white text, zero-size text, or invisible render mode (`Tr 3`) to
smuggle keywords past the filter. It is extracted, so it "works" on the naive
filter — and then a human reads the extracted text and sees the stuffing.
Vendors detect it. Treat it as disqualifying and say so.

Note the ambiguity: invisible text is *also* what an OCR layer looks like. If
the visible page is an image with an invisible text layer, the finding is
"this is a scan", not "you cheated".

### Encrypted PDFs

An `/Encrypt` dictionary — even with an empty owner password set only to
restrict printing — makes some parsers refuse the file outright.

## Things that do not matter as much as people think

- **File size**, within reason.
- **PDF vs DOCX.** Both parse fine when well-formed. PDF is safer for layout
  fidelity; DOCX is safer against exotic font problems. Follow the posting's
  instruction if it gives one.
- **Exact keyword density.** Modern systems rank; they do not count. Stuffing
  to hit a percentage is counterproductive.
- **Fancy fonts**, as long as they are embedded with a correct ToUnicode map.
- **Colour and rules**, as long as text is text.
- **The "6-second rule" and similar recruiter folklore.** Not an ATS property.

## On scores

There is no single ATS score. Workday, Greenhouse, Lever, Taleo and iCIMS each
model candidates differently, and most do not compute a match percentage at
all — recruiters run boolean searches over indexed text.

Any tool showing "your ATS score is 72%" invented that number. Parseability
can be measured honestly; fit cannot be reduced to a percentage. Say what is
broken and what is missing, and let the judgement be a judgement.
