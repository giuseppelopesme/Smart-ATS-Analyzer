"""Hand-rolled PDF builders for testing pdfmini (no third-party deps)."""
import zlib


def _esc(s):
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build(objects, root_num, use_xref_stream=False, use_objstm=False, break_xref=False):
    """objects: dict num -> bytes body (already serialized, without 'N 0 obj')."""
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}

    if use_objstm:
        # Pack all non-stream dict objects into an object stream.
        packable = {n: b for n, b in objects.items() if not b.lstrip().startswith(b"<<") or b"stream" not in b}
        packable = {n: b for n, b in packable.items() if b"stream" not in b}
        rest = {n: b for n, b in objects.items() if n not in packable}
        pairs, body = [], bytearray()
        for n, b in sorted(packable.items()):
            pairs.append(f"{n} {len(body)}")
            body.extend(b.strip() + b"\n")
        header = (" ".join(pairs) + "\n").encode()
        payload = header + bytes(body)
        comp = zlib.compress(payload)
        objstm_num = max(objects) + 1
        for n, b in sorted(rest.items()):
            offsets[n] = len(out)
            out.extend(f"{n} 0 obj\n".encode() + b.strip() + b"\nendobj\n")
        offsets[objstm_num] = len(out)
        out.extend(
            f"{objstm_num} 0 obj\n<< /Type /ObjStm /N {len(pairs)} /First {len(header)} "
            f"/Length {len(comp)} /Filter /FlateDecode >>\nstream\n".encode()
        )
        out.extend(comp)
        out.extend(b"\nendstream\nendobj\n")

        xref_num = objstm_num + 1
        size = xref_num + 1
        rows = bytearray()
        entries = {0: (0, 0, 65535)}
        for n in rest:
            entries[n] = (1, offsets[n], 0)
        for i, n in enumerate(sorted(packable)):
            entries[n] = (2, objstm_num, i)
        entries[objstm_num] = (1, offsets[objstm_num], 0)
        xref_off = len(out)
        entries[xref_num] = (1, xref_off, 0)
        for n in range(size):
            t, f2, f3 = entries.get(n, (0, 0, 0))
            rows.extend(bytes([t]) + f2.to_bytes(4, "big") + f3.to_bytes(2, "big"))
        xcomp = zlib.compress(bytes(rows))
        out.extend(
            f"{xref_num} 0 obj\n<< /Type /XRef /Size {size} /W [1 4 2] /Root {root_num} 0 R "
            f"/Length {len(xcomp)} /Filter /FlateDecode >>\nstream\n".encode()
        )
        out.extend(xcomp)
        out.extend(b"\nendstream\nendobj\n")
        out.extend(f"startxref\n{xref_off}\n%%EOF\n".encode())
        return bytes(out)

    for n in sorted(objects):
        offsets[n] = len(out)
        out.extend(f"{n} 0 obj\n".encode() + objects[n].strip() + b"\nendobj\n")

    if use_xref_stream:
        xref_num = max(objects) + 1
        size = xref_num + 1
        xref_off = len(out)
        rows = bytearray()
        for n in range(size):
            if n == 0:
                rows.extend(b"\x00" + (0).to_bytes(4, "big") + (65535).to_bytes(2, "big"))
            elif n == xref_num:
                rows.extend(b"\x01" + xref_off.to_bytes(4, "big") + (0).to_bytes(2, "big"))
            elif n in offsets:
                rows.extend(b"\x01" + offsets[n].to_bytes(4, "big") + (0).to_bytes(2, "big"))
            else:
                rows.extend(b"\x00" + (0).to_bytes(4, "big") + (0).to_bytes(2, "big"))
        comp = zlib.compress(bytes(rows))
        out.extend(
            f"{xref_num} 0 obj\n<< /Type /XRef /Size {size} /W [1 4 2] /Root {root_num} 0 R "
            f"/Length {len(comp)} /Filter /FlateDecode >>\nstream\n".encode()
        )
        out.extend(comp)
        out.extend(b"\nendstream\nendobj\n")
        out.extend(f"startxref\n{xref_off}\n%%EOF\n".encode())
        return bytes(out)

    xref_off = len(out)
    size = max(objects) + 1
    out.extend(f"xref\n0 {size}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for n in range(1, size):
        if n in offsets:
            out.extend(f"{offsets[n]:010d} 00000 n \n".encode())
        else:
            out.extend(b"0000000000 65535 f \n")
    out.extend(f"trailer\n<< /Size {size} /Root {root_num} 0 R >>\n".encode())
    if break_xref:
        xref_off += 137  # deliberately wrong -> forces reconstruction
    out.extend(f"startxref\n{xref_off}\n%%EOF\n".encode())
    return bytes(out)


def content_stream(body: bytes, compress=False):
    if compress:
        c = zlib.compress(body)
        return b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(c) + c + b"\nendstream"
    return b"<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream"


def simple_text_pdf(lines, compress=False, **kw):
    """lines: list of (x, y, size, text)."""
    ops = [b"BT /F1 12 Tf"]
    for x, y, size, text in lines:
        ops.append(f"/F1 {size} Tf 1 0 0 1 {x} {y} Tm ({_esc(text)}) Tj".encode())
    ops.append(b"ET")
    body = b"\n".join(ops)
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
           b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        4: content_stream(body, compress),
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    return build(objs, 1, **kw)


def type0_no_tounicode_pdf(text="Senior Engineer"):
    """Identity-H CID font with no ToUnicode: the classic garbage-extraction case."""
    codes = "".join(f"{ord(c):04X}" for c in text)
    body = f"BT /F1 12 Tf 1 0 0 1 72 700 Tm <{codes}> Tj ET".encode()
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
           b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        4: content_stream(body),
        5: b"<< /Type /Font /Subtype /Type0 /BaseFont /AAAAAA+Custom /Encoding /Identity-H "
           b"/DescendantFonts [6 0 R] >>",
        6: b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /AAAAAA+Custom "
           b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
           b"/FontDescriptor 7 0 R >>",
        7: b"<< /Type /FontDescriptor /FontName /AAAAAA+Custom /Flags 4 /FontFile2 8 0 R >>",
        8: content_stream(b"\x00\x01\x00\x00fake-font-data"),
    }
    return build(objs, 1)


