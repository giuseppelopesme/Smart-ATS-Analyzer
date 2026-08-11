"""Build .docx files by hand for validator tests. Standard library only.

Word measurements, so the numbers below are not arbitrary:
  * font sizes in ``w:sz`` are HALF-points (18pt -> 36)
  * page and margin values are twips (1 cm = 566.93 twips)
  * A4 is 11906 x 16838 twips
"""

from __future__ import annotations

import zipfile
from typing import Dict, List, Optional, Tuple

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
R_NS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'

A4_W, A4_H = 11906, 16838
CM = 566.929133858


def cm(v: float) -> int:
    return int(round(v * CM))


def esc(t: str) -> str:
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def run(text: str, size_pt: float = 10, bold: bool = False, italic: bool = False,
        color: str = "", font: str = "", caps: bool = False) -> str:
    rpr = []
    if font:
        rpr.append(f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}"/>')
    if bold:
        rpr.append("<w:b/>")
    if italic:
        rpr.append("<w:i/>")
    if caps:
        rpr.append("<w:caps/>")
    if color:
        rpr.append(f'<w:color w:val="{color}"/>')
    rpr.append(f'<w:sz w:val="{int(size_pt * 2)}"/>')
    return (f"<w:r><w:rPr>{''.join(rpr)}</w:rPr>"
            f'<w:t xml:space="preserve">{esc(text)}</w:t></w:r>')


def para(text: str, size_pt: float = 10, bold: bool = False, italic: bool = False,
         color: str = "", bullet: bool = False, border: bool = False,
         font: str = "", ilvl: int = 0, num_id: str = "1") -> str:
    ppr = []
    if bullet:
        ppr.append(f'<w:numPr><w:ilvl w:val="{ilvl}"/><w:numId w:val="{num_id}"/></w:numPr>')
    if border:
        ppr.append('<w:pBdr><w:bottom w:val="single" w:sz="4" w:space="1" '
                   'w:color="BFBFBF"/></w:pBdr>')
    ppr_xml = f"<w:pPr>{''.join(ppr)}</w:pPr>" if ppr else ""
    return f"<w:p>{ppr_xml}{run(text, size_pt, bold, italic, color, font)}</w:p>"


def hyperlink_para(text: str, rid: str, size_pt: float = 10) -> str:
    return (f'<w:p><w:hyperlink r:id="{rid}">'
            f"{run(text, size_pt)}</w:hyperlink></w:p>")


def sect_pr(top: float = 1.5, bottom: float = 1.5, left: float = 1.7,
            right: float = 1.7, cols: int = 1, header_ref: bool = False) -> str:
    hdr = '<w:headerReference w:type="default" r:id="rIdH"/>' if header_ref else ""
    return (f"<w:sectPr>{hdr}"
            f'<w:pgSz w:w="{A4_W}" w:h="{A4_H}"/>'
            f'<w:pgMar w:top="{cm(top)}" w:right="{cm(right)}" '
            f'w:bottom="{cm(bottom)}" w:left="{cm(left)}" '
            f'w:header="0" w:footer="0" w:gutter="0"/>'
            f'<w:cols w:num="{cols}" w:space="708"/>'
            f"</w:sectPr>")


STYLES = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:styles {W_NS}>
  <w:docDefaults><w:rPrDefault><w:rPr>
    <w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>
    <w:sz w:val="20"/>
  </w:rPr></w:rPrDefault></w:docDefaults>
  <w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
  <w:style w:type="paragraph" w:styleId="ListParagraph">
    <w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/>
  </w:style>
</w:styles>"""

NUMBERING = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:numbering {W_NS}>
  <w:abstractNum w:abstractNumId="0">
    <w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
</w:numbering>"""

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""


def core_props(author: str = "Giuseppe Lopes",
               title: str = "Giuseppe Lopes - Global AI Director - CV") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<cp:coreProperties
  xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
  xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:creator>{esc(author)}</dc:creator>
  <dc:title>{esc(title)}</dc:title>
