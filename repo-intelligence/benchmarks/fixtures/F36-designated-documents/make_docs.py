"""Write the fixture's Office documents: base/docs/data-dictionary.docx and base/docs/order-codes.xlsx.

    python benchmarks/fixtures/F36-designated-documents/make_docs.py

Both are zip archives with members stored uncompressed and a fixed date, so the bytes are
the same on every run and the documents reader can give exact spans. They hold only what
the reader needs: the document and its styles for DOCX; the workbook, its relationships,
shared strings and sheets for XLSX. The `Counts` sheet has formulas with cached values,
and one without, which the reader must not evaluate.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
DATE = (2026, 1, 1, 0, 0, 0)


def _zip(path: Path, members: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, text in members.items():
            info = zipfile.ZipInfo(name, date_time=DATE)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            zf.writestr(info, text.encode("utf-8"))


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _p(text: str, style: str | None = None) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{ppr}<w:r><w:t xml:space=\"preserve\">{escape(text)}</w:t></w:r></w:p>"


def _table(rows: list[list[str]]) -> str:
    body = "".join("<w:tr>" + "".join(f"<w:tc><w:tcPr/>{_p(cell)}</w:tc>" for cell in row) + "</w:tr>" for row in rows)
    return f"<w:tbl><w:tblPr/>{body}</w:tbl>"


def docx() -> None:
    header = ["Field", "Type", "Nullable", "Key", "Description"]
    body = "".join([
        _p("Order data dictionary", "Title"),
        _p("Owned by the order desk. Columns as the warehouse holds them."),
        _p("orders", "Heading1"),
        _p("One row per customer order."),
        _table([header,
                ["order_id", "integer", "No", "PK", "Order number"],
                ["customer_id", "integer", "No", "FK", "The customer who placed it"],
                ["status", "varchar(20)", "No", "", "Order status code, see order-codes.xlsx"],
                ["amount", "decimal(10,2)", "Yes", "", "Order value before tax"]]),
        _p("customers", "Heading1"),
        _table([header,
                ["id", "integer", "No", "PK", "Customer number"],
                ["name", "text", "No", "", "Trading name"],
                ["region", "char(2)", "Yes", "", "Sales region code"]]),
    ])
    styles = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {W}>'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
        "</w:styles>"
    )
    _zip(HERE / "base" / "docs" / "data-dictionary.docx", {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {W}><w:body>{body}</w:body></w:document>',
        "word/styles.xml": styles,
    })


def xlsx() -> None:
    strings = ["Code", "Meaning", "D", "Draft", "S", "Submitted", "A", "Approved", "X", "Cancelled", "Count"]
    index = {s: i for i, s in enumerate(strings)}

    def row(n: int, cells: list[tuple[str, str]]) -> str:
        out = []
        for col, value in cells:
            if value.startswith("="):
                formula, _, cached = value[1:].partition("|")
                v = f"<v>{cached}</v>" if cached else ""
                out.append(f'<c r="{col}{n}"><f>{escape(formula)}</f>{v}</c>')
            elif value in index:
                out.append(f'<c r="{col}{n}" t="s"><v>{index[value]}</v></c>')
            else:
                out.append(f'<c r="{col}{n}"><v>{value}</v></c>')
        return f'<row r="{n}">{"".join(out)}</row>'

    def sheet(rows: list[str]) -> str:
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f'<sheetData>{"".join(rows)}</sheetData></worksheet>')

    codes = sheet([row(1, [("A", "Code"), ("B", "Meaning")]), row(2, [("A", "D"), ("B", "Draft")]), row(3, [("A", "S"), ("B", "Submitted")]),
                   row(4, [("A", "A"), ("B", "Approved")]), row(5, [("A", "X"), ("B", "Cancelled")])])
    counts = sheet([row(1, [("A", "Code"), ("B", "Count")]), row(2, [("A", "D"), ("B", "=COUNTIF(Orders!C:C,A2)|12")]),
                    row(3, [("A", "S"), ("B", "=COUNTIF(Orders!C:C,A3)|7")]), row(4, [("A", "A"), ("B", "=COUNTIF(Orders!C:C,A4)")])])
    shared = "".join(f"<si><t>{escape(s)}</t></si>" for s in strings)
    _zip(HERE / "base" / "docs" / "order-codes.xlsx", {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                            '<sheet name="Order status" sheetId="1" r:id="rId1"/><sheet name="Counts" sheetId="2" r:id="rId2"/></sheets></workbook>'),
        "xl/_rels/workbook.xml.rels": ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                                       '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
                                       '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/></Relationships>'),
        "xl/sharedStrings.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(strings)}" uniqueCount="{len(strings)}">{shared}</sst>',
        "xl/worksheets/sheet1.xml": codes,
        "xl/worksheets/sheet2.xml": counts,
    })


if __name__ == "__main__":
    docx()
    xlsx()
