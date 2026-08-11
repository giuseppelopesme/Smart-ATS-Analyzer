"""Validate an ATS deliverable against the canonical structure.

This is the gate that runs *before* a tailored CV is archived or submitted:
the ATS docx must follow the canonical structure exactly, the ATS PDF must be
the same document rendered, and both must survive a machine parse.

Three independent things get checked, and they fail for different reasons:

* **Structure** -- the docx against ``canonical_spec.json``: page setup, fonts,
  point sizes, real numbering definitions instead of typed bullets, section
  order, per-section content rules, metadata.
* **Round trip** -- text extracted from the docx and from the PDF must agree.
  If they do not, the PDF was not generated from this docx, or something was
  dropped on the way out.
* **Content parity** -- optionally, against the Canva design export, to enforce
  the rule that the ATS build carries 100% of the tailored twin's text.

A check that cannot be performed is reported as ``skipped``, never as a pass.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from docx_inspect import DocxModel, Paragraph

__all__ = ["Check", "ValidationReport", "load_spec", "validate_structure",
           "compare_text", "validate_all"]

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC_PATH = os.path.join(_HERE, "..", "references", "canonical_spec.json")

MONTHS = ("january february march april may june july august september october "
          "november december jan feb mar apr jun jul aug sep sept oct nov dec").split()


@dataclass
class Check:
    id: str
    ok: Optional[bool]  # True pass, False fail, None skipped
    title: str
    detail: str = ""
    fix: str = ""
    severity: str = "high"  # blocker | high | medium | low
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return {True: "pass", False: "fail", None: "skip"}[self.ok]


@dataclass
class ValidationReport:
    checks: List[Check] = field(default_factory=list)
    score: int = 0
    docx_text: str = ""
    pdf_text: str = ""

    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if c.ok is False]

    @property
    def skipped(self) -> List[Check]:
        return [c for c in self.checks if c.ok is None]

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "passed": self.passed,
            "counts": {
                "pass": sum(1 for c in self.checks if c.ok is True),
                "fail": len(self.failures),
                "skip": len(self.skipped),
            },
            "checks": [{**asdict(c), "status": c.status} for c in self.checks],
        }


PENALTY = {"blocker": 40, "high": 12, "medium": 5, "low": 2}


def load_spec(path: Optional[str] = None, local: Optional[str] = None) -> Dict[str, Any]:
    with open(path or _SPEC_PATH, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    if local and os.path.exists(local):
        with open(local, "r", encoding="utf-8") as fh:
            overlay = json.load(fh)
        spec = _deep_merge(spec, overlay)
    return spec


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------
# Section segmentation
# --------------------------------------------------------------------------


@dataclass
class DocSection:
    name: str
    heading: Optional[Paragraph]
    paragraphs: List[Paragraph] = field(default_factory=list)

    @property
    def bullets(self) -> List[Paragraph]:
        return [p for p in self.paragraphs if p.numbered or p.typed_bullet]

    @property
    def prose(self) -> List[Paragraph]:
        return [p for p in self.paragraphs
                if p.text.strip() and not (p.numbered or p.typed_bullet)]


def _norm_heading(text: str) -> str:
    return re.sub(r"[^A-Z ]", "", text.upper()).strip()


def segment(model: DocxModel, spec: Dict[str, Any]) -> Tuple[List[Paragraph], List[DocSection]]:
    """Split into the pre-heading header block and the named sections."""
    wanted = {_norm_heading(s) for s in spec["sections"]}
    header_block: List[Paragraph] = []
    sections: List[DocSection] = []
    current: Optional[DocSection] = None

    for p in model.paragraphs:
        if not p.text.strip():
            continue
        key = _norm_heading(p.text)
        # A heading is short, matches a known name, and is not a bullet.
        if key in wanted and not p.numbered and len(p.text.strip()) <= 40:
            current = DocSection(name=key, heading=p)
            sections.append(current)
            continue
        if current is None:
            header_block.append(p)
        else:
            current.paragraphs.append(p)
    return header_block, sections


# --------------------------------------------------------------------------
# Structure validation
# --------------------------------------------------------------------------


def validate_structure(model: DocxModel, spec: Dict[str, Any]) -> List[Check]:
    checks: List[Check] = []
    add = checks.append
    page = spec["page"]
    elements = spec["elements"]
    patterns = spec["patterns"]
    expected = spec.get("expected", {})

    # ---- page setup ----------------------------------------------------
    if model.sections:
        sect = model.sections[0]
        add(Check("page.size", sect.is_a4, "Page size is A4",
                  f"{sect.page_w_twips}x{sect.page_h_twips} twips",
                  "Set page size to A4 in Layout > Size.",
                  evidence={"w": sect.page_w_twips, "h": sect.page_h_twips}))

        tol = page["margin_tolerance_cm"]
        got = sect.margins_cm()
        want = page["margins_cm"]
        bad = {k: got[k] for k in want if abs(got[k] - want[k]) > tol}
        add(Check("page.margins", not bad, "Margins match the canonical setup",
                  f"got {got} cm, want {want} cm (±{tol})" if bad else f"{got} cm",
                  "Set margins in Layout > Margins > Custom.",
                  severity="medium", evidence={"got": got, "want": want, "off": bad}))

        add(Check("page.columns", sect.cols == page["columns"],
                  "Single column throughout", f"{sect.cols} column(s)",
                  "Layout > Columns > One.", severity="blocker",
                  evidence={"cols": sect.cols}))
    else:
        for cid, title in (("page.size", "Page size is A4"),
                           ("page.margins", "Margins match the canonical setup"),
                           ("page.columns", "Single column throughout")):
            add(Check(cid, None, title, "no section properties found in the document"))

    # ---- forbidden constructs -------------------------------------------
    f = spec["forbidden"]
    if f.get("tables"):
        add(Check("no.tables", model.tables == 0, "No tables",
                  f"{model.tables} table(s) found",
                  "Rebuild those rows as ordinary paragraphs with tab stops.",
                  severity="blocker", evidence={"count": model.tables}))
    if f.get("textboxes"):
        add(Check("no.textboxes", model.textboxes == 0, "No text boxes",
                  f"{model.textboxes} text box(es) found",
                  "Move the content into the main body flow.",
                  severity="blocker", evidence={"count": model.textboxes}))
    if f.get("images"):
        add(Check("no.images", model.images == 0, "No images or icons",
                  f"{model.images} drawing/picture object(s) found",
                  "Delete images; express the content as text.",
                  severity="blocker", evidence={"count": model.images}))
    if f.get("header_text"):
        hdr = [t for t in model.header_texts if t.strip()]
        add(Check("no.header", not hdr, "Header is empty",
                  f"header text: {hdr[:1]}" if hdr else "empty",
                  "Clear the header; contact details belong in the body.",
                  severity="blocker", evidence={"texts": hdr[:3]}))
    if f.get("footer_text"):
        ftr = [t for t in model.footer_texts if t.strip()]
        add(Check("no.footer", not ftr, "Footer is empty",
                  f"footer text: {ftr[:1]}" if ftr else "empty",
                  "Clear the footer. The Canva ID label belongs on the design "
                  "export only, never on the ATS build.",
                  severity="blocker", evidence={"texts": ftr[:3]}))

    # ---- bullets ---------------------------------------------------------
    typed = [p for p in model.paragraphs if p.typed_bullet]
    add(Check("bullets.real", not typed, "Bullets are real list formatting",
              f"{len(typed)} paragraph(s) start with a typed glyph"
              if typed else "all bullets use numbering definitions",
              "Delete the typed character and apply Word's bullet list style.",
              severity="high",
              evidence={"examples": [p.text[:70] for p in typed[:5]]}))

    nested = [p for p in model.paragraphs if p.numbered and p.ilvl > 0]
    add(Check("bullets.one_level", not nested, "List is one level only",
              f"{len(nested)} paragraph(s) at indent level > 0" if nested else "single level",
              "Promote nested bullets to the top level.",
              severity="medium",
              evidence={"examples": [p.text[:70] for p in nested[:5]]}))

    # ---- fonts ------------------------------------------------------------
    want_font = spec["font_family"]
    used = {f for f in model.fonts_used if f}
    stray = {f for f in used if f.lower() != want_font.lower()}
    add(Check("font.family", not stray and bool(used),
              f"All text is {want_font}",
              f"other fonts present: {sorted(stray)}" if stray
              else (f"{want_font} throughout" if used else "no font information found"),
              f"Select all and set the font to {want_font}. Pasted text is the usual source.",
              severity="high", evidence={"fonts": sorted(used)}))

    header_block, sections = segment(model, spec)

    # ---- header block ------------------------------------------------------
    if header_block:
        name_p = header_block[0]
        want = elements["name"]
        ok = abs(name_p.size_pt - want["size_pt"]) < 0.6 and name_p.bold
        add(Check("header.name", ok, f"Name is {want['size_pt']}pt bold",
                  f"'{name_p.text[:40]}' at {name_p.size_pt}pt, bold={name_p.bold}",
                  f"Set the name line to {want['size_pt']}pt bold.",
                  evidence={"text": name_p.text[:60], "size": name_p.size_pt}))

        if expected.get("name"):
            add(Check("header.name_value", name_p.text.strip() == expected["name"],
                      "Name matches the expected value",
                      f"got '{name_p.text.strip()}'", severity="medium"))

        if len(header_block) > 1:
            hl = header_block[1]
            want = elements["headline"]
            size_ok = abs(hl.size_pt - want["size_pt"]) < 0.6
            gray_ok = any(r.is_gray for r in hl.runs if r.text.strip())
            add(Check("header.headline", size_ok and gray_ok,
                      f"Headline is {want['size_pt']}pt light gray",
                      f"'{hl.text[:40]}' at {hl.size_pt}pt, gray={gray_ok}",
                      "Set the target job title under the name to "
                      f"{want['size_pt']}pt in a light gray.",
                      severity="medium",
                      evidence={"text": hl.text[:60], "size": hl.size_pt,
                                "colors": [r.color for r in hl.runs if r.color][:3]}))
        else:
            add(Check("header.headline", False, "Headline is present",
                      "no paragraph found between the name and the contact line",
                      "Add the target job title directly under the name."))

        contact = next((p for p in header_block
                        if re.search(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", p.text)), None)
        if contact is None:
            add(Check("header.contact", False, "Contact line present and well formed",
                      "no line containing an email address found in the header block",
                      "Add 'City, Country | +phone | email' under the headline.",
                      severity="blocker"))
        else:
            shape_ok = bool(re.match(patterns["contact_line"], contact.text.strip()))
            size_ok = abs(contact.size_pt - elements["contact"]["size_pt"]) < 0.6
            exp = expected.get("contact_line")
            value_ok = (contact.text.strip() == exp) if exp else True
            add(Check("header.contact", shape_ok and size_ok and value_ok,
                      "Contact line present and well formed",
                      f"'{contact.text.strip()[:70]}' at {contact.size_pt}pt"
                      + ("" if shape_ok else " — expected 'City, Country | +phone | email'")
                      + ("" if value_ok else " — does not match the pinned value"),
                      "Format as 'City, Country | +phone | email' at "
                      f"{elements['contact']['size_pt']}pt.",
                      severity="high",
                      evidence={"text": contact.text.strip()[:90], "size": contact.size_pt}))

        li = next((p for p in header_block
                   if re.search(patterns["linkedin_line"], p.text, re.I)), None)
        if li is None:
            add(Check("header.linkedin", False, "LinkedIn line present as a live hyperlink",
                      "no linkedin.com line found",
                      "Add the LinkedIn URL as a real hyperlink.", severity="medium"))
        else:
            linked = any(r.hyperlink for r in li.runs)
            size_ok = abs(li.size_pt - elements["linkedin"]["size_pt"]) < 0.6
            add(Check("header.linkedin", linked and size_ok,
                      "LinkedIn line present as a live hyperlink",
                      f"hyperlink={'yes' if linked else 'no'}, {li.size_pt}pt",
                      "Insert > Link so the URL is a real hyperlink, at "
                      f"{elements['linkedin']['size_pt']}pt.",
                      severity="medium", evidence={"text": li.text[:80]}))
    else:
        add(Check("header.block", False, "Header block present",
                  "no paragraphs before the first section heading",
                  "Add Name / Headline / Contact / LinkedIn above the first section.",
                  severity="blocker"))

    # ---- section order ------------------------------------------------------
    want_order = [_norm_heading(s) for s in spec["sections"]]
    got_order = [s.name for s in sections]
    missing = [s for s in want_order if s not in got_order]
    extra = [s for s in got_order if s not in want_order]
    in_order = [s for s in got_order if s in want_order] == [s for s in want_order if s in got_order]

    add(Check("sections.present", not missing, "All canonical sections present",
              f"missing: {missing}" if missing else "all present",
              "Add the missing section headings.", severity="high",
              evidence={"missing": missing, "got": got_order}))
    add(Check("sections.order", in_order and not extra, "Sections in the canonical order",
              (f"unexpected: {extra}. " if extra else "") +
              (f"got {got_order}" if not in_order else "correct order"),
              "Reorder to: " + " → ".join(want_order), severity="medium",
              evidence={"got": got_order, "want": want_order}))

    # ---- heading formatting --------------------------------------------------
    want = elements["section_heading"]
    bad_headings = []
    for s in sections:
        h = s.heading
        if h is None:
            continue
        problems = []
        if abs(h.size_pt - want["size_pt"]) >= 0.6:
            problems.append(f"{h.size_pt}pt")
        if want.get("bold") and not h.bold:
            problems.append("not bold")
        if want.get("all_caps") and not h.is_all_caps:
            problems.append("not all caps")
        if want.get("bottom_border") and not h.has_bottom_border:
            problems.append("no bottom rule")
        if problems:
            bad_headings.append(f"{s.name}: {', '.join(problems)}")
    add(Check("sections.formatting", not bad_headings,
              f"Section headings are {want['size_pt']}pt bold caps with a bottom rule",
              "; ".join(bad_headings) if bad_headings else "all correct",
              "Apply a paragraph bottom border (Borders > Bottom Border), not a table "
              "and not an underscore row.",
              severity="medium", evidence={"problems": bad_headings}))

    by_name = {s.name: s for s in sections}
    rules = spec["section_rules"]

    # ---- per-section content -------------------------------------------------
    _check_summary(add, by_name, rules)
    _check_pipe_section(add, by_name, rules, "CORE COMPETENCIES")
    _check_pipe_section(add, by_name, rules, "TECHNOLOGY")
    _check_pipe_section(add, by_name, rules, "LANGUAGES")
    _check_achievements(add, by_name, rules, patterns)
    _check_experience(add, by_name, rules, patterns, elements)
    _check_line_pattern(add, by_name, rules, "EARLIER CAREER")
    _check_line_pattern(add, by_name, rules, "EDUCATION")

    # ---- body size -------------------------------------------------------------
    body_want = elements["body"]["size_pt"]
    body_paras = [p for s in sections for p in s.paragraphs
                  if p.text.strip() and p.size_pt]
    off = [p for p in body_paras
           if p.size_pt not in (body_want, elements["job_title"]["size_pt"],
                                elements["section_heading"]["size_pt"])
           and abs(p.size_pt - body_want) >= 0.6]
    add(Check("body.size", not off, f"Body text is {body_want}pt",
              f"{len(off)} paragraph(s) at other sizes" if off else f"all {body_want}pt",
              f"Set body and bullet text to {body_want}pt.", severity="medium",
              evidence={"examples": [f"{p.size_pt}pt: {p.text[:50]}" for p in off[:5]]}))

    # ---- length ------------------------------------------------------------------
    words = model.word_count
    lo, hi = spec["page"]["target_words"]
    add(Check("length.words", lo <= words <= hi, f"Length is {lo}-{hi} words",
              f"{words} words", "Trim or expand to about 1,000 words.",
              severity="low", evidence={"words": words}))

    pages_meta = model.metadata.get("pages")
    if pages_meta:
        try:
            n = int(pages_meta)
            add(Check("length.pages", n <= spec["page"]["max_pages"],
                      f"At most {spec['page']['max_pages']} pages", f"{n} pages",
                      "Trim to two pages.", severity="medium", evidence={"pages": n}))
        except ValueError:
            pass
    else:
        add(Check("length.pages", None, f"At most {spec['page']['max_pages']} pages",
                  "page count not recorded in docProps/app.xml (normal for a "
                  "programmatically built file) — confirm in Word or via the PDF",
                  severity="low"))

    # ---- metadata --------------------------------------------------------------
    meta = model.metadata
    want_author = spec["metadata"].get("author")
    author = meta.get("author", "")
    if want_author:
        add(Check("meta.author", author == want_author, "Document author is set correctly",
                  f"got '{author}'", f"Set File > Info > Author to '{want_author}'.",
                  severity="medium"))
    else:
        add(Check("meta.author", bool(author.strip()), "Document author is set",
                  f"got '{author}'" if author else "author is empty",
                  "Set the document author in File > Info.", severity="medium"))

    title = meta.get("title", "")
    pat = spec["metadata"].get("title_pattern")
    add(Check("meta.title", bool(title and pat and re.match(pat, title)),
              "Document title follows 'Name - Role - CV'",
              f"got '{title}'" if title else "title is empty",
              "Set File > Info > Title to 'Giuseppe Lopes - [Target Role] - CV'.",
              severity="medium", evidence={"title": title}))

    return checks


def _check_summary(add, by_name, rules) -> None:
    rule = rules.get("PROFESSIONAL SUMMARY", {})
    s = by_name.get("PROFESSIONAL SUMMARY")
    if s is None:
        add(Check("summary.shape", None, "Summary is 3 paragraphs, no bullets",
                  "section not found"))
        return
    n_para, n_bullet = len(s.prose), len(s.bullets)
    want_p = rule.get("paragraphs", 3)
    ok = n_para == want_p and n_bullet == rule.get("bullets", 0)
    add(Check("summary.shape", ok, f"Summary is {want_p} paragraphs, no bullets",
              f"{n_para} paragraph(s), {n_bullet} bullet(s)",
              f"Rewrite as {want_p} short paragraphs with no bullets.",
              severity="medium", evidence={"paragraphs": n_para, "bullets": n_bullet}))


def _check_pipe_section(add, by_name, rules, name: str) -> None:
    rule = rules.get(name, {})
    want = rule.get("pipe_lines")
    cid = f"{name.split()[0].lower()}.pipes"
    s = by_name.get(name)
    if s is None:
        add(Check(cid, None, f"{name} is {want} pipe-separated line(s)", "section not found"))
        return
    lines = [p for p in s.paragraphs if p.text.strip()]
    piped = [p for p in lines if "|" in p.text]
    ok = len(lines) == want and len(piped) == want
    add(Check(cid, ok, f"{name} is {want} pipe-separated line(s)",
              f"{len(lines)} line(s), {len(piped)} with pipes",
              f"Format as exactly {want} line(s) with ' | ' between terms.",
              severity="medium",
              evidence={"lines": [p.text[:70] for p in lines[:5]]}))


def _check_achievements(add, by_name, rules, patterns) -> None:
    rule = rules.get("KEY ACHIEVEMENTS", {})
    want = rule.get("bullets", 4)
    s = by_name.get("KEY ACHIEVEMENTS")
    if s is None:
        add(Check("achievements.count", None, f"{want} quantified achievement bullets",
                  "section not found"))
        return
    bullets = s.bullets
    add(Check("achievements.count", len(bullets) == want,
              f"{want} achievement bullets", f"{len(bullets)} bullet(s)",
              f"Use exactly {want} bullets.", severity="medium",
              evidence={"count": len(bullets)}))
    if rule.get("each_quantified"):
        qre = re.compile(patterns["quantified_bullet"], re.I)
        unquantified = [b.text[:70] for b in bullets if not qre.search(b.text)]
        add(Check("achievements.quantified", not unquantified,
                  "Every achievement bullet carries a figure",
                  f"{len(unquantified)} without a number" if unquantified else "all quantified",
                  "Add a concrete figure to each bullet. Keep every number truthful.",
                  severity="medium", evidence={"examples": unquantified[:4]}))


def _check_experience(add, by_name, rules, patterns, elements) -> None:
    rule = rules.get("PROFESSIONAL EXPERIENCE", {})
    s = by_name.get("PROFESSIONAL EXPERIENCE")
    if s is None:
        for cid, t in (("experience.roles", "Roles well formed"),
                       ("experience.bullets", "Bullets per role in range"),
                       ("experience.chrono", "Reverse chronological")):
            add(Check(cid, None, t, "section not found"))
        return

    date_re = re.compile(patterns["company_date_line"])
    roles: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for p in s.paragraphs:
        if not p.text.strip():
            continue
        if p.numbered or p.typed_bullet:
            if current is not None:
                current["bullets"].append(p)
            continue
        m = date_re.match(p.text.strip())
        if m and current is not None and current.get("company_date") is None:
            current["company_date"] = p
            current["dates"] = (m.group("start"), m.group("end"))
            continue
        # A non-bullet, non-date line starts a new role.
        current = {"title": p, "company_date": None, "dates": None, "bullets": []}
        roles.append(current)

    malformed = [r["title"].text[:50] for r in roles if r["company_date"] is None]
    add(Check("experience.roles", bool(roles) and not malformed,
              "Each role has a title line and a 'Company | Month YYYY - Month YYYY' line",
              f"{len(roles)} role(s); malformed: {malformed}" if malformed
              else f"{len(roles)} role(s), all well formed",
              "Add the italic company/date line under each job title, using "
              "'Company | Month YYYY - Month YYYY'.",
              severity="high", evidence={"roles": len(roles), "malformed": malformed}))

    lo, hi = rule.get("bullets_per_role", [4, 5])
    off = [f"{r['title'].text[:40]}: {len(r['bullets'])}"
           for r in roles if not (lo <= len(r["bullets"]) <= hi)]
    add(Check("experience.bullets", not off, f"Each role has {lo}-{hi} bullets",
              "; ".join(off) if off else f"all roles have {lo}-{hi}",
              f"Bring every role to {lo}-{hi} achievement bullets.",
              severity="medium", evidence={"off": off}))

    title_want = elements["job_title"]
    bad_titles = [r["title"].text[:40] for r in roles
                  if abs(r["title"].size_pt - title_want["size_pt"]) >= 0.6
                  or (title_want.get("bold") and not r["title"].bold)]
    add(Check("experience.title_format", not bad_titles,
              f"Job titles are {title_want['size_pt']}pt bold",
              f"{len(bad_titles)} off-format" if bad_titles else "all correct",
              f"Set job titles to {title_want['size_pt']}pt bold.",
              severity="medium", evidence={"examples": bad_titles[:4]}))

    cd_want = elements["company_date"]
    have_cd = [r for r in roles if r["company_date"] is not None]
    bad_cd = [r["company_date"].text[:50] for r in have_cd
              if abs(r["company_date"].size_pt - cd_want["size_pt"]) >= 0.6
              or (cd_want.get("italic") and not r["company_date"].italic)]
    # With no recognisable company/date line there is nothing to judge; saying
    # "pass" here would paper over the malformed lines experience.roles found.
    add(Check("experience.company_format", (not bad_cd) if have_cd else None,
              f"Company/date lines are {cd_want['size_pt']}pt italic",
              f"{len(bad_cd)} off-format" if bad_cd else "all correct",
              f"Set company/date lines to {cd_want['size_pt']}pt italic.",
              severity="medium", evidence={"examples": bad_cd[:4]}))

    if rule.get("reverse_chronological"):
        starts = [_year_of(r["dates"][0]) for r in roles if r.get("dates")]
        starts = [y for y in starts if y]
        ok = all(starts[i] >= starts[i + 1] for i in range(len(starts) - 1))
        add(Check("experience.chrono", ok if starts else None,
                  "Roles are reverse chronological",
                  f"start years in document order: {starts}" if starts
                  else "no parseable dates",
                  "Reorder so the most recent role comes first.",
                  severity="medium", evidence={"starts": starts}))


def _year_of(text: str) -> Optional[int]:
    m = re.search(r"(19|20)\d{2}", text or "")
    return int(m.group(0)) if m else None


def _check_line_pattern(add, by_name, rules, name: str) -> None:
    rule = rules.get(name, {})
    pat = rule.get("line_pattern")
    cid = f"{name.split()[0].lower()}.lines"
    s = by_name.get(name)
    if s is None or not pat:
        add(Check(cid, None, f"{name} lines follow the canonical shape",
                  "section not found" if s is None else "no pattern configured"))
        return
    rx = re.compile(pat)
    lines = [p for p in s.paragraphs if p.text.strip()]
    bad = [p.text[:70] for p in lines if not rx.match(p.text.strip())]
    add(Check(cid, not bad and bool(lines), f"{name} lines follow the canonical shape",
              f"{len(bad)} line(s) off-pattern" if bad else f"{len(lines)} line(s) correct",
              {"EARLIER CAREER": "Use 'Title, Company (YYYY - YYYY)'.",
               "EDUCATION": "Use 'Degree, Institution (YYYY)' or '(YYYY - YYYY)'."}.get(name, ""),
              severity="medium", evidence={"bad": bad[:4]}))


# --------------------------------------------------------------------------
# Text parity
# --------------------------------------------------------------------------


def _normalise_for_compare(text: str) -> List[str]:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace(" ", " ").replace("’", "'")
    text = re.sub(r"[‐-―]", "-", text)
    words = re.findall(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9'.\-+#/]*|[€$£%]", text.lower())
    return words


def compare_text(a: str, b: str) -> Dict[str, Any]:
    """Compare two extractions as word sequences, order-sensitive."""
    wa, wb = _normalise_for_compare(a), _normalise_for_compare(b)
    sm = difflib.SequenceMatcher(None, wa, wb, autojunk=False)
    ratio = sm.ratio()

    only_a: List[str] = []
    only_b: List[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            only_a.extend(wa[i1:i2])
        if tag in ("insert", "replace"):
            only_b.extend(wb[j1:j2])

    return {
        "similarity": round(ratio, 4),
        "words_a": len(wa),
        "words_b": len(wb),
        "only_in_a": only_a[:60],
        "only_in_b": only_b[:60],
        "only_in_a_count": len(only_a),
        "only_in_b_count": len(only_b),
    }


def compare_coverage(source: str, target: str) -> Dict[str, Any]:
    """How much of ``source``'s wording survives into ``target``, ignoring order.

    Used for design-export parity. The Canva twin is multi-column, so its text
    extracts in a scrambled order by construction -- an order-sensitive
    comparison would fail every time and tell us nothing. The actual rule
    being enforced is "the ATS build contains 100% of the twin's text", which
    is a multiset question, not a sequence one.
    """
    from collections import Counter

    src = Counter(_normalise_for_compare(source))
    tgt = Counter(_normalise_for_compare(target))
    missing = src - tgt
    added = tgt - src
    total = sum(src.values())
    covered = total - sum(missing.values())
    return {
        "coverage": round(covered / total, 4) if total else 0.0,
        "words_source": total,
        "words_target": sum(tgt.values()),
        "missing": [w for w, _ in missing.most_common(60)],
        "missing_count": sum(missing.values()),
        "added": [w for w, _ in added.most_common(40)],
        "added_count": sum(added.values()),
    }


def compare_blocks(source: str, target: str, min_words: int = 4,
                   match_threshold: float = 0.75) -> Dict[str, Any]:
    """Find whole blocks of ``source`` that have no counterpart in ``target``.

    A word-count ratio is too blunt for the content-parity rule: an entire
    bullet can disappear from a 750-word CV and still leave ~98% of the words
    intact, which sails past any sane ratio threshold. Matching block by block
    makes a dropped bullet unambiguous.

    Each source line is matched against its best counterpart anywhere in the
    target, so reordering -- which a multi-column design export guarantees --
    costs nothing.
    """
    def blocks(text: str) -> List[str]:
        out: List[str] = []
        for raw in re.split(r"[\n\r]+", text):
            line = raw.strip(" \t•●▪-–—*·")
            if len(_normalise_for_compare(line)) >= min_words:
                out.append(line)
        return out

    src_blocks = blocks(source)
    tgt_blocks = blocks(target)
    tgt_norm = [" ".join(_normalise_for_compare(b)) for b in tgt_blocks]
    # One long haystack catches blocks that were re-wrapped across lines.
    haystack = " ".join(tgt_norm)

    missing: List[Dict[str, Any]] = []
    for block in src_blocks[:400]:
        norm = " ".join(_normalise_for_compare(block))
        if not norm:
            continue
        if norm in haystack:
            continue
        best, best_line = 0.0, ""
        for cand, cand_norm in zip(tgt_blocks[:400], tgt_norm[:400]):
            ratio = difflib.SequenceMatcher(None, norm, cand_norm, autojunk=False).ratio()
            if ratio > best:
                best, best_line = ratio, cand
                if best >= 0.99:
                    break
        if best < match_threshold:
            missing.append({
                "text": block[:160],
                "best_match": best_line[:160],
                "best_ratio": round(best, 3),
            })

    return {
        "source_blocks": len(src_blocks),
        "missing_blocks": missing[:20],
        "missing_count": len(missing),
    }


def score(checks: Sequence[Check]) -> int:
    total = 100
    for c in checks:
        if c.ok is False:
            total -= PENALTY.get(c.severity, 5)
    return max(0, min(100, total))
