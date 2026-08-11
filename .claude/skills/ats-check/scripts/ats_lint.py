"""Parseability checks: will an ATS read this resume correctly at all?

This module is deliberately kept separate from job-description matching.
Keyword fit is a judgement call; parseability is closer to a fact, and it is
worth reporting even when no JD is supplied -- a resume that extracts as
garbage scores zero against every job.

Each check returns a Finding with a severity:

  blocker  the ATS almost certainly gets nothing, or nothing usable
  high     a whole section or the contact block is likely lost or scrambled
  medium   content survives but degrades; some parsers will mis-file it
  low      cosmetic or defensive
  info     context, no action implied
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from readers import Extraction

__all__ = ["Finding", "lint", "parseability_score"]

SEVERITY_WEIGHT = {"blocker": 55, "high": 18, "medium": 7, "low": 2, "info": 0}


@dataclass
class Finding:
    id: str
    severity: str
    title: str
    detail: str
    fix: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Section headings an ATS looks for
# --------------------------------------------------------------------------

SECTION_PATTERNS: Dict[str, List[str]] = {
    "experience": [
        r"\bwork\s+experience\b", r"\bprofessional\s+experience\b", r"\bexperience\b",
        r"\bemployment(\s+history)?\b", r"\bcareer\s+history\b", r"\besperienz[ae]\b",
    ],
    "education": [r"\beducation\b", r"\bacademic\b", r"\bqualifications\b", r"\bistruzione\b", r"\bformazione\b"],
    "skills": [r"\bskills\b", r"\btechnical\s+skills\b", r"\bcompetenc(?:e|ies)\b", r"\bcompetenze\b", r"\btech\s+stack\b"],
    "summary": [r"\bsummary\b", r"\bprofile\b", r"\bobjective\b", r"\babout\s+me\b", r"\bprofilo\b"],
}

# Headings that read well to a human but that keyword-driven parsers miss.
CREATIVE_HEADINGS = [
    r"where\s+i(?:'ve)?\s+worked", r"what\s+i(?:'ve)?\s+done", r"my\s+journey",
    r"the\s+story\s+so\s+far", r"things\s+i\s+know", r"my\s+toolkit",
    r"career\s+highlights", r"selected\s+work", r"how\s+i\s+work",
]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Deliberately permissive: international formats, spaces, dots and dashes.
PHONE_RE = re.compile(r"(?:(?:\+|00)\d{1,3}[\s.\-]?)?(?:\(\d{1,4}\)[\s.\-]?)?\d[\d\s.\-]{6,14}\d")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()]+|(?:linkedin\.com|github\.com)/[^\s<>()]+", re.I)

DATE_PATTERNS = [
    r"\b(19|20)\d{2}\s*[-–—]\s*((19|20)\d{2}|present|current|now|ongoing|oggi|attuale)\b",
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(19|20)\d{2}\b",
    r"\b(?:gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic)[a-z]*\.?\s+(19|20)\d{2}\b",
    r"\b\d{1,2}/(19|20)\d{2}\b",
    r"\b(19|20)\d{2}\b",
]

# Bullet glyphs that survive extraction, versus ones that turn into noise.
SAFE_BULLETS = set("•-–—*·")
RISKY_BULLET_RANGES = [(0xE000, 0xF8FF), (0x2700, 0x27BF), (0x1F300, 0x1FAFF), (0x2190, 0x21FF)]


def _has_section(text: str, key: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in SECTION_PATTERNS[key])


def _in_risky_range(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in RISKY_BULLET_RANGES)


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def lint(ex: Extraction, jd_text: Optional[str] = None) -> List[Finding]:
    findings: List[Finding] = []
    text = ex.text or ""
    norm = _normalise(text)
    words = ex.word_count

    # ---- encryption ----------------------------------------------------
    if ex.encrypted:
        findings.append(Finding(
            id="encrypted",
            severity="blocker",
            title="PDF is encrypted or password-protected",
            detail="The file carries an /Encrypt dictionary. Many ATS refuse encrypted PDFs "
                   "outright, and those that accept them often extract nothing.",
            fix="Re-export without a password and without permission restrictions.",
        ))

    # ---- is there a text layer at all? ---------------------------------
    total_glyphs = sum(p.total_glyphs for p in ex.pages)
    if ex.kind == "pdf":
        img_heavy = any(p.image_area_ratio > 0.55 for p in ex.pages)
        if words < 40 and img_heavy:
            findings.append(Finding(
                id="scanned_no_text",
                severity="blocker",
                title="Resume is an image, not text",
                detail=f"Only {words} extractable words across {len(ex.pages)} page(s), with "
                       f"page-covering images. This is a scan or an exported picture. An ATS "
                       f"reads it as an empty document.",
                fix="Export the original document straight to PDF (File > Save as PDF), or run OCR. "
                    "Never print-to-image or scan a printout.",
                evidence={"words": words, "image_area_ratio": max(p.image_area_ratio for p in ex.pages)},
            ))
        elif words < 40:
            findings.append(Finding(
                id="almost_no_text",
                severity="blocker",
                title="Almost no text could be extracted",
                detail=f"Only {words} words came out of the file. Whatever the cause, an ATS "
                       f"will see roughly the same thing.",
                fix="Re-export the PDF from the source document.",
                evidence={"words": words},
            ))
        elif img_heavy and words < 180:
            findings.append(Finding(
                id="partly_image",
                severity="high",
                title="Large parts of the page are images",
                detail="Images cover most of at least one page while the text layer is thin. "
                       "Sections rendered as pictures are invisible to an ATS.",
                fix="Replace image-rendered text (skill graphics, charts, logos with wording) with real text.",
                evidence={"words": words},
            ))

    # ---- unmapped glyphs ------------------------------------------------
    unmapped = sum(p.unmapped_glyphs for p in ex.pages)
    if total_glyphs and unmapped / total_glyphs > 0.02:
        pct = 100 * unmapped / total_glyphs
        sev = "blocker" if pct > 25 else ("high" if pct > 8 else "medium")
        bad_fonts = [f["name"] for f in ex.fonts if f["two_byte"] and not f["has_tounicode"]]
        findings.append(Finding(
            id="unmapped_glyphs",
            severity=sev,
            title=f"{pct:.0f}% of characters extract as garbage",
            detail="The PDF's fonts carry no usable character map, so the text on screen does "
                   "not correspond to the bytes an extractor recovers. The resume looks fine to "
                   "a human and is nonsense to a machine."
                   + (f" Fonts at fault: {', '.join(sorted(set(bad_fonts))[:4])}." if bad_fonts else ""),
            fix="Re-export the PDF with a different generator (Word, Google Docs and LibreOffice all "
                "embed ToUnicode maps), or embed the fonts as standard subsets. Verify by selecting "
                "text in a PDF viewer and pasting it into a plain text editor.",
            evidence={"unmapped": unmapped, "total": total_glyphs, "fonts": sorted(set(bad_fonts))[:6]},
        ))

    non_embedded = [f["name"] for f in ex.fonts if not f["embedded"] and f["symbolic"]]
    if non_embedded:
        findings.append(Finding(
            id="non_embedded_symbolic_font",
            severity="medium",
            title="Symbolic fonts are not embedded",
            detail=f"{', '.join(sorted(set(non_embedded))[:4])} are symbolic and not embedded. "
                   "Extraction falls back to guesswork, which usually yields wrong characters.",
            fix="Embed all fonts on export, or switch icon fonts out for plain text.",
        ))

    # ---- reading order --------------------------------------------------
    multi = [p.number for p in ex.pages if p.multi_column]
    if multi:
        findings.append(Finding(
            id="multi_column",
            severity="high",
            title=f"Multi-column layout on page(s) {', '.join(map(str, multi))}",
            detail="A vertical gutter splits the page. Parsers that read in content-stream order "
                   "interleave the columns, so a job title from the left column ends up glued to a "
                   "skill from the right one. Dates, employers and bullets stop lining up.",
            fix="Use a single-column layout for the whole document. Keep a sidebar only if you are "
                "certain the resume is read by a human first.",
            evidence={"pages": multi, "gutters": [g for p in ex.pages for g in p.gutters]},
        ))
        divergence = _order_divergence(ex)
        if divergence > 0.12:
            findings.append(Finding(
                id="order_dependent",
                severity="high",
                title="Extracted meaning depends on which parser reads the file",
                detail=f"Column-aware and stream-order extraction disagree on about "
                       f"{divergence * 100:.0f}% of line content. Two different ATS will build two "
                       f"different resumes out of this same file.",
                fix="Collapse to one column so both extraction strategies agree.",
                evidence={"divergence": round(divergence, 3)},
            ))

    # ---- contact details -------------------------------------------------
    emails = EMAIL_RE.findall(norm)
    phones = [p for p in PHONE_RE.findall(norm) if len(re.sub(r"\D", "", p)) >= 8]
    if not emails:
        link_mails = [u for p in ex.pages for u in p.link_uris if u.lower().startswith("mailto:")]
        if link_mails:
            findings.append(Finding(
                id="email_only_in_link",
                severity="high",
                title="Email address exists only as a hyperlink",
                detail="No email appears in the extractable text; it is only in a link annotation. "
                       "Most parsers read text, not annotations, so the contact field comes back empty.",
                fix="Write the address out as visible text as well as linking it.",
                evidence={"mailto": link_mails[:3]},
            ))
        else:
            findings.append(Finding(
                id="no_email",
                severity="blocker",
                title="No email address found in the text",
                detail="An ATS that cannot find an email address frequently rejects the application "
                       "outright, or creates a contactless record.",
                fix="Put a plain-text email in the top section of page 1.",
            ))
    if not phones:
        findings.append(Finding(
            id="no_phone",
            severity="medium",
            title="No phone number found",
            detail="No digit sequence in the text looks like a phone number.",
            fix="Add a phone number in plain text, ideally in international format (+39 ...).",
        ))

    # Contact details stranded in the header/footer band.
    if ex.kind == "pdf" and ex.pages:
        first = ex.pages[0]
        band = first.top_band_text + " " + first.bottom_band_text
        body_wo_band = first.layout_text
        if emails and EMAIL_RE.search(band):
            in_band_only = not EMAIL_RE.search(body_wo_band.replace(band, " "))
            if in_band_only:
                findings.append(Finding(
                    id="contact_in_header_band",
                    severity="medium",
                    title="Contact details sit in the page header/footer band",
                    detail="The email is in the top or bottom 6% of the sheet. A number of parsers "
                           "strip running headers and footers before analysing the body.",
                    fix="Move the contact block into the body of the page, just under your name.",
                ))
    if ex.kind == "docx" and ex.header_footer_text and EMAIL_RE.search(ex.header_footer_text):
        findings.append(Finding(
            id="contact_in_docx_header",
            severity="high",
            title="Contact details are in the Word header/footer",
            detail="Word headers and footers live outside the document body. Many DOCX parsers "
                   "never open them, so the contact block vanishes.",
            fix="Move your name, email and phone into the body of the document.",
            evidence={"header_footer": ex.header_footer_text[:160]},
        ))

    # ---- structure --------------------------------------------------------
    missing = [k for k in ("experience", "education", "skills") if not _has_section(text, k)]
    if missing:
        creative = [h for h in CREATIVE_HEADINGS if re.search(h, text.lower())]
        findings.append(Finding(
            id="missing_sections",
            severity="high" if "experience" in missing else "medium",
            title=f"Standard section heading(s) not found: {', '.join(missing)}",
            detail="ATS software segments a resume by matching well-known heading words. Sections it "
                   "cannot label are often dropped from the structured profile even when the text is read."
                   + (f" Non-standard headings detected: {', '.join(creative[:3])}." if creative else ""),
            fix="Use literal headings: 'Work Experience', 'Education', 'Skills'. Keep them on their "
                "own line, in the same language as the job posting.",
            evidence={"missing": missing, "creative_headings": creative[:5]},
        ))

    # ---- dates ------------------------------------------------------------
    date_hits = sum(len(re.findall(p, text, re.I)) for p in DATE_PATTERNS)
    ranges = len(re.findall(DATE_PATTERNS[0], text, re.I))
    if date_hits == 0:
        findings.append(Finding(
            id="no_dates",
            severity="high",
            title="No dates found",
            detail="Without parseable dates an ATS cannot compute tenure or total years of "
                   "experience, which many screening rules filter on.",
            fix="Add explicit ranges such as 'Mar 2021 – Present' to every role.",
        ))
    elif ranges == 0 and _has_section(text, "experience"):
        findings.append(Finding(
            id="no_date_ranges",
            severity="medium",
            title="Dates found, but no start–end ranges",
            detail="Individual years appear but no 'YYYY – YYYY' style ranges. Tenure per role "
                   "cannot be derived reliably.",
            fix="Give each role a start and an end (or 'Present').",
            evidence={"date_mentions": date_hits},
        ))

    # ---- tables and text boxes (DOCX) --------------------------------------
    if ex.tables:
        findings.append(Finding(
            id="docx_tables",
            severity="medium",
            title=f"{ex.tables} table(s) used for layout",
            detail="Tables are read cell by cell. Parsers commonly flatten them row-first, which "
                   "shuffles a two-column table into alternating fragments.",
            fix="Replace layout tables with ordinary paragraphs and tab stops.",
        ))
    if ex.textboxes:
        findings.append(Finding(
            id="docx_textboxes",
            severity="high",
            title=f"{ex.textboxes} text box(es) detected",
            detail="Text boxes sit outside the main document flow. A large share of DOCX parsers "
                   "skip them entirely, so anything inside is simply absent.",
            fix="Move all text box content into normal paragraphs.",
        ))

    # ---- invisible text -----------------------------------------------------
    invisible = sum(p.invisible_chars for p in ex.pages)
    if invisible > 30:
        findings.append(Finding(
            id="invisible_text",
            severity="high",
            title=f"{invisible} characters are rendered invisibly",
            detail="Text drawn in invisible render mode is extracted but not displayed. If this is "
                   "deliberate keyword stuffing, note that modern ATS and recruiters flag it and it "
                   "is grounds for rejection. If it is an OCR layer, the visible page is an image.",
            fix="Remove hidden text. If the page is a scan with an OCR layer, rebuild the resume "
                "from the source document instead.",
            evidence={"chars": invisible},
        ))
    white_stuffing = _detect_keyword_stuffing(text)
    if white_stuffing:
        findings.append(Finding(
            id="keyword_stuffing",
            severity="high",
            title="Possible keyword stuffing",
            detail=f"Repeated dense keyword runs detected ({', '.join(white_stuffing[:4])}). "
                   "Recruiters see the extracted text, and this pattern reads as gaming the filter.",
            fix="Remove keyword lists that are not backed by real experience in the body.",
        ))

    # ---- typography ----------------------------------------------------------
    if ex.kind == "pdf":
        tiny = [p.number for p in ex.pages if 0 < p.min_body_size < 8.0]
        if tiny:
            findings.append(Finding(
                id="tiny_text",
                severity="low",
                title="Very small text present",
                detail=f"Text below 8pt on page(s) {', '.join(map(str, tiny))}. It extracts fine but "
                       "signals an over-stuffed page, and recruiters skim past it.",
                fix="Keep body text at 10pt or above; cut content instead of shrinking it.",
            ))

    risky = sorted({ch for ch in norm if _in_risky_range(ch)})
    if risky:
        findings.append(Finding(
            id="risky_glyphs",
            severity="low",
            title="Icon or private-use characters in the text",
            detail=f"Characters such as {' '.join(repr(c) for c in risky[:5])} come from icon fonts "
                   "or private-use ranges. They extract as noise and can split adjacent words.",
            fix="Replace icon glyphs with plain words ('Email:', 'Phone:', 'GitHub:').",
            evidence={"chars": [hex(ord(c)) for c in risky[:10]]},
        ))

    # ---- length ---------------------------------------------------------------
    pages_n = len(ex.pages)
    if words > 1200 or pages_n > 3:
        findings.append(Finding(
            id="too_long",
            severity="low",
            title=f"Long resume ({words} words, {pages_n} page(s))",
            detail="Parsing is unaffected, but human reviewers rarely read past page two.",
            fix="Trim to two pages unless the role is academic or the market expects a long CV.",
        ))
    elif words < 200:
        findings.append(Finding(
            id="too_short",
            severity="medium",
            title=f"Very short resume ({words} words)",
            detail="There may be too little text for keyword matching to find anything to latch onto.",
            fix="Expand each role with concrete, quantified accomplishments.",
        ))

    for warning in ex.warnings:
        findings.append(Finding(
            id="reader_warning", severity="info", title="Reader note", detail=warning,
        ))

    order = {"blocker": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    findings.sort(key=lambda f: (order.get(f.severity, 9), f.id))
    return findings


def _order_divergence(ex: Extraction) -> float:
    """How much do stream-order and layout-order extraction disagree?

    Compared as multisets of lines: identical content in a different order
    still counts as divergence, because that reordering is exactly the damage.
    """
    total, diff = 0, 0
    for page in ex.pages:
        a = [ln.strip() for ln in page.naive_text.split("\n") if ln.strip()]
        b = [ln.strip() for ln in page.layout_text.split("\n") if ln.strip()]
        total += max(len(a), len(b), 1)
        common = 0
        pool = list(b)
        for line in a:
            if line in pool:
                pool.remove(line)
                common += 1
        diff += max(len(a), len(b)) - common
    return diff / total if total else 0.0


def _detect_keyword_stuffing(text: str) -> List[str]:
    """Flag comma/pipe runs of many short noun-ish tokens repeated verbatim."""
    suspects: List[str] = []
    for line in text.split("\n"):
        parts = [p.strip() for p in re.split(r"[,|;/]", line) if p.strip()]
        if len(parts) >= 12 and all(len(p) <= 24 for p in parts):
            suspects.append(line.strip()[:60])
    counts: Dict[str, int] = {}
    for token in re.findall(r"[A-Za-z][A-Za-z+#.]{2,20}", text.lower()):
        counts[token] = counts.get(token, 0) + 1
    words_total = max(sum(counts.values()), 1)
    for token, n in counts.items():
        if n >= 12 and n / words_total > 0.02 and token not in _COMMON:
            suspects.append(f"{token} x{n}")
    return suspects[:6]


_COMMON = {
    "and", "the", "for", "with", "team", "data", "using", "from", "that", "this",
    "work", "role", "new", "was", "were", "been", "has", "have", "our", "all",
    "development", "management", "experience", "engineer", "software", "project",
    "projects", "business", "product", "system", "systems", "service", "services",
}


def parseability_score(findings: List[Finding]) -> int:
    """0-100. Starts at 100 and subtracts weighted penalties."""
    score = 100
    for f in findings:
        score -= SEVERITY_WEIGHT.get(f.severity, 0)
    return max(0, min(100, score))


def findings_as_dicts(findings: List[Finding]) -> List[Dict[str, Any]]:
    return [asdict(f) for f in findings]
