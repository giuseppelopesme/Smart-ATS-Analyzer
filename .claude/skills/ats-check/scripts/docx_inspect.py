"""Deep DOCX inspection: formatting, not just text.

Validating a CV against a canonical structure needs the things a plain text
extraction throws away -- what point size a run is, whether a bullet is a real
numbering definition or a typed "•", whether a heading rule is a paragraph
border or a smuggled-in table, whether the header part is genuinely empty.

Everything is resolved the way Word resolves it: direct run properties win,
then the paragraph style (following ``basedOn`` up the chain), then
``docDefaults``. Reading only direct properties reports "no font set" on a
document that renders perfectly, which is worse than not checking at all.

Standard library only: a .docx is a ZIP of XML.
"""

from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

__all__ = ["DocxModel", "Paragraph", "Run", "Section", "inspect_docx", "DocxError"]

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
DC = "{http://purl.org/dc/elements/1.1/}"
EP = "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}"

TWIPS_PER_PT = 20.0
TWIPS_PER_CM = 566.929133858  # 1 cm = 72/2.54 pt = 28.3465 pt = 566.93 twips

TYPED_BULLET_RE = re.compile(r"^\s*[•●▪◦‣⁃·\-\*–—o]\s+")


class DocxError(Exception):
    pass


@dataclass
class Run:
    text: str
    font: str = ""
    size_pt: float = 0.0
    bold: bool = False
    italic: bool = False
    underline: bool = False
    color: str = ""
    caps: bool = False
    hyperlink: str = ""

    @property
    def is_gray(self) -> bool:
        c = self.color.lower().lstrip("#")
        if len(c) != 6:
            return False
        try:
            r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        except ValueError:
            return False
        return max(r, g, b) - min(r, g, b) <= 24 and 0x40 <= r <= 0xE0


@dataclass
class Paragraph:
    index: int
    text: str
    style: str = ""
    runs: List[Run] = field(default_factory=list)
    numbered: bool = False
    num_id: str = ""
    ilvl: int = 0
    has_bottom_border: bool = False
    alignment: str = ""
    in_table: bool = False
    in_textbox: bool = False
    page_break_before: bool = False
    has_page_break: bool = False

    @property
    def size_pt(self) -> float:
        """Dominant point size, weighted by how much text each run carries."""
        best, best_len = 0.0, -1
        for run in self.runs:
            n = len(run.text.strip())
            if n > best_len and run.size_pt:
                best, best_len = run.size_pt, n
        return best

    @property
    def fonts(self) -> Set[str]:
        return {r.font for r in self.runs if r.font and r.text.strip()}

    @property
    def bold(self) -> bool:
        sized = [r for r in self.runs if r.text.strip()]
        return bool(sized) and all(r.bold for r in sized)

    @property
    def italic(self) -> bool:
        sized = [r for r in self.runs if r.text.strip()]
        return bool(sized) and all(r.italic for r in sized)

    @property
    def typed_bullet(self) -> bool:
        return bool(TYPED_BULLET_RE.match(self.text)) and not self.numbered

    @property
    def is_all_caps(self) -> bool:
        letters = [c for c in self.text if c.isalpha()]
        if len(letters) < 3:
            return False
        if all(r.caps for r in self.runs if r.text.strip()):
            return True
        return all(c.isupper() for c in letters)


@dataclass
class Section:
    page_w_twips: int = 0
    page_h_twips: int = 0
    top: int = 0
    bottom: int = 0
    left: int = 0
    right: int = 0
    cols: int = 1
    header_ref: bool = False
    footer_ref: bool = False

    @property
    def is_a4(self) -> bool:
        # A4 is 11906 x 16838 twips; allow a little rounding slack.
        return abs(self.page_w_twips - 11906) <= 60 and abs(self.page_h_twips - 16838) <= 60

    def margins_cm(self) -> Dict[str, float]:
        return {
            "top": round(self.top / TWIPS_PER_CM, 2),
            "bottom": round(self.bottom / TWIPS_PER_CM, 2),
            "left": round(self.left / TWIPS_PER_CM, 2),
            "right": round(self.right / TWIPS_PER_CM, 2),
        }


