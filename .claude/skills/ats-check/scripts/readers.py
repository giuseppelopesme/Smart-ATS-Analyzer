"""Load a resume from PDF, DOCX, TXT or MD into one shape the linter can use.

Everything here is standard library only. DOCX is a ZIP of XML, so it needs
no more than ``zipfile`` and the XML parser; PDF goes through ``pdfmini``.
"""

from __future__ import annotations

import html
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import layout as layout_mod
import pdfmini

__all__ = ["Extraction", "PageView", "load_any", "UnsupportedFormat"]


class UnsupportedFormat(Exception):
    pass


@dataclass
class PageView:
    number: int
    naive_text: str
    layout_text: str
    multi_column: bool = False
    gutters: List[Any] = field(default_factory=list)
    n_spans: int = 0
    n_images: int = 0
    image_area_ratio: float = 0.0
    unmapped_glyphs: int = 0
    total_glyphs: int = 0
    invisible_chars: int = 0
    body_size: float = 0.0
    min_body_size: float = 0.0
    width: float = 0.0
    height: float = 0.0
    link_uris: List[str] = field(default_factory=list)
    top_band_text: str = ""
    bottom_band_text: str = ""


@dataclass
class Extraction:
    path: str
    kind: str
    text: str  # best-effort reading order (layout aware)
    naive_text: str  # content-stream order
    pages: List[PageView] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    fonts: List[Dict[str, Any]] = field(default_factory=list)
    encrypted: bool = False
    # DOCX-specific structures an ATS may mishandle.
    tables: int = 0
    textboxes: int = 0
    header_footer_text: str = ""

    @property
    def word_count(self) -> int:
        return len(re.findall(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9'’\-]*", self.text))


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


def _load_pdf(path: str, max_pages: int = 30) -> Extraction:
    with open(path, "rb") as fh:
        data = fh.read()
    doc = pdfmini.Document(data)
    doc.load_pages(max_pages=max_pages)

    pages: List[PageView] = []
    naive_all: List[str] = []
    layout_all: List[str] = []

    for page in doc.pages:
        lay = layout_mod.analyse_page(page)
        n_text = layout_mod.naive_text(page)
        l_text = layout_mod.layout_text(lay)
        naive_all.append(n_text)
        layout_all.append(l_text)

        page_area = max(page.width * page.height, 1.0)
        img_area = sum(max(i.draw_w, 0) * max(i.draw_h, 0) for i in page.images)
        sizes = [s.size for s in page.spans if s.text.strip() and s.size > 0]

        # Text drawn in the top / bottom 6% of the sheet is header/footer
        # territory -- some parsers drop those bands wholesale.
        top_cut, bot_cut = page.height * 0.06, page.height * 0.94
        top_txt = " ".join(s.text for s in page.spans if s.y <= top_cut and s.text.strip())
        bot_txt = " ".join(s.text for s in page.spans if s.y >= bot_cut and s.text.strip())

        pages.append(
            PageView(
                number=page.number,
                naive_text=n_text,
                layout_text=l_text,
                multi_column=lay.multi_column,
                gutters=[{"x0": round(g[0], 1), "x1": round(g[1], 1), "coverage": g[2]} for g in lay.gutters],
                n_spans=len(page.spans),
                n_images=len(page.images),
                image_area_ratio=round(min(img_area / page_area, 4.0), 3),
                unmapped_glyphs=sum(s.unmapped for s in page.spans),
                total_glyphs=sum(s.glyphs for s in page.spans),
                invisible_chars=sum(len(s.text) for s in page.spans if s.invisible),
                body_size=round(lay.body_size, 1),
                min_body_size=round(min(sizes), 1) if sizes else 0.0,
                width=round(page.width, 1),
                height=round(page.height, 1),
                link_uris=list(dict.fromkeys(page.annots_uri))[:40],
                top_band_text=top_txt[:400],
                bottom_band_text=bot_txt[:400],
            )
        )

    fonts = [
        {
            "name": f.family or f.key,
            "subtype": f.subtype,
            "embedded": f.embedded,
            "symbolic": f.symbolic,
            "has_tounicode": f.has_tounicode,
            "two_byte": f.two_byte,
        }
        for f in doc.fonts.values()
    ]
    # De-duplicate by family+subtype.
    seen = set()
    uniq_fonts = []
    for f in fonts:
        key = (f["name"], f["subtype"])
        if key in seen:
            continue
        seen.add(key)
        uniq_fonts.append(f)

    return Extraction(
        path=path,
        kind="pdf",
        text="\n\n".join(layout_all).strip(),
        naive_text="\n\n".join(naive_all).strip(),
        pages=pages,
        meta={"page_count": len(doc.pages), "producer": _pdf_producer(doc)},
        warnings=list(doc.warnings),
        fonts=uniq_fonts,
        encrypted=doc.encrypted,
    )


def _pdf_producer(doc: pdfmini.Document) -> str:
    info = doc.resolve(doc.trailer.get("Info"))
    if not isinstance(info, dict):
        return ""
    bits = []
    for key in ("Producer", "Creator"):
        v = doc.resolve(info.get(key))
        if isinstance(v, bytes):
            try:
                v = v.decode("utf-16" if v[:2] in (b"\xfe\xff", b"\xff\xfe") else "latin-1", "replace")
            except Exception:
                v = ""
        if isinstance(v, str) and v.strip():
            bits.append(v.strip())
    return " / ".join(dict.fromkeys(bits))[:200]


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_para_text(node: ET.Element) -> str:
    parts: List[str] = []
    for el in node.iter():
        tag = el.tag
        if tag == W_NS + "t":
            parts.append(el.text or "")
        elif tag == W_NS + "tab":
            parts.append("\t")
        elif tag in (W_NS + "br", W_NS + "cr"):
            parts.append("\n")
    return "".join(parts)


def _load_docx(path: str) -> Extraction:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise UnsupportedFormat(f"not a readable .docx (bad zip): {exc}") from exc

    warnings: List[str] = []
    with zf:
        names = set(zf.namelist())
        if "word/document.xml" not in names:
            raise UnsupportedFormat("missing word/document.xml -- is this really a .docx?")
        try:
            root = ET.fromstring(zf.read("word/document.xml"))
        except ET.ParseError as exc:
            raise UnsupportedFormat(f"document.xml is not valid XML: {exc}") from exc

        body = root.find(W_NS + "body")
        blocks: List[str] = []
        tables = 0
        if body is not None:
            for child in body:
                if child.tag == W_NS + "p":
                    blocks.append(_docx_para_text(child))
                elif child.tag == W_NS + "tbl":
                    tables += 1
                    for row in child.iter(W_NS + "tr"):
                        cells = [
                            " ".join(_docx_para_text(p).strip() for p in cell.iter(W_NS + "p")).strip()
                            for cell in row.findall(W_NS + "tc")
                        ]
                        blocks.append(" \t ".join(c for c in cells if c))

        # Text boxes live outside the main flow and are frequently dropped.
        textboxes = 0
        for el in root.iter():
            if el.tag.endswith("}txbxContent"):
                textboxes += 1

        hf_parts: List[str] = []
        for name in sorted(names):
            if re.match(r"word/(header|footer)\d*\.xml$", name):
                try:
                    hf_root = ET.fromstring(zf.read(name))
                except ET.ParseError:
                    continue
                text = " ".join(
                    _docx_para_text(p).strip() for p in hf_root.iter(W_NS + "p")
                ).strip()
                if text:
                    hf_parts.append(text)

        meta: Dict[str, Any] = {}
        if "docProps/app.xml" in names:
            try:
                app = ET.fromstring(zf.read("docProps/app.xml"))
                for tag in ("Pages", "Words", "Application"):
                    el = next((e for e in app.iter() if e.tag.endswith("}" + tag)), None)
                    if el is not None and el.text:
                        meta[tag.lower()] = el.text.strip()
            except ET.ParseError:
                pass

    text = "\n".join(b for b in blocks)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if textboxes:
        warnings.append(f"{textboxes} text box(es) found; their content is outside the main document flow")

    page = PageView(number=1, naive_text=text, layout_text=text, n_spans=len(blocks))
    return Extraction(
        path=path,
        kind="docx",
        text=text,
        naive_text=text,
        pages=[page],
        meta=meta,
        warnings=warnings,
        tables=tables,
        textboxes=textboxes,
        header_footer_text=" ".join(hf_parts)[:600],
    )


# --------------------------------------------------------------------------
# Plain text
# --------------------------------------------------------------------------


def _load_text(path: str) -> Extraction:
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin-1", "replace")
    text = html.unescape(text) if path.lower().endswith((".html", ".htm")) else text
    if path.lower().endswith((".html", ".htm")):
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    kind = "markdown" if path.lower().endswith((".md", ".markdown")) else "text"
    return Extraction(
        path=path,
        kind=kind,
        text=text,
        naive_text=text,
        pages=[PageView(number=1, naive_text=text, layout_text=text)],
    )


def load_any(path: str, max_pages: int = 30) -> Extraction:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _load_pdf(path, max_pages=max_pages)
    if ext in (".docx", ".dotx", ".docm"):
        return _load_docx(path)
    if ext in (".txt", ".md", ".markdown", ".html", ".htm", ".rtf"):
        if ext == ".rtf":
            ex = _load_text(path)
            ex.kind = "rtf"
            ex.text = _strip_rtf(ex.text)
            ex.naive_text = ex.text
            ex.pages = [PageView(number=1, naive_text=ex.text, layout_text=ex.text)]
            return ex
        return _load_text(path)
    if ext == ".doc":
        raise UnsupportedFormat(
            "legacy .doc (Word 97) is not supported -- and most ATS handle it poorly too. "
            "Re-save as .docx or PDF."
        )
    if ext == ".pages":
        raise UnsupportedFormat(
            "Apple Pages files are not readable by any ATS. Export to PDF or .docx."
        )
    # Sniff the magic bytes before giving up.
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head.startswith(b"%PDF"):
        return _load_pdf(path, max_pages=max_pages)
    if head.startswith(b"PK\x03\x04"):
        return _load_docx(path)
    raise UnsupportedFormat(f"unrecognised resume format: {ext or 'no extension'}")


def _strip_rtf(text: str) -> str:
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r"\\par[d]?\b", "\n", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d*\s?", " ", text)
    text = text.replace("{", " ").replace("}", " ")
    return re.sub(r"[ \t]{2,}", " ", text).strip()