def type0_with_tounicode_pdf(text="Senior Engineer"):
    codes = "".join(f"{ord(c):04X}" for c in text)
    body = f"BT /F1 12 Tf 1 0 0 1 72 700 Tm <{codes}> Tj ET".encode()
    uniq = sorted(set(text))
    bfchars = "".join(f"<{ord(c):04X}> <{ord(c):04X}>\n" for c in uniq)
    cmap = (
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        f"{len(uniq)} beginbfchar\n{bfchars}endbfchar\n"
        "endcmap end end"
    ).encode()
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
           b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        4: content_stream(body),
        5: b"<< /Type /Font /Subtype /Type0 /BaseFont /AAAAAA+Custom /Encoding /Identity-H "
           b"/DescendantFonts [6 0 R] /ToUnicode 9 0 R >>",
        6: b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /AAAAAA+Custom "
           b"/FontDescriptor 7 0 R >>",
        7: b"<< /Type /FontDescriptor /FontName /AAAAAA+Custom /Flags 4 /FontFile2 8 0 R >>",
        8: content_stream(b"\x00\x01\x00\x00fake"),
        9: content_stream(cmap),
    }
    return build(objs, 1)


def two_column_pdf():
    """Left column = experience, right column = skills. Interleaved in stream order."""
    left = ["EXPERIENCE", "Senior Backend Engineer", "Acme Corp 2020-2024",
            "Built payment systems handling 4M requests", "Led team of six engineers",
            "Cut p99 latency by 40 percent", "Staff Engineer, Globex 2017-2020",
            "Designed event-driven ingestion", "Mentored four juniors",
            "EDUCATION", "BSc Computer Science, Bologna"]
    right = ["SKILLS", "Python", "Kubernetes", "PostgreSQL", "Terraform",
             "Go", "AWS", "Kafka", "gRPC", "CI/CD", "Observability"]
    ops = [b"BT"]
    y = 700
    for i in range(max(len(left), len(right))):
        # Emit left then right on the same visual line -- the layout that
        # makes naive extractors interleave the two columns.
        if i < len(left):
            ops.append(f"/F1 11 Tf 1 0 0 1 60 {y} Tm ({_esc(left[i])}) Tj".encode())
        if i < len(right):
            ops.append(f"/F1 11 Tf 1 0 0 1 400 {y} Tm ({_esc(right[i])}) Tj".encode())
        y -= 24
    ops.append(b"ET")
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
           b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        4: content_stream(b"\n".join(ops), compress=True),
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    return build(objs, 1, use_xref_stream=True)


def scanned_pdf():
    """A page that is one big image with no text layer."""
    body = b"q 612 0 0 792 0 0 cm /Im0 Do Q"
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
           b"/Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>",
        4: content_stream(body),
        5: b"<< /Type /XObject /Subtype /Image /Width 1700 /Height 2200 "
           b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length 12 >>\n"
           b"stream\n\xff\xd8\xff\xe0fakejpg\n\nendstream",
    }
    return build(objs, 1)


def multi_page_text_pdf(text, compress=True, font_size=10, leading=13,
                        top=780, bottom=52, left=60, wrap=95):
    """Lay plain text out over as many pages as it needs.

    Needed for round-trip tests: a two-page CV crammed onto one page has
    overlapping baselines, which scrambles line grouping and makes the
    extraction differ from the source for reasons that have nothing to do
    with the code under test.
    """
    pages, cur, y = [], [], top
    for raw in text.split("\n"):
        chunks = []
        line = raw
        while len(line) > wrap:
            cut = line.rfind(" ", 0, wrap)
            cut = cut if cut > 0 else wrap
            chunks.append(line[:cut])
            line = line[cut + 1:]
        chunks.append(line)
        for chunk in chunks:
            if y < bottom:
                pages.append(cur)
                cur, y = [], top
            cur.append((left, y, font_size, chunk))
            y -= leading
    if cur:
        pages.append(cur)

    objs = {1: b"", 2: b""}
    kids, next_num = [], 3
    page_nums = []
    for page_lines in pages:
        ops = [b"BT"]
        for x, yy, size, t in page_lines:
            ops.append(f"/F1 {size} Tf 1 0 0 1 {x} {yy} Tm ({_esc(t)}) Tj".encode())
        ops.append(b"ET")
        content_num = next_num + 1
        objs[next_num] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 " + str(9999).encode() + b" 0 R >> >> "
            b"/Contents " + str(content_num).encode() + b" 0 R >>")
        objs[content_num] = content_stream(b"\n".join(ops), compress)
        page_nums.append(next_num)
        kids.append(f"{next_num} 0 R".encode())
        next_num += 2

    font_num = next_num
    for n in page_nums:
        objs[n] = objs[n].replace(b"9999 0 R", f"{font_num} 0 R".encode())
    objs[font_num] = (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                      b"/Encoding /WinAnsiEncoding >>")
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = (b"<< /Type /Pages /Kids [" + b" ".join(kids) +
               b"] /Count " + str(len(kids)).encode() + b" >>")
    return build(objs, 1, use_xref_stream=True)