@dataclass
class DocxModel:
    paragraphs: List[Paragraph] = field(default_factory=list)
    sections: List[Section] = field(default_factory=list)
    tables: int = 0
    textboxes: int = 0
    images: int = 0
    header_texts: List[str] = field(default_factory=list)
    footer_texts: List[str] = field(default_factory=list)
    hyperlinks: List[str] = field(default_factory=list)
    numbering_ids: Set[str] = field(default_factory=set)
    numbering_defined: Set[str] = field(default_factory=set)
    metadata: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        lines = [p.text for p in self.paragraphs]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    @property
    def body_paragraphs(self) -> List[Paragraph]:
        return [p for p in self.paragraphs if p.text.strip()]

    @property
    def word_count(self) -> int:
        return len(re.findall(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9'’\-]*", self.text))

    @property
    def fonts_used(self) -> Set[str]:
        out: Set[str] = set()
        for p in self.paragraphs:
            out |= p.fonts
        return out


# --------------------------------------------------------------------------
# Style resolution
# --------------------------------------------------------------------------


class _StyleTable:
    """Resolves run properties through the style chain, as Word does."""

    def __init__(self, styles_xml: Optional[bytes]):
        self.default: Dict[str, Any] = {"font": "", "size_pt": 0.0, "bold": False,
                                        "italic": False, "color": "", "caps": False}
        self.styles: Dict[str, Dict[str, Any]] = {}
        self.based_on: Dict[str, str] = {}
        if not styles_xml:
            return
        try:
            root = ET.fromstring(styles_xml)
        except ET.ParseError:
            return

        dd = root.find(f"{W}docDefaults/{W}rPrDefault/{W}rPr")
        if dd is not None:
            self.default.update(_read_rpr(dd))

        for st in root.findall(f"{W}style"):
            sid = st.get(f"{W}styleId")
            if not sid:
                continue
            rpr = st.find(f"{W}rPr")
            self.styles[sid] = _read_rpr(rpr) if rpr is not None else {}
            base = st.find(f"{W}basedOn")
            if base is not None and base.get(f"{W}val"):
                self.based_on[sid] = base.get(f"{W}val")

    def resolve(self, style_id: str, direct: Dict[str, Any]) -> Dict[str, Any]:
        chain: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        sid = style_id
        while sid and sid in self.styles and sid not in seen:
            seen.add(sid)
            chain.append(self.styles[sid])
            sid = self.based_on.get(sid, "")

        out = dict(self.default)
        for layer in reversed(chain):  # base style first, most derived last
            out.update({k: v for k, v in layer.items() if v not in ("", None)})
        out.update({k: v for k, v in direct.items() if v not in ("", None)})
        return out


def _on(el: Optional[ET.Element]) -> Optional[bool]:
    """A w:b / w:i style toggle: present means on unless val says otherwise."""
    if el is None:
        return None
    val = el.get(f"{W}val")
    if val is None:
        return True
    return val not in ("0", "false", "off")


def _read_rpr(rpr: Optional[ET.Element]) -> Dict[str, Any]:
    if rpr is None:
        return {}
    out: Dict[str, Any] = {}
    fonts = rpr.find(f"{W}rFonts")
    if fonts is not None:
        name = (fonts.get(f"{W}ascii") or fonts.get(f"{W}hAnsi")
                or fonts.get(f"{W}cs") or fonts.get(f"{W}eastAsia"))
        if name:
            out["font"] = name
    sz = rpr.find(f"{W}sz")
    if sz is not None and sz.get(f"{W}val"):
        try:
            out["size_pt"] = float(sz.get(f"{W}val")) / 2.0  # half-points
        except ValueError:
            pass
    for tag, key in ((f"{W}b", "bold"), (f"{W}i", "italic"), (f"{W}caps", "caps")):
        state = _on(rpr.find(tag))
        if state is not None:
            out[key] = state
    u = rpr.find(f"{W}u")
    if u is not None:
        out["underline"] = u.get(f"{W}val", "single") != "none"
    color = rpr.find(f"{W}color")
    if color is not None and color.get(f"{W}val"):
        out["color"] = color.get(f"{W}val")
    return out


# --------------------------------------------------------------------------
# Body walk
# --------------------------------------------------------------------------


def _para_text(p: ET.Element) -> str:
    parts: List[str] = []
    for el in p.iter():
        if el.tag == f"{W}t":
            parts.append(el.text or "")
        elif el.tag == f"{W}tab":
            parts.append("\t")
        elif el.tag in (f"{W}br", f"{W}cr"):
            parts.append("\n")
    return "".join(parts)


def _build_paragraph(
    p: ET.Element, index: int, styles: _StyleTable, rels: Dict[str, str],
    in_table: bool, in_textbox: bool,
) -> Paragraph:
    ppr = p.find(f"{W}pPr")
    style_id = ""
    numbered, num_id, ilvl = False, "", 0
    bottom_border = False
    alignment = ""
    page_break_before = False

    if ppr is not None:
        ps = ppr.find(f"{W}pStyle")
        if ps is not None:
            style_id = ps.get(f"{W}val", "")
        numpr = ppr.find(f"{W}numPr")
        if numpr is not None:
            nid = numpr.find(f"{W}numId")
            lvl = numpr.find(f"{W}ilvl")
            # numId 0 explicitly means "no numbering" (a style override).
            if nid is not None and nid.get(f"{W}val") not in (None, "0"):
                numbered = True
                num_id = nid.get(f"{W}val", "")
            if lvl is not None:
                try:
                    ilvl = int(lvl.get(f"{W}val", "0"))
                except ValueError:
                    ilvl = 0
        pbdr = ppr.find(f"{W}pBdr")
        if pbdr is not None:
            bot = pbdr.find(f"{W}bottom")
            if bot is not None and bot.get(f"{W}val", "none") not in ("none", "nil"):
                bottom_border = True
        jc = ppr.find(f"{W}jc")
        if jc is not None:
            alignment = jc.get(f"{W}val", "")
        if _on(ppr.find(f"{W}pageBreakBefore")):
            page_break_before = True

    runs: List[Run] = []
    has_break = False
    # Walk direct children so hyperlink wrappers are handled, but runs inside
    # a nested textbox are not attributed to this paragraph.
    for child in p:
        if child.tag == f"{W}hyperlink":
            target = rels.get(child.get(f"{R}id", ""), "")
            for r in child.findall(f"{W}r"):
                runs.extend(_build_runs(r, styles, style_id, target))
        elif child.tag == f"{W}r":
            if r_has_textbox(child):
                continue
            runs.extend(_build_runs(child, styles, style_id, ""))
            if child.find(f"{W}br[@{W}type='page']") is not None:
                has_break = True

    text = "".join(r.text for r in runs)
    if not text.strip():
        text = _para_text(p) if not in_textbox else text

    return Paragraph(
        index=index, text=text.rstrip(), style=style_id, runs=runs,
        numbered=numbered, num_id=num_id, ilvl=ilvl,
        has_bottom_border=bottom_border, alignment=alignment,
        in_table=in_table, in_textbox=in_textbox,
        page_break_before=page_break_before, has_page_break=has_break,
    )


def r_has_textbox(r: ET.Element) -> bool:
    return any(el.tag.endswith("}txbxContent") for el in r.iter())


def _build_runs(r: ET.Element, styles: _StyleTable, style_id: str, link: str) -> List[Run]:
    direct = _read_rpr(r.find(f"{W}rPr"))
    props = styles.resolve(style_id, direct)
    text_parts: List[str] = []
    for el in r:
        if el.tag == f"{W}t":
            text_parts.append(el.text or "")
        elif el.tag == f"{W}tab":
            text_parts.append("\t")
        elif el.tag in (f"{W}br", f"{W}cr"):
            text_parts.append("\n")
    text = "".join(text_parts)
    if not text:
        return []
    return [Run(
        text=text,
        font=props.get("font", ""),
        size_pt=float(props.get("size_pt") or 0.0),
        bold=bool(props.get("bold")),
        italic=bool(props.get("italic")),
        underline=bool(props.get("underline")),
        color=props.get("color", ""),
        caps=bool(props.get("caps")),
        hyperlink=link,
    )]


def _read_section(sect: ET.Element) -> Section:
    s = Section()
    pg = sect.find(f"{W}pgSz")
    if pg is not None:
        s.page_w_twips = _int(pg.get(f"{W}w"))
        s.page_h_twips = _int(pg.get(f"{W}h"))
    mar = sect.find(f"{W}pgMar")
    if mar is not None:
        s.top = _int(mar.get(f"{W}top"))
        s.bottom = _int(mar.get(f"{W}bottom"))
        s.left = _int(mar.get(f"{W}left"))
        s.right = _int(mar.get(f"{W}right"))
    cols = sect.find(f"{W}cols")
    if cols is not None:
        s.cols = max(1, _int(cols.get(f"{W}num")) or 1)
    s.header_ref = sect.find(f"{W}headerReference") is not None
    s.footer_ref = sect.find(f"{W}footerReference") is not None
    return s


def _int(v: Optional[str]) -> int:
    try:
        return int(float(v)) if v is not None else 0
    except ValueError:
        return 0


def inspect_docx(path: str) -> DocxModel:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocxError(f"cannot open as .docx: {exc}") from exc

    model = DocxModel()
    with zf:
        names = set(zf.namelist())
        if "word/document.xml" not in names:
            raise DocxError("missing word/document.xml -- not a Word document")

        styles = _StyleTable(zf.read("word/styles.xml") if "word/styles.xml" in names else None)

        rels: Dict[str, str] = {}
        if "word/_rels/document.xml.rels" in names:
            try:
                rroot = ET.fromstring(zf.read("word/_rels/document.xml.rels"))
                for rel in rroot:
                    rid, tgt = rel.get("Id"), rel.get("Target")
                    if rid and tgt:
                        rels[rid] = tgt
            except ET.ParseError:
                model.warnings.append("document.xml.rels is not valid XML")

        if "word/numbering.xml" in names:
            try:
                nroot = ET.fromstring(zf.read("word/numbering.xml"))
                for num in nroot.findall(f"{W}num"):
                    nid = num.get(f"{W}numId")
                    if nid:
                        model.numbering_defined.add(nid)
            except ET.ParseError:
                model.warnings.append("numbering.xml is not valid XML")

        try:
            root = ET.fromstring(zf.read("word/document.xml"))
        except ET.ParseError as exc:
            raise DocxError(f"document.xml is not valid XML: {exc}") from exc

        body = root.find(f"{W}body")
        if body is None:
            raise DocxError("document.xml has no body")

        idx = 0

        def walk(node: ET.Element, in_table: bool, in_textbox: bool) -> None:
            nonlocal idx
            for child in node:
                tag = child.tag
                if tag == f"{W}p":
                    model.paragraphs.append(
                        _build_paragraph(child, idx, styles, rels, in_table, in_textbox))
                    idx += 1
                    # A paragraph may still wrap a text box holding more paragraphs.
                    for tb in child.iter():
                        if tb.tag.endswith("}txbxContent"):
                            model.textboxes += 1
                            walk(tb, in_table, True)
                elif tag == f"{W}tbl":
                    model.tables += 1
                    walk(child, True, in_textbox)
                elif tag in (f"{W}tr", f"{W}tc", f"{W}sdt", f"{W}sdtContent",
                             f"{W}smartTag", f"{W}customXml"):
                    walk(child, in_table or tag in (f"{W}tr", f"{W}tc"), in_textbox)
                elif tag == f"{W}sectPr":
                    model.sections.append(_read_section(child))

        walk(body, False, False)

        for el in root.iter():
            if el.tag in (f"{W}drawing", f"{W}pict", f"{W}object"):
                if not any(x.tag.endswith("}txbxContent") for x in el.iter()):
                    model.images += 1

        model.hyperlinks = [
            v for k, v in rels.items()
            if v.startswith("http") or v.startswith("mailto:")
        ]
        model.numbering_ids = {p.num_id for p in model.paragraphs if p.num_id}

        for name in sorted(names):
            if re.match(r"word/header\d*\.xml$", name):
                model.header_texts.append(_part_text(zf, name))
            elif re.match(r"word/footer\d*\.xml$", name):
                model.footer_texts.append(_part_text(zf, name))

        model.metadata = _read_metadata(zf, names)

    if not model.sections:
        model.warnings.append("no sectPr found; page setup could not be read")
    return model


def _part_text(zf: zipfile.ZipFile, name: str) -> str:
    try:
        root = ET.fromstring(zf.read(name))
    except (ET.ParseError, KeyError):
        return ""
    return " ".join(t.text or "" for t in root.iter(f"{W}t")).strip()


def _read_metadata(zf: zipfile.ZipFile, names: Set[str]) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if "docProps/core.xml" in names:
        try:
            root = ET.fromstring(zf.read("docProps/core.xml"))
            for tag, key in ((f"{DC}creator", "author"), (f"{DC}title", "title"),
                             (f"{DC}subject", "subject"), (f"{CP}lastModifiedBy", "last_modified_by")):
                el = root.find(tag)
                if el is not None and el.text:
                    meta[key] = el.text.strip()
        except ET.ParseError:
            pass
    if "docProps/app.xml" in names:
        try:
            root = ET.fromstring(zf.read("docProps/app.xml"))
            for tag, key in ((f"{EP}Pages", "pages"), (f"{EP}Words", "words"),
                             (f"{EP}Application", "application")):
                el = root.find(tag)
                if el is not None and el.text:
                    meta[key] = el.text.strip()
        except ET.ParseError:
            pass
    return meta
