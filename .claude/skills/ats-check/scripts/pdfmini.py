"""Minimal dependency-free PDF reader, tuned for resume parseability analysis.

Only the standard library is used, so this runs anywhere Python 3.9+ runs --
including sandboxes with no network access and no pip.

What it exposes, and why an ATS cares:

* ``Document.pages`` -- page geometry, so we can tell a column split from a
  wide indent.
* ``Page.spans`` -- every show-text operation with its position on the page.
  ATS parsers read a PDF by walking these in content-stream order; recovering
  the same list is what lets us predict how the resume will be linearised.
* ``Page.images`` -- image XObjects. A resume that is mostly image is a resume
  the ATS reads as blank.
* ``Document.fonts`` -- embedding and ToUnicode status. A non-embedded symbolic
  font, or a Type0 font with no ToUnicode map, extracts as garbage.

The parser is deliberately forgiving: a resume exported by a word processor is
usually well-formed, but the interesting failures are the malformed ones, so
every stage falls back rather than raising.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "Document",
    "Page",
    "Span",
    "ImageRef",
    "FontInfo",
    "PdfError",
    "Name",
    "Ref",
]

WHITESPACE = b"\x00\t\n\x0c\r "
DELIMITERS = b"()<>[]{}/%"


class PdfError(Exception):
    """Raised only when a file cannot be treated as a PDF at all."""


class Name(str):
    """A PDF name object (``/Foo``). Subclasses str so it compares cleanly."""

    __slots__ = ()


@dataclass(frozen=True)
class Ref:
    """An indirect reference (``12 0 R``)."""

    num: int
    gen: int = 0


@dataclass
class Stream:
    dict: Dict[str, Any]
    raw: bytes
    _doc: Optional["Document"] = None
    _decoded: Optional[bytes] = None

    def data(self) -> bytes:
        if self._decoded is None:
            self._decoded = _apply_filters(self.dict, self.raw, self._doc)
        return self._decoded


# --------------------------------------------------------------------------
# Lexer / object parser
# --------------------------------------------------------------------------


class Lexer:
    """Tokenises PDF syntax out of a byte buffer."""

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def skip_ws(self) -> None:
        data, n = self.data, len(self.data)
        while self.pos < n:
            c = data[self.pos]
            if c in WHITESPACE:
                self.pos += 1
            elif c == 0x25:  # '%' comment runs to end of line
                while self.pos < n and data[self.pos] not in b"\r\n":
                    self.pos += 1
            else:
                return

    def peek_byte(self) -> int:
        return self.data[self.pos] if self.pos < len(self.data) else -1

    def read_token(self) -> Optional[bytes]:
        """Read a bare keyword/number token."""
        self.skip_ws()
        start = self.pos
        data, n = self.data, len(self.data)
        while self.pos < n and data[self.pos] not in WHITESPACE and data[self.pos] not in DELIMITERS:
            self.pos += 1
        if self.pos == start:
            return None
        return data[start : self.pos]

    def read_name(self) -> Name:
        assert self.data[self.pos] == 0x2F
        self.pos += 1
        start = self.pos
        data, n = self.data, len(self.data)
        while self.pos < n and data[self.pos] not in WHITESPACE and data[self.pos] not in DELIMITERS:
            self.pos += 1
        raw = data[start : self.pos]
        if b"#" in raw:
            out = bytearray()
            i = 0
            while i < len(raw):
                if raw[i] == 0x23 and i + 2 < len(raw):
                    try:
                        out.append(int(raw[i + 1 : i + 3], 16))
                        i += 3
                        continue
                    except ValueError:
                        pass
                out.append(raw[i])
                i += 1
            raw = bytes(out)
        return Name(raw.decode("latin-1"))

    def read_literal_string(self) -> bytes:
        assert self.data[self.pos] == 0x28
        self.pos += 1
        out = bytearray()
        depth = 1
        data, n = self.data, len(self.data)
        while self.pos < n:
            c = data[self.pos]
            if c == 0x5C:  # backslash
                self.pos += 1
                if self.pos >= n:
                    break
                e = data[self.pos]
                simple = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}
                if e in simple:
                    out.append(simple[e])
                    self.pos += 1
                elif e in b"()\\":
                    out.append(e)
                    self.pos += 1
                elif 0x30 <= e <= 0x37:  # octal, up to 3 digits
                    digits = bytearray()
                    while self.pos < n and len(digits) < 3 and 0x30 <= data[self.pos] <= 0x37:
                        digits.append(data[self.pos])
                        self.pos += 1
                    out.append(int(digits, 8) & 0xFF)
                elif e in b"\r\n":  # line continuation
                    self.pos += 1
                    if e == 0x0D and self.pos < n and data[self.pos] == 0x0A:
                        self.pos += 1
                else:
                    out.append(e)
                    self.pos += 1
                continue
            if c == 0x28:
                depth += 1
            elif c == 0x29:
                depth -= 1
                if depth == 0:
                    self.pos += 1
                    break
            out.append(c)
            self.pos += 1
        return bytes(out)

    def read_hex_string(self) -> bytes:
        assert self.data[self.pos] == 0x3C
        self.pos += 1
        digits = bytearray()
        data, n = self.data, len(self.data)
        while self.pos < n and data[self.pos] != 0x3E:
            c = data[self.pos]
            if c not in WHITESPACE:
                digits.append(c)
            self.pos += 1
        self.pos += 1  # consume '>'
        if len(digits) % 2:
            digits.append(0x30)  # odd digit count: pad with '0'
        try:
            return bytes.fromhex(digits.decode("latin-1"))
        except ValueError:
            cleaned = bytes(c for c in digits if c in b"0123456789abcdefABCDEF")
            if len(cleaned) % 2:
                cleaned += b"0"
            return bytes.fromhex(cleaned.decode("latin-1"))

    def parse_object(self, doc: Optional["Document"] = None) -> Any:
        self.skip_ws()
        if self.pos >= len(self.data):
            return None
        c = self.data[self.pos]

        if c == 0x2F:
            return self.read_name()
        if c == 0x28:
            return self.read_literal_string()
        if c == 0x3C:
            if self.pos + 1 < len(self.data) and self.data[self.pos + 1] == 0x3C:
                return self._parse_dict_or_stream(doc)
            return self.read_hex_string()
        if c == 0x5B:  # '['
            self.pos += 1
            arr: List[Any] = []
            while True:
                self.skip_ws()
                if self.pos >= len(self.data):
                    break
                if self.data[self.pos] == 0x5D:
                    self.pos += 1
                    break
                before = self.pos
                arr.append(self.parse_object(doc))
                if self.pos == before:  # no progress: bail out
                    self.pos += 1
            return arr
        if c == 0x5D or c == 0x3E or c == 0x7D or c == 0x29:
            # Stray closing delimiter -- skip it.
            self.pos += 1
            return None
        if c == 0x7B:  # '{' PostScript function body; treat as array
            self.pos += 1
            return None

        tok = self.read_token()
        if tok is None:
            self.pos += 1
            return None
        value = _atom(tok)
        # An integer may begin an indirect reference (`12 0 R`). Look ahead
        # for the generation number and the `R`, and rewind if it is not one.
        # Doing this here means refs fold correctly in every position --
        # array elements, dictionary values and bare top-level objects alike.
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            save = self.pos
            gen_tok = self.read_token()
            if gen_tok is not None and _NUM_RE.match(gen_tok) and b"." not in gen_tok:
                r_tok = self.read_token()
                if r_tok == b"R":
                    try:
                        return Ref(value, int(gen_tok))
                    except ValueError:
                        pass
            self.pos = save
        return value

    def _parse_dict_or_stream(self, doc: Optional["Document"]) -> Any:
        self.pos += 2  # '<<'
        d: Dict[str, Any] = {}
        while True:
            self.skip_ws()
            if self.pos >= len(self.data):
                break
            if self.data[self.pos] == 0x3E:
                if self.pos + 1 < len(self.data) and self.data[self.pos + 1] == 0x3E:
                    self.pos += 2
                    break
                self.pos += 1
                continue
            if self.data[self.pos] != 0x2F:
                # Malformed key -- skip a value and keep going.
                before = self.pos
                self.parse_object(doc)
                if self.pos == before:
                    self.pos += 1
                continue
            key = self.read_name()
            value = self.parse_object(doc)
            d[str(key)] = value

        # A stream keyword may follow the dictionary.
        save = self.pos
        self.skip_ws()
        if self.data[self.pos : self.pos + 6] == b"stream":
            self.pos += 6
            if self.data[self.pos : self.pos + 2] == b"\r\n":
                self.pos += 2
            elif self.pos < len(self.data) and self.data[self.pos] in b"\n\r":
                self.pos += 1
            start = self.pos
            length = d.get("Length")
            if isinstance(length, Ref) and doc is not None:
                length = doc.resolve(length)
            end = -1
            if isinstance(length, int) and length >= 0 and start + length <= len(self.data):
                end = start + length
                tail = self.data[end : end + 20]
                # Trust /Length only if 'endstream' actually follows it.
                if b"endstream" not in tail:
                    end = -1
            if end < 0:
                idx = self.data.find(b"endstream", start)
                end = idx if idx >= 0 else len(self.data)
                while end > start and self.data[end - 1] in b"\r\n":
                    end -= 1
            raw = self.data[start:end]
            idx = self.data.find(b"endstream", end)
            self.pos = (idx + 9) if idx >= 0 else end
            return Stream(dict=d, raw=raw, _doc=doc)
        self.pos = save
        return d


_NUM_RE = re.compile(rb"^[+-]?(\d+\.?\d*|\.\d+)$")


def _atom(tok: bytes) -> Any:
    if tok == b"true":
        return True
    if tok == b"false":
        return False
    if tok == b"null":
        return None
    if _NUM_RE.match(tok):
        text = tok.decode("latin-1")
        if "." in text:
            try:
                return float(text)
            except ValueError:
                return 0.0
        try:
            return int(text)
        except ValueError:
            return 0
    # Tolerate broken reals such as '--3' or '3.4.5'.
    if re.match(rb"^[+-.\d]+$", tok):
        cleaned = re.sub(rb"[^0-9.\-]", b"", tok)
        try:
            return float(cleaned) if b"." in cleaned else int(cleaned or b"0")
        except ValueError:
            return 0
    return Keyword(tok.decode("latin-1"))


class Keyword(str):
    __slots__ = ()


# --------------------------------------------------------------------------
# Stream filters
# --------------------------------------------------------------------------


def _apply_png_predictor(data: bytes, colors: int, bpc: int, columns: int) -> bytes:
    bpp = max(1, (colors * bpc + 7) // 8)
    row_len = (columns * colors * bpc + 7) // 8
    out = bytearray()
    prev = bytearray(row_len)
    i = 0
    n = len(data)
    while i + 1 <= n - 1:
        ft = data[i]
        i += 1
        row = bytearray(data[i : i + row_len])
        if len(row) < row_len:
            row.extend(b"\x00" * (row_len - len(row)))
        i += row_len
        if ft == 1:  # Sub
            for j in range(bpp, row_len):
                row[j] = (row[j] + row[j - bpp]) & 0xFF
        elif ft == 2:  # Up
            for j in range(row_len):
                row[j] = (row[j] + prev[j]) & 0xFF
        elif ft == 3:  # Average
            for j in range(row_len):
                left = row[j - bpp] if j >= bpp else 0
                row[j] = (row[j] + ((left + prev[j]) >> 1)) & 0xFF
        elif ft == 4:  # Paeth
            for j in range(row_len):
                a = row[j - bpp] if j >= bpp else 0
                b = prev[j]
                c = prev[j - bpp] if j >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[j] = (row[j] + pred) & 0xFF
        out.extend(row)
        prev = row
    return bytes(out)


def _lzw_decode(data: bytes, early: int = 1) -> bytes:
    out = bytearray()
    table: List[bytes] = [bytes([i]) for i in range(256)] + [b"", b""]
    bits, code_len, prev = 0, 9, None
    buf = 0
    for byte in data:
        buf = (buf << 8) | byte
        bits += 8
        while bits >= code_len:
            code = (buf >> (bits - code_len)) & ((1 << code_len) - 1)
            bits -= code_len
            if code == 256:
                table = [bytes([i]) for i in range(256)] + [b"", b""]
                code_len, prev = 9, None
                continue
            if code == 257:
                return bytes(out)
            if prev is None:
                entry = table[code]
            elif code < len(table):
                entry = table[code]
                table.append(prev + entry[:1])
            else:
                entry = prev + prev[:1]
                table.append(entry)
            out.extend(entry)
            prev = entry
            if len(table) + early >= (1 << code_len) and code_len < 12:
                code_len += 1
    return bytes(out)


def _ascii85_decode(data: bytes) -> bytes:
    data = re.sub(rb"\s", b"", data)
    if data.startswith(b"<~"):
        data = data[2:]
    idx = data.find(b"~>")
    if idx >= 0:
        data = data[:idx]
    try:
        import base64

        return base64.a85decode(data)
    except Exception:
        return b""


def _asciihex_decode(data: bytes) -> bytes:
    idx = data.find(b">")
    if idx >= 0:
        data = data[:idx]
    digits = bytes(c for c in data if c in b"0123456789abcdefABCDEF")
    if len(digits) % 2:
        digits += b"0"
    try:
        return bytes.fromhex(digits.decode("latin-1"))
    except ValueError:
        return b""


def _runlength_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        length = data[i]
        i += 1
        if length == 128:
            break
        if length < 128:
            out.extend(data[i : i + length + 1])
            i += length + 1
        else:
            if i < len(data):
                out.extend(bytes([data[i]]) * (257 - length))
            i += 1
    return bytes(out)


IMAGE_FILTERS = {"DCTDecode", "JPXDecode", "JBIG2Decode", "CCITTFaxDecode"}


def _apply_filters(d: Dict[str, Any], raw: bytes, doc: Optional["Document"]) -> bytes:
    def rz(v: Any) -> Any:
        return doc.resolve(v) if doc is not None else v

    filters = rz(d.get("Filter"))
    if filters is None:
        return raw
    if not isinstance(filters, list):
        filters = [filters]
    parms = rz(d.get("DecodeParms")) or rz(d.get("DP"))
    if not isinstance(parms, list):
        parms = [parms] * len(filters)
    while len(parms) < len(filters):
        parms.append(None)

    data = raw
    for filt, parm in zip(filters, parms):
        filt = str(rz(filt) or "")
        parm = rz(parm) or {}
        if not isinstance(parm, dict):
            parm = {}
        if filt in ("FlateDecode", "Fl"):
            data = _inflate(data)
        elif filt in ("LZWDecode", "LZW"):
            data = _lzw_decode(data, int(rz(parm.get("EarlyChange", 1)) or 0))
        elif filt in ("ASCII85Decode", "A85"):
            data = _ascii85_decode(data)
            continue
        elif filt in ("ASCIIHexDecode", "AHx"):
            data = _asciihex_decode(data)
            continue
        elif filt in ("RunLengthDecode", "RL"):
            data = _runlength_decode(data)
            continue
        elif filt in IMAGE_FILTERS:
            # Compressed pixels; we only care that the object exists.
            return data
        else:
            continue

        pred = int(rz(parm.get("Predictor", 1)) or 1)
        if pred >= 10:
            data = _apply_png_predictor(
                data,
                int(rz(parm.get("Colors", 1)) or 1),
                int(rz(parm.get("BitsPerComponent", 8)) or 8),
                int(rz(parm.get("Columns", 1)) or 1),
            )
        elif pred == 2:
            colors = int(rz(parm.get("Colors", 1)) or 1)
            columns = int(rz(parm.get("Columns", 1)) or 1)
            if int(rz(parm.get("BitsPerComponent", 8)) or 8) == 8:
                buf = bytearray(data)
                row_len = colors * columns
                for r in range(0, len(buf) - row_len + 1, row_len):
                    for j in range(colors, row_len):
                        buf[r + j] = (buf[r + j] + buf[r + j - colors]) & 0xFF
                data = bytes(buf)
    return data


def _inflate(data: bytes) -> bytes:
    """zlib inflate that tolerates the truncated streams real PDFs contain."""
    for wbits in (15, -15, 47):
        try:
            return zlib.decompress(data, wbits)
        except zlib.error:
            try:
                obj = zlib.decompressobj(wbits)
                out = obj.decompress(data)
                out += obj.flush()
                if out:
                    return out
            except zlib.error:
                continue
    # Some writers prepend junk before the zlib header.
    idx = data.find(b"\x78")
    if 0 < idx < 32:
        try:
            return zlib.decompressobj().decompress(data[idx:])
        except zlib.error:
            pass
    return b""


# --------------------------------------------------------------------------
# Encodings
# --------------------------------------------------------------------------

# WinAnsiEncoding differs from Latin-1 only in 0x80-0x9F; that range carries
# the smart quotes, en/em dashes and bullets that word processors emit, and
# mis-decoding them is a classic source of mojibake in extracted resume text.
_WINANSI_HIGH = {
    0x80: "€", 0x82: "‚", 0x83: "ƒ", 0x84: "„",
    0x85: "…", 0x86: "†", 0x87: "‡", 0x88: "ˆ",
    0x89: "‰", 0x8A: "Š", 0x8B: "‹", 0x8C: "Œ",
    0x8E: "Ž", 0x91: "‘", 0x92: "’", 0x93: "“",
    0x94: "”", 0x95: "•", 0x96: "–", 0x97: "—",
    0x98: "˜", 0x99: "™", 0x9A: "š", 0x9B: "›",
    0x9C: "œ", 0x9E: "ž", 0x9F: "Ÿ",
}

# Glyph names that show up in resume PDFs often enough to be worth mapping.
_GLYPH_NAMES = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#",
    "dollar": "$", "percent": "%", "ampersand": "&", "quotesingle": "'",
    "parenleft": "(", "parenright": ")", "asterisk": "*", "plus": "+",
    "comma": ",", "hyphen": "-", "period": ".", "slash": "/",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "colon": ":", "semicolon": ";", "less": "<", "equal": "=",
    "greater": ">", "question": "?", "at": "@", "bracketleft": "[",
    "backslash": "\\", "bracketright": "]", "asciicircum": "^",
    "underscore": "_", "grave": "`", "braceleft": "{", "bar": "|",
    "braceright": "}", "asciitilde": "~", "bullet": "•",
    "endash": "–", "emdash": "—", "quoteleft": "‘",
    "quoteright": "’", "quotedblleft": "“",
    "quotedblright": "”", "ellipsis": "…", "fi": "fi",
    "fl": "fl", "ff": "ff", "ffi": "ffi", "ffl": "ffl",
    "middot": "·", "periodcentered": "·", "degree": "°",
    "eacute": "é", "egrave": "è", "agrave": "à",
    "ccedilla": "ç", "uuml": "ü", "ouml": "ö",
    "auml": "ä", "ntilde": "ñ", "nbspace": " ",
}


def _glyphname_to_unicode(name: str) -> Optional[str]:
    if name in _GLYPH_NAMES:
        return _GLYPH_NAMES[name]
    m = re.match(r"^uni([0-9A-Fa-f]{4,6})$", name)
    if m:
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return None
    m = re.match(r"^u([0-9A-Fa-f]{4,6})$", name)
    if m:
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return None
    # gXX / cidXX / index-style names carry no Unicode meaning at all.
    return None


def _byte_to_unicode(code: int, base: str) -> str:
    if base == "WinAnsiEncoding" and code in _WINANSI_HIGH:
        return _WINANSI_HIGH[code]
    if 0x20 <= code < 0x7F:
        return chr(code)
    if code >= 0xA0:
        return chr(code)
    if base == "MacRomanEncoding" and code >= 0x80:
        return "�"
    return "�" if code not in (9, 10, 13, 32) else " "


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------


@dataclass
class FontInfo:
    key: str
    base_font: str = ""
    subtype: str = ""
    embedded: bool = False
    symbolic: bool = False
    has_tounicode: bool = False
    two_byte: bool = False
    encoding_name: str = ""
    to_unicode: Dict[int, str] = field(default_factory=dict)
    differences: Dict[int, str] = field(default_factory=dict)
    codespace_1byte: bool = True

    @property
    def family(self) -> str:
        """BaseFont minus the ``ABCDEF+`` subset tag."""
        name = self.base_font
        if len(name) > 7 and name[6] == "+":
            name = name[7:]
        return name

    def decode(self, raw: bytes) -> Tuple[str, int, int]:
        """Decode a PDF string to text. Returns (text, glyphs, unmapped)."""
        out: List[str] = []
        unmapped = 0
        if self.two_byte:
            codes: Iterable[int] = [
                (raw[i] << 8) | (raw[i + 1] if i + 1 < len(raw) else 0)
                for i in range(0, len(raw), 2)
            ]
        else:
            codes = list(raw)
        total = 0
        for code in codes:
            total += 1
            if self.to_unicode:
                ch = self.to_unicode.get(code)
                if ch is not None:
                    out.append(ch)
                    if ch == "�":
                        unmapped += 1
                    continue
            if not self.two_byte and code in self.differences:
                ch = _glyphname_to_unicode(self.differences[code])
                if ch is not None:
                    out.append(ch)
                    continue
                out.append("�")
                unmapped += 1
                continue
            if self.two_byte:
                # Type0 with no usable ToUnicode: the code is a CID, which
                # carries no Unicode meaning. This is the "extracts as
                # garbage" case an ATS hits.
                out.append("�")
                unmapped += 1
                continue
            ch = _byte_to_unicode(code, self.encoding_name)
            if ch == "�":
                unmapped += 1
            out.append(ch)
        return "".join(out), total, unmapped


def _parse_tounicode(data: bytes) -> Tuple[Dict[int, str], bool]:
    """Parse a ToUnicode CMap. Returns (map, saw_2byte_codespace)."""
    mapping: Dict[int, str] = {}
    two_byte = False

    for m in re.finditer(rb"begincodespacerange(.*?)endcodespacerange", data, re.S):
        for lo, _hi in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", m.group(1)):
            if len(lo) >= 4:
                two_byte = True

    def to_text(hexstr: bytes) -> str:
        try:
            b = bytes.fromhex(hexstr.decode("latin-1"))
        except ValueError:
            return "�"
        if len(b) % 2:
            b += b"\x00"
        try:
            text = b.decode("utf-16-be")
        except UnicodeDecodeError:
            return "�"
        # Strip the trailing NULs some writers pad destinations with.
        return text.replace("\x00", "")

    for m in re.finditer(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", m.group(1)):
            try:
                code = int(src, 16)
            except ValueError:
                continue
            if len(src) >= 4:
                two_byte = True
            mapping[code] = to_text(dst) if dst else "�"
        # bfchar destinations may also be glyph names.
        for src, name in re.findall(rb"<([0-9A-Fa-f]+)>\s*/([^\s/<\]]+)", m.group(1)):
            try:
                code = int(src, 16)
            except ValueError:
                continue
            ch = _glyphname_to_unicode(name.decode("latin-1"))
            mapping[code] = ch if ch is not None else "�"

    for m in re.finditer(rb"beginbfrange(.*?)endbfrange", data, re.S):
        body = m.group(1)
        # <lo> <hi> [<d1> <d2> ...]
        for lo, hi, arr in re.findall(
            rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", body, re.S
        ):
            try:
                start, end = int(lo, 16), int(hi, 16)
            except ValueError:
                continue
            if len(lo) >= 4:
                two_byte = True
            dsts = re.findall(rb"<([0-9A-Fa-f]*)>", arr)
            for offset, dst in enumerate(dsts):
                if start + offset > end:
                    break
                mapping[start + offset] = to_text(dst) if dst else "�"
        # <lo> <hi> <dststart>
        stripped = re.sub(rb"<[0-9A-Fa-f]+>\s*<[0-9A-Fa-f]+>\s*\[.*?\]", b" ", body, flags=re.S)
        for lo, hi, dst in re.findall(
            rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", stripped
        ):
            try:
                start, end = int(lo, 16), int(hi, 16)
            except ValueError:
                continue
            if len(lo) >= 4:
                two_byte = True
            base_text = to_text(dst)
            if not base_text:
                continue
            if end - start > 65535:
                end = start + 65535
            prefix, last = base_text[:-1], base_text[-1]
            for offset in range(end - start + 1):
                try:
                    mapping[start + offset] = prefix + chr(ord(last) + offset)
                except ValueError:
                    break
    return mapping, two_byte


# --------------------------------------------------------------------------
# Page content
# --------------------------------------------------------------------------


@dataclass
class Span:
    """One show-text operation, placed on the page."""

    text: str
    x: float
    y: float
    width: float
    size: float
    font: str
    font_family: str
    glyphs: int = 0
    unmapped: int = 0
    render_mode: int = 0
    order: int = 0

    @property
    def x1(self) -> float:
        return self.x + self.width

    @property
    def invisible(self) -> bool:
        # Tr 3 is the OCR-under-image trick; Tr 7 is clip-only.
        return self.render_mode in (3, 7)


@dataclass
class ImageRef:
    name: str
    width: int
    height: int
    x: float
    y: float
    draw_w: float
    draw_h: float
    is_mask: bool = False


@dataclass
class Page:
    number: int
    width: float
    height: float
    spans: List[Span] = field(default_factory=list)
    images: List[ImageRef] = field(default_factory=list)
    rotate: int = 0
    annots_uri: List[str] = field(default_factory=list)
    content_ops: int = 0
    truncated: bool = False


def _mat_mul(a: Sequence[float], b: Sequence[float]) -> Tuple[float, ...]:
    a0, a1, a2, a3, a4, a5 = a
    b0, b1, b2, b3, b4, b5 = b
    return (
        a0 * b0 + a1 * b2,
        a0 * b1 + a1 * b3,
        a2 * b0 + a3 * b2,
        a2 * b1 + a3 * b3,
        a4 * b0 + a5 * b2 + b4,
        a4 * b1 + a5 * b3 + b5,
    )


IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


class _ContentInterpreter:
    """Walks a content stream and records placed text and images.

    This mirrors what a text-extracting ATS does: replay the drawing
    operators, keep the text matrix up to date, and emit each string with the
    position it lands at.
    """

    MAX_OPS = 400_000

    def __init__(self, doc: "Document", page: Page, resources: Dict[str, Any]):
        self.doc = doc
        self.page = page
        self.order = 0
        self.depth = 0
        self.ops = 0

    def run(self, data: bytes, resources: Dict[str, Any], ctm: Sequence[float]) -> None:
        if self.depth > 8:
            return
        self.depth += 1
        try:
            self._run(data, resources, ctm)
        finally:
            self.depth -= 1

    def _run(self, data: bytes, resources: Dict[str, Any], ctm: Sequence[float]) -> None:
        doc = self.doc
        lex = Lexer(data)
        stack: List[Any] = []
        gs_stack: List[Tuple[Any, ...]] = []

        cur_ctm = tuple(ctm)
        tm = IDENTITY
        tlm = IDENTITY
        font: Optional[FontInfo] = None
        size = 0.0
        leading = 0.0
        charspace = 0.0
        wordspace = 0.0
        hscale = 1.0
        rise = 0.0
        render_mode = 0

        fonts_res = doc.resolve(resources.get("Font")) or {}
        xobj_res = doc.resolve(resources.get("XObject")) or {}

        def show(raw: bytes) -> None:
            nonlocal tm
            if not raw:
                return
            if font is None:
                text, glyphs, unmapped = raw.decode("latin-1", "replace"), len(raw), 0
                fam, key = "", ""
            else:
                text, glyphs, unmapped = font.decode(raw)
                fam, key = font.family, font.key
            trm = _mat_mul((size * hscale, 0, 0, size, 0, rise), _mat_mul(tm, cur_ctm))
            eff_size = abs(size) * ((cur_ctm[0] ** 2 + cur_ctm[1] ** 2) ** 0.5 or 1.0)
            # No glyph widths are parsed, so the advance is estimated at
            # 0.5em/char -- a good average for proportional text. It is only
            # used for column geometry and gap detection, never for layout
            # fidelity, so the error budget is generous.
            adv_text = (len(text) * 0.5 * abs(size) + charspace * len(text)) * hscale
            width = len(text) * 0.5 * eff_size * hscale
            self.order += 1
            self.page.spans.append(
                Span(
                    text=text,
                    x=trm[4],
                    y=trm[5],
                    width=width,
                    size=eff_size or abs(size),
                    font=key,
                    font_family=fam,
                    glyphs=glyphs,
                    unmapped=unmapped,
                    render_mode=render_mode,
                    order=self.order,
                )
            )
            tm = _mat_mul((1, 0, 0, 1, adv_text, 0), tm)

        while True:
            self.ops += 1
            if self.ops > self.MAX_OPS:
                self.page.truncated = True
                return
            lex.skip_ws()
            if lex.pos >= len(data):
                break
            before = lex.pos
            obj = lex.parse_object(doc)
            if lex.pos == before:
                lex.pos += 1
                continue
            if not isinstance(obj, Keyword):
                stack.append(obj)
                if len(stack) > 64:
                    del stack[:-32]
                continue

            op = str(obj)
            self.page.content_ops += 1

            def num(i: int, default: float = 0.0) -> float:
                try:
                    v = stack[i]
                    return float(v) if isinstance(v, (int, float)) else default
                except (IndexError, TypeError, ValueError):
                    return default

            if op == "q":
                gs_stack.append((cur_ctm, font, size, leading, charspace, wordspace, hscale, rise, render_mode))
            elif op == "Q":
                if gs_stack:
                    (cur_ctm, font, size, leading, charspace, wordspace, hscale, rise, render_mode) = gs_stack.pop()
            elif op == "cm" and len(stack) >= 6:
                cur_ctm = _mat_mul(tuple(num(i) for i in range(-6, 0)), cur_ctm)
            elif op == "BT":
                tm = tlm = IDENTITY
            elif op == "ET":
                tm = tlm = IDENTITY
            elif op == "Tf" and len(stack) >= 2:
                size = num(-1)
                fname = stack[-2]
                font = doc.font_for(fonts_res, str(fname) if fname is not None else "")
            elif op == "Td" and len(stack) >= 2:
                tlm = _mat_mul((1, 0, 0, 1, num(-2), num(-1)), tlm)
                tm = tlm
            elif op == "TD" and len(stack) >= 2:
                leading = -num(-1)
                tlm = _mat_mul((1, 0, 0, 1, num(-2), num(-1)), tlm)
                tm = tlm
            elif op == "Tm" and len(stack) >= 6:
                tlm = tuple(num(i) for i in range(-6, 0))
                tm = tlm
            elif op == "T*":
                tlm = _mat_mul((1, 0, 0, 1, 0, -leading), tlm)
                tm = tlm
            elif op == "TL":
                leading = num(-1)
            elif op == "Tc":
                charspace = num(-1)
            elif op == "Tw":
                wordspace = num(-1)
            elif op == "Tz":
                hscale = num(-1, 100.0) / 100.0
            elif op == "Ts":
                rise = num(-1)
            elif op == "Tr":
                render_mode = int(num(-1))
            elif op == "Tj" and stack:
                if isinstance(stack[-1], bytes):
                    show(stack[-1])
            elif op == "'" and stack:
                tlm = _mat_mul((1, 0, 0, 1, 0, -leading), tlm)
                tm = tlm
                if isinstance(stack[-1], bytes):
                    show(stack[-1])
            elif op == '"' and len(stack) >= 3:
                wordspace = num(-3)
                charspace = num(-2)
                tlm = _mat_mul((1, 0, 0, 1, 0, -leading), tlm)
                tm = tlm
                if isinstance(stack[-1], bytes):
                    show(stack[-1])
            elif op == "TJ" and stack:
                arr = stack[-1]
                if isinstance(arr, list):
                    parts: List[bytes] = []
                    for el in arr:
                        if isinstance(el, bytes):
                            parts.append(el)
                        elif isinstance(el, (int, float)) and el <= -120:
                            # A large negative kern is an inter-word gap.
                            parts.append(b" " if font and not font.two_byte else b"")
                    if parts:
                        show(b"".join(parts))
            elif op == "Do" and stack:
                self._do_xobject(str(stack[-1]), xobj_res, cur_ctm)
            elif op == "BI":
                # Inline image: skip to EI and note it.
                idx = data.find(b"EI", lex.pos)
                self.page.images.append(
                    ImageRef("<inline>", 0, 0, cur_ctm[4], cur_ctm[5], abs(cur_ctm[0]), abs(cur_ctm[3]))
                )
                lex.pos = (idx + 2) if idx >= 0 else len(data)

            stack.clear()

    def _do_xobject(self, name: str, xobj_res: Dict[str, Any], ctm: Sequence[float]) -> None:
        doc = self.doc
        xobj = doc.resolve(xobj_res.get(name))
        if not isinstance(xobj, Stream):
            return
        subtype = str(doc.resolve(xobj.dict.get("Subtype")) or "")
        if subtype == "Image":
            self.page.images.append(
                ImageRef(
                    name=name,
                    width=int(doc.resolve(xobj.dict.get("Width")) or 0),
                    height=int(doc.resolve(xobj.dict.get("Height")) or 0),
                    x=ctm[4],
                    y=ctm[5],
                    draw_w=abs(ctm[0]) or abs(ctm[1]),
                    draw_h=abs(ctm[3]) or abs(ctm[2]),
                    is_mask=bool(doc.resolve(xobj.dict.get("ImageMask"))),
                )
            )
        elif subtype == "Form":
            matrix = doc.resolve(xobj.dict.get("Matrix"))
            new_ctm = ctm
            if isinstance(matrix, list) and len(matrix) == 6:
                try:
                    new_ctm = _mat_mul([float(v) for v in matrix], ctm)
                except (TypeError, ValueError):
                    new_ctm = ctm
            res = doc.resolve(xobj.dict.get("Resources")) or {}
            try:
                self.run(xobj.data(), res, new_ctm)
            except Exception:
                pass


# --------------------------------------------------------------------------
# Document
# --------------------------------------------------------------------------


class Document:
    def __init__(self, data: bytes):
        if b"%PDF" not in data[:1024]:
            raise PdfError("not a PDF: missing %PDF header")
        self.data = data
        self.xref: Dict[int, Tuple[str, int, int]] = {}
        self.trailer: Dict[str, Any] = {}
        self._cache: Dict[int, Any] = {}
        self._objstm_cache: Dict[int, Dict[int, Any]] = {}
        self._font_cache: Dict[Any, FontInfo] = {}
        self.encrypted = False
        self.warnings: List[str] = []
        self._recovered = False

        try:
            self._read_xref_chain()
        except Exception as exc:  # noqa: BLE001 - recovery is the point
            self.warnings.append(f"xref parse failed ({exc}); rebuilt by scanning")
            self._reconstruct()
        if not self.xref:
            self._reconstruct()
        if "Root" not in self.trailer:
            self._find_root()

        if self.trailer.get("Encrypt") is not None:
            self.encrypted = True

        self.pages: List[Page] = []
        self.fonts: Dict[str, FontInfo] = {}

    # -- object access ----------------------------------------------------

    def resolve(self, obj: Any, _depth: int = 0) -> Any:
        while isinstance(obj, Ref) and _depth < 64:
            obj = self.get_object(obj.num)
            _depth += 1
        return obj

    def get_object(self, num: int) -> Any:
        if num in self._cache:
            return self._cache[num]
        self._cache[num] = None  # cycle guard
        entry = self.xref.get(num)
        value = None
        if entry is not None:
            kind, a, _b = entry
            try:
                if kind == "n":
                    value = self._parse_indirect_at(a, num)
                elif kind == "o":
                    value = self._object_from_stream(a, num)
            except Exception:
                value = None
        if value is None and not self._recovered:
            self._reconstruct(keep=True)
            entry = self.xref.get(num)
            if entry is not None and entry[0] == "n":
                try:
                    value = self._parse_indirect_at(entry[1], num)
                except Exception:
                    value = None
        self._cache[num] = value
        return value

    def _parse_indirect_at(self, offset: int, expect: int) -> Any:
        if offset <= 0 or offset >= len(self.data):
            return None
        lex = Lexer(self.data, offset)
        num_tok = lex.read_token()
        gen_tok = lex.read_token()
        obj_tok = lex.read_token()
        if obj_tok != b"obj":
            # Offset may be off by a few bytes; search nearby.
            window = self.data[max(0, offset - 64) : offset + 512]
            m = re.search(rb"(\d+)\s+(\d+)\s+obj", window)
            if not m:
                return None
            lex = Lexer(self.data, max(0, offset - 64) + m.end())
            num_tok = m.group(1)
        try:
            if num_tok is not None and int(num_tok) != expect:
                pass  # generation/number mismatch: still try to read it
        except ValueError:
            pass
        return lex.parse_object(self)

    def _object_from_stream(self, stm_num: int, want: int) -> Any:
        table = self._objstm_cache.get(stm_num)
        if table is None:
            table = {}
            stm = self.resolve(Ref(stm_num))
            if isinstance(stm, Stream):
                try:
                    payload = stm.data()
                    n = int(self.resolve(stm.dict.get("N")) or 0)
                    first = int(self.resolve(stm.dict.get("First")) or 0)
                    head = Lexer(payload[:first])
                    pairs: List[Tuple[int, int]] = []
                    for _ in range(n):
                        a = head.read_token()
                        b = head.read_token()
                        if a is None or b is None:
                            break
                        try:
                            pairs.append((int(a), int(b)))
                        except ValueError:
                            break
                    for onum, off in pairs:
                        sub = Lexer(payload, first + off)
                        try:
                            table[onum] = sub.parse_object(self)
                        except Exception:
                            table[onum] = None
                except Exception:
                    table = {}
            self._objstm_cache[stm_num] = table
        return table.get(want)

    # -- xref -------------------------------------------------------------

    def _read_xref_chain(self) -> None:
        tail = self.data[-2048:]
        idx = tail.rfind(b"startxref")
        if idx < 0:
            raise PdfError("no startxref")
        lex = Lexer(tail, idx + 9)
        tok = lex.read_token()
        if tok is None:
            raise PdfError("bad startxref")
        offset = int(tok)
        seen: set = set()
        while offset and offset not in seen and 0 < offset < len(self.data):
            seen.add(offset)
            offset = self._read_xref_section(offset)

    def _read_xref_section(self, offset: int) -> int:
        lex = Lexer(self.data, offset)
        lex.skip_ws()
        if self.data[lex.pos : lex.pos + 4] == b"xref":
            lex.pos += 4
            while True:
                lex.skip_ws()
                if self.data[lex.pos : lex.pos + 7] == b"trailer":
                    lex.pos += 7
                    trailer = lex.parse_object(self)
                    if isinstance(trailer, dict):
                        for k, v in trailer.items():
                            self.trailer.setdefault(k, v)
                        # A hybrid file points at an xref stream too.
                        xs = trailer.get("XRefStm")
                        if isinstance(xs, int):
                            try:
                                self._read_xref_section(xs)
                            except Exception:
                                pass
                        prev = trailer.get("Prev")
                        return int(prev) if isinstance(prev, (int, float)) else 0
                    return 0
                start_tok = lex.read_token()
                count_tok = lex.read_token()
                if start_tok is None or count_tok is None:
                    return 0
                try:
                    start, count = int(start_tok), int(count_tok)
                except ValueError:
                    return 0
                for i in range(count):
                    lex.skip_ws()
                    entry = self.data[lex.pos : lex.pos + 20]
                    m = re.match(rb"(\d{10})\s(\d{5})\s([nf])", entry)
                    if not m:
                        a = lex.read_token()
                        b = lex.read_token()
                        c = lex.read_token()
                        if a is None or b is None or c is None:
                            return 0
                        off_s, gen_s, kind_s = a, b, c
                        lex.pos = lex.pos
                    else:
                        off_s, gen_s, kind_s = m.group(1), m.group(2), m.group(3)
                        lex.pos += m.end()
                    if kind_s == b"n":
                        self.xref.setdefault(start + i, ("n", int(off_s), int(gen_s)))
            return 0

        # Cross-reference stream.
        obj = self._parse_indirect_at(offset, -1)
        if not isinstance(obj, Stream):
            raise PdfError("xref offset points at neither table nor stream")
        d = obj.dict
        for k, v in d.items():
            self.trailer.setdefault(k, v)
        widths = [int(self.resolve(w) or 0) for w in (self.resolve(d.get("W")) or [])]
        if len(widths) < 3:
            raise PdfError("xref stream missing /W")
        size = int(self.resolve(d.get("Size")) or 0)
        index = self.resolve(d.get("Index")) or [0, size]
        index = [int(self.resolve(v) or 0) for v in index]
        payload = obj.data()
        row = sum(widths)
        pos = 0

        def field(buf: bytes, start: int, width: int, default: int) -> int:
            if width == 0:
                return default
            return int.from_bytes(buf[start : start + width], "big")

        for i in range(0, len(index) - 1, 2):
            first, count = index[i], index[i + 1]
            for j in range(count):
                if pos + row > len(payload):
                    break
                buf = payload[pos : pos + row]
                pos += row
                t = field(buf, 0, widths[0], 1)
                f2 = field(buf, widths[0], widths[1], 0)
                f3 = field(buf, widths[0] + widths[1], widths[2], 0)
                num = first + j
                if num in self.xref:
                    continue
                if t == 1:
                    self.xref[num] = ("n", f2, f3)
                elif t == 2:
                    self.xref[num] = ("o", f2, f3)
        prev = d.get("Prev")
        return int(prev) if isinstance(prev, (int, float)) else 0

    def _reconstruct(self, keep: bool = False) -> None:
        """Rebuild the xref by scanning for ``N G obj``.

        Later definitions win, matching how incremental updates work.
        """
        self._recovered = True
        found: Dict[int, Tuple[str, int, int]] = {}
        for m in re.finditer(rb"(?<![0-9])(\d{1,9})\s+(\d{1,5})\s+obj\b", self.data):
            try:
                found[int(m.group(1))] = ("n", m.start(), int(m.group(2)))
            except ValueError:
                continue
        if keep:
            for num, entry in found.items():
                if num not in self.xref:
                    self.xref[num] = entry
        else:
            found.update({k: v for k, v in self.xref.items() if v[0] == "o"})
            self.xref = found
        self._cache.clear()

        # Pick up any trailer dictionaries left in the file.
        for m in re.finditer(rb"trailer", self.data):
            lex = Lexer(self.data, m.end())
            try:
                t = lex.parse_object(self)
            except Exception:
                continue
            if isinstance(t, dict):
                for k, v in t.items():
                    self.trailer.setdefault(k, v)

    def _find_root(self) -> None:
        for num in sorted(self.xref):
            obj = self.resolve(Ref(num))
            d = obj.dict if isinstance(obj, Stream) else obj
            if isinstance(d, dict) and str(self.resolve(d.get("Type")) or "") == "Catalog":
                self.trailer["Root"] = Ref(num)
                return
        # Last resort: any object with /Pages.
        for num in sorted(self.xref):
            obj = self.resolve(Ref(num))
            if isinstance(obj, dict) and "Pages" in obj:
                self.trailer["Root"] = Ref(num)
                return

    # -- fonts ------------------------------------------------------------

    def font_for(self, fonts_res: Dict[str, Any], name: str) -> Optional[FontInfo]:
        ref = fonts_res.get(name)
        if ref is None:
            return None
        cache_key = (ref.num, ref.gen) if isinstance(ref, Ref) else (name, id(fonts_res))
        if cache_key in self._font_cache:
            return self._font_cache[cache_key]
        info = self._build_font(name, self.resolve(ref))
        self._font_cache[cache_key] = info
        if info is not None:
            self.fonts.setdefault(f"{info.family or name}#{len(self.fonts)}", info)
        return info

    def _build_font(self, key: str, fd: Any) -> Optional[FontInfo]:
        if not isinstance(fd, dict):
            return None
        info = FontInfo(key=key)
        info.subtype = str(self.resolve(fd.get("Subtype")) or "")
        info.base_font = str(self.resolve(fd.get("BaseFont")) or "")

        descendant = None
        if info.subtype == "Type0":
            info.two_byte = True
            desc_list = self.resolve(fd.get("DescendantFonts"))
            if isinstance(desc_list, list) and desc_list:
                descendant = self.resolve(desc_list[0])

        tu = self.resolve(fd.get("ToUnicode"))
        if isinstance(tu, Stream):
            try:
                mapping, two_byte = _parse_tounicode(tu.data())
                if mapping:
                    info.to_unicode = mapping
                    info.has_tounicode = True
                    if two_byte:
                        info.two_byte = True
            except Exception:
                pass

        enc = self.resolve(fd.get("Encoding"))
        if isinstance(enc, str):
            info.encoding_name = str(enc)
            if "Identity" in info.encoding_name:
                info.two_byte = True
        elif isinstance(enc, dict):
            info.encoding_name = str(self.resolve(enc.get("BaseEncoding")) or "")
            diffs = self.resolve(enc.get("Differences"))
            if isinstance(diffs, list):
                code = 0
                for item in diffs:
                    item = self.resolve(item)
                    if isinstance(item, (int, float)):
                        code = int(item)
                    elif isinstance(item, str):
                        info.differences[code] = str(item)
                        code += 1
        elif isinstance(enc, Stream):
            info.two_byte = True

        source = descendant if isinstance(descendant, dict) else fd
        fdesc = self.resolve(source.get("FontDescriptor"))
        if isinstance(fdesc, dict):
            for slot in ("FontFile", "FontFile2", "FontFile3"):
                if fdesc.get(slot) is not None:
                    info.embedded = True
                    break
            flags = self.resolve(fdesc.get("Flags"))
            if isinstance(flags, (int, float)):
                info.symbolic = bool(int(flags) & 4) and not bool(int(flags) & 32)
        elif info.subtype == "Type3":
            info.embedded = True

        return info

    # -- pages ------------------------------------------------------------

    def load_pages(self, max_pages: int = 30) -> List[Page]:
        if self.pages:
            return self.pages
        root = self.resolve(self.trailer.get("Root"))
        page_nodes: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        if isinstance(root, dict):
            tree = self.resolve(root.get("Pages"))
            if isinstance(tree, dict):
                self._walk_pages(tree, {}, page_nodes, set(), max_pages)
        if not page_nodes:
            # Recovery: scan for objects of /Type /Page.
            for num in sorted(self.xref):
                obj = self.resolve(Ref(num))
                if isinstance(obj, dict) and str(self.resolve(obj.get("Type")) or "") == "Page":
                    page_nodes.append((obj, {}))
                    if len(page_nodes) >= max_pages:
                        break

        for i, (node, inherited) in enumerate(page_nodes):
            self.pages.append(self._build_page(i + 1, node, inherited))
        return self.pages

    INHERITABLE = ("Resources", "MediaBox", "CropBox", "Rotate")

    def _walk_pages(
        self,
        node: Dict[str, Any],
        inherited: Dict[str, Any],
        out: List[Tuple[Dict[str, Any], Dict[str, Any]]],
        seen: set,
        limit: int,
    ) -> None:
        if len(out) >= limit:
            return
        node_id = id(node)
        if node_id in seen:
            return
        seen.add(node_id)

        merged = dict(inherited)
        for key in self.INHERITABLE:
            if node.get(key) is not None:
                merged[key] = node[key]

        kids = self.resolve(node.get("Kids"))
        node_type = str(self.resolve(node.get("Type")) or "")
        if isinstance(kids, list) and node_type != "Page":
            for kid in kids:
                kid_obj = self.resolve(kid)
                if isinstance(kid_obj, dict):
                    self._walk_pages(kid_obj, merged, out, seen, limit)
                if len(out) >= limit:
                    return
        else:
            out.append((node, merged))

    def _build_page(self, number: int, node: Dict[str, Any], inherited: Dict[str, Any]) -> Page:
        def attr(key: str) -> Any:
            return self.resolve(node.get(key, inherited.get(key)))

        box = attr("CropBox") or attr("MediaBox") or [0, 0, 612, 792]
        try:
            coords = [float(self.resolve(v)) for v in box]
            x0, y0, x1, y1 = coords[0], coords[1], coords[2], coords[3]
            width, height = abs(x1 - x0), abs(y1 - y0)
        except (TypeError, ValueError, IndexError):
            width, height = 612.0, 792.0
        if width <= 1 or height <= 1:
            width, height = 612.0, 792.0

        rotate = attr("Rotate")
        rotate = int(rotate) % 360 if isinstance(rotate, (int, float)) else 0
        page = Page(number=number, width=width, height=height, rotate=rotate)

        annots = self.resolve(node.get("Annots"))
        if isinstance(annots, list):
            for a in annots[:200]:
                a = self.resolve(a)
                if not isinstance(a, dict):
                    continue
                action = self.resolve(a.get("A"))
                if isinstance(action, dict):
                    uri = self.resolve(action.get("URI"))
                    if isinstance(uri, bytes):
                        page.annots_uri.append(uri.decode("latin-1", "replace"))
                    elif isinstance(uri, str):
                        page.annots_uri.append(uri)

        contents = self.resolve(node.get("Contents"))
        chunks: List[bytes] = []
        if isinstance(contents, Stream):
            chunks.append(_safe_stream_data(contents))
        elif isinstance(contents, list):
            for c in contents:
                c = self.resolve(c)
                if isinstance(c, Stream):
                    chunks.append(_safe_stream_data(c))
        if chunks:
            resources = self.resolve(attr("Resources")) or {}
            interp = _ContentInterpreter(self, page, resources)
            try:
                interp.run(b"\n".join(chunks), resources, IDENTITY)
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"page {number}: content parse stopped ({exc})")

        # Normalise coordinates so y grows downward and rotation is applied.
        _normalise_page(page)
        return page


def _safe_stream_data(stream: Stream) -> bytes:
    try:
        return stream.data()
    except Exception:
        return b""


def _normalise_page(page: Page) -> None:
    """Convert PDF user space to a top-left origin, honouring /Rotate."""
    w, h = page.width, page.height
    rot = page.rotate

    def convert(x: float, y: float) -> Tuple[float, float]:
        if rot == 90:
            return y, x
        if rot == 180:
            return w - x, y
        if rot == 270:
            return h - y, w - x
        return x, h - y

    for span in page.spans:
        span.x, span.y = convert(span.x, span.y)
    for img in page.images:
        img.x, img.y = convert(img.x, img.y)
    if rot in (90, 270):
        page.width, page.height = h, w


def load(path_or_bytes: Any) -> Document:
    if isinstance(path_or_bytes, (bytes, bytearray)):
        data = bytes(path_or_bytes)
    else:
        with open(path_or_bytes, "rb") as fh:
            data = fh.read()
    doc = Document(data)
    doc.load_pages()
    return doc