</cp:coreProperties>"""


APP_PROPS = """<?xml version="1.0" encoding="UTF-8"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
  <Pages>2</Pages><Words>1000</Words><Application>Microsoft Office Word</Application>
</Properties>"""


# --------------------------------------------------------------------------
# Canonical content
# --------------------------------------------------------------------------

SUMMARY = [
    "Commercial technology leader with fifteen years building and running artificial "
    "intelligence and data practices for regulated industries across Italy and "
    "continental Europe, with direct profit and loss accountability at country level and "
    "a consistent record of growing service lines through market contractions rather "
    "than only in favourable conditions.",
    "Built an artificial intelligence practice to EUR 4.5M in annual revenue from a "
    "standing start, hiring and leading roughly forty specialists across data science, "
    "machine learning engineering, data platform and delivery management, and "
    "establishing the operating standards that let the practice scale across five "
    "country units without a corresponding increase in delivery risk.",
    "Operates comfortably inside DORA, TUB and TUF regulatory perimeters, translating "
    "supervisory requirements into delivery constraints that engineering teams can act "
    "on without stalling commercial momentum, and representing technical positions "
    "directly to boards, regulators and tier one client procurement functions.",
]

COMPETENCIES = [
    "Artificial Intelligence Strategy | Data Governance | P&L Ownership | Practice Building",
    "Machine Learning Delivery | Cloud Architecture | Regulatory Compliance | DORA | TUB | TUF",
    "Commercial Negotiation | Partner Ecosystems | Team Leadership | Stakeholder Management",
]

ACHIEVEMENTS = [
    "Built an artificial intelligence practice from a standing start to EUR 4.5M in "
    "annual recurring revenue within three financial years, entirely organically.",
    "Recruited, structured and led approximately 40 specialists spanning data science, "
    "machine learning engineering, data platform and delivery management.",
    "Owned a EUR 15M country profit and loss and improved operating margin by 5pp across "
    "two consecutive years while absorbing a major restructuring.",
    "Delivered +53% year-on-year growth in the data and analytics service line, against a "
    "market that contracted over the same period.",
]

ROLES: List[Tuple[str, str, List[str]]] = [
    ("Country Manager, Italy", "Ulixe Group | March 2024 - Present", [
        "Own the Italian profit and loss of EUR 15M across consulting, data engineering "
        "and managed services, reporting directly to the group chief executive.",
        "Grew the artificial intelligence service line 53% year on year by repositioning "
        "it around regulated data workloads rather than generic automation.",
        "Restructured the delivery organisation across three offices, lifting billable "
        "utilisation from 78% to 89% without involuntary attrition.",
        "Negotiated three multi-year framework agreements with tier one banking clients, "
        "each covering data platform modernisation under DORA obligations.",
        "Introduced a commercial governance model that cut proposal turnaround from "
        "eleven days to four and raised competitive win rate by 9pp.",
    ]),
    ("Director, AI and Data Practice", "Reply | January 2019 - February 2024", [
        "Founded the artificial intelligence practice and scaled it to EUR 4.5M in annual "
        "revenue across banking, insurance and asset management clients.",
        "Hired and managed a team of 40 data scientists, machine learning engineers and "
        "platform specialists across two delivery centres.",
        "Led machine learning delivery for four systemically important banks, working "
        "inside DORA, TUB and TUF supervisory constraints throughout.",
        "Established the MLOps platform standard subsequently adopted across five country "
        "units, reducing model time to production from months to weeks.",
        "Chaired the internal AI governance board covering model risk, validation and "
        "the documentation required for supervisory review.",
    ]),
    ("Head of Data Engineering", "Reply | March 2016 - December 2018", [
        "Led the data engineering function through the migration of three on-premise "
        "warehouses to a cloud lakehouse architecture on Azure and Databricks.",
        "Standardised ingestion across 40 source systems, cutting the average time to "
        "onboard a new data source from six weeks to nine days.",
        "Owned the EUR 2.8M platform budget and renegotiated cloud commitments to "
        "release 22% of annual run cost for reinvestment in delivery capacity.",
        "Established the on-call and incident review practice still used by the group, "
        "reducing repeat incidents by 44% in the first year.",
    ]),
    ("Senior Manager, Data and Analytics", "Accenture | September 2015 - February 2016", [
        "Ran a EUR 6M portfolio of data warehouse and analytics programmes for two "
        "retail banking groups and one insurance carrier.",
        "Built and led a 25-person delivery team spanning Milan and Naples, with a "
        "graduate intake programme that produced eleven permanent hires.",
        "Delivered a regulatory reporting platform that cut month-end close from nine "
        "days to two, adopted as the group standard.",
        "Introduced automated data quality testing across four programmes, reducing "
        "production defects reported by business users by 62%.",
    ]),
]

EARLIER = [
    "Consultant, Capgemini (2011 - 2015)",
    "Analyst, Engineering Ingegneria Informatica (2009 - 2011)",
]

EDUCATION = [
    "MSc Computer Engineering, Politecnico di Milano (2009 - 2011)",
    "BSc Computer Engineering, Universita di Bologna (2006 - 2009)",
]

TECHNOLOGY = [
    "Python | SQL | Databricks | Snowflake | dbt | Apache Spark | Apache Kafka",
    "AWS | Microsoft Azure | Kubernetes | Docker | Terraform | MLflow | Airflow",
    "PyTorch | TensorFlow | scikit-learn | Large Language Models | Retrieval Augmented Generation",
]

LANGUAGES = "Italian (Native) | English (Proficient, C2)"

HEADING = dict(size_pt=12, bold=True, border=True)


def canonical_body(
    name: str = "Giuseppe Lopes",
    headline: str = "Global AI Director",
    contact: str = "Milan, Italy | +39 000 000 0000 | giuseppe@example.com",
) -> str:
    p: List[str] = []
    p.append(para(name, size_pt=18, bold=True))
    p.append(para(headline, size_pt=12, color="808080"))
    p.append(para(contact, size_pt=10))
    p.append(hyperlink_para("linkedin.com/in/giuseppelopes", "rId1", size_pt=10))

    p.append(para("PROFESSIONAL SUMMARY", **HEADING))
    p += [para(t, size_pt=10) for t in SUMMARY]

    p.append(para("CORE COMPETENCIES", **HEADING))
    p += [para(t, size_pt=10) for t in COMPETENCIES]

    p.append(para("KEY ACHIEVEMENTS", **HEADING))
    p += [para(t, size_pt=10, bullet=True) for t in ACHIEVEMENTS]

    p.append(para("PROFESSIONAL EXPERIENCE", **HEADING))
    for title, company, bullets in ROLES:
        p.append(para(title, size_pt=11, bold=True))
        p.append(para(company, size_pt=10, italic=True))
        p += [para(b, size_pt=10, bullet=True) for b in bullets]

    p.append(para("EARLIER CAREER", **HEADING))
    p += [para(t, size_pt=10, bullet=True) for t in EARLIER]

    p.append(para("EDUCATION", **HEADING))
    p += [para(t, size_pt=10, bullet=True) for t in EDUCATION]

    p.append(para("TECHNOLOGY", **HEADING))
    p += [para(t, size_pt=10) for t in TECHNOLOGY]

    p.append(para("LANGUAGES", **HEADING))
    p.append(para(LANGUAGES, size_pt=10))
    return "".join(p)


def plain_text() -> str:
    """The same content as plain text, for building a matching PDF."""
    lines = [
        "Giuseppe Lopes", "Global AI Director",
        "Milan, Italy | +39 000 000 0000 | giuseppe@example.com",
        "linkedin.com/in/giuseppelopes",
        "PROFESSIONAL SUMMARY", *SUMMARY,
        "CORE COMPETENCIES", *COMPETENCIES,
        "KEY ACHIEVEMENTS", *ACHIEVEMENTS,
        "PROFESSIONAL EXPERIENCE",
    ]
    for title, company, bullets in ROLES:
        lines += [title, company, *bullets]
    lines += ["EARLIER CAREER", *EARLIER, "EDUCATION", *EDUCATION,
              "TECHNOLOGY", *TECHNOLOGY, "LANGUAGES", LANGUAGES]
    return "\n".join(lines)


def build_docx(
    path: str,
    body: Optional[str] = None,
    sect: Optional[str] = None,
    author: str = "Giuseppe Lopes",
    title: str = "Giuseppe Lopes - Global AI Director - CV",
    header_text: str = "",
    with_numbering: bool = True,
) -> str:
    body = canonical_body() if body is None else body
    sect = sect_pr(header_ref=bool(header_text)) if sect is None else sect
    document = (f'<?xml version="1.0" encoding="UTF-8"?>'
                f"<w:document {W_NS} {R_NS}><w:body>{body}{sect}</w:body></w:document>")

    rels = ['<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
            'Target="https://www.linkedin.com/in/giuseppelopes" TargetMode="External"/>']
    if header_text:
        rels.append('<Relationship Id="rIdH" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" '
                    'Target="header1.xml"/>')
    rels_xml = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                + "".join(rels) + "</Relationships>")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                   'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", rels_xml)
        z.writestr("word/styles.xml", STYLES)
        if with_numbering:
            z.writestr("word/numbering.xml", NUMBERING)
        if header_text:
            z.writestr("word/header1.xml",
                       f'<?xml version="1.0"?><w:hdr {W_NS}>{para(header_text)}</w:hdr>')
        z.writestr("docProps/core.xml", core_props(author, title))
        z.writestr("docProps/app.xml", APP_PROPS)
    return path


# --------------------------------------------------------------------------
# Deliberately broken variants
# --------------------------------------------------------------------------


def broken_body() -> str:
    """Every failure the canonical spec is meant to catch."""
    p: List[str] = []
    p.append(para("Giuseppe Lopes", size_pt=14, bold=False))          # wrong size, not bold
    p.append(para("Global AI Director", size_pt=12))                   # not gray
    p.append(para("Milan Italy giuseppe@example.com", size_pt=10))     # no pipes, no phone
    p.append(para("linkedin.com/in/giuseppelopes", size_pt=10))        # not a hyperlink

    p.append(para("PROFESSIONAL SUMMARY", size_pt=12, bold=True))      # no bottom rule
    p.append(para(SUMMARY[0], size_pt=10))                             # only 1 paragraph

    p.append(para("CORE COMPETENCIES", size_pt=12, bold=True, border=True))
    p.append(para(COMPETENCIES[0], size_pt=10))                        # 1 line, want 3

    p.append(para("KEY ACHIEVEMENTS", size_pt=12, bold=True, border=True))
    p.append(para("• Built an AI practice from a standing start.", size_pt=10))  # typed glyph
    p.append(para("• Led a large team of specialists.", size_pt=10))             # unquantified

    p.append(para("PROFESSIONAL EXPERIENCE", size_pt=12, bold=True, border=True))
    p.append(para("Country Manager, Italy", size_pt=11, bold=True))
    p.append(para("Ulixe Group 2024-now", size_pt=10, italic=True))    # malformed date line
    p.append(para(ROLES[0][2][0], size_pt=10, bullet=True))            # only 1 bullet

    p.append(para("LANGUAGES", size_pt=12, bold=True, border=True))
    p.append(para("Italian, English", size_pt=10))                     # no pipe

    p.append(para("EDUCATION", size_pt=12, bold=True, border=True))    # after LANGUAGES
    p.append(para("MSc Computer Engineering Politecnico", size_pt=10, bullet=True))

    # A layout table and an inline image, both forbidden.
    p.append("<w:tbl><w:tr><w:tc>" + para("Skills") + "</w:tc><w:tc>"
             + para("Python") + "</w:tc></w:tr></w:tbl>")
    p.append("<w:p><w:r><w:drawing/></w:r></w:p>")
    return "".join(p)


def build_broken_docx(path: str) -> str:
    return build_docx(
        path,
        body=broken_body(),
        sect=sect_pr(top=2.5, bottom=2.5, left=3.0, right=3.0, cols=2, header_ref=True),
        author="",
        title="CV final v3",
        header_text="giuseppe@example.com | +39 000 000 0000",
    )
