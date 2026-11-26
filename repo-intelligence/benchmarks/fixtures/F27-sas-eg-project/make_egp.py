"""Write the Enterprise Guide project of each layer of this fixture.

    python benchmarks/fixtures/F27-sas-eg-project/make_egp.py

Run once; the `.egp` files it writes are committed. Each is a zip archive with
`project.xml` (UTF-16 with a byte-order mark, compressed, as Enterprise Guide writes
it) and one `<task id>/code.sas` member per embedded code task, some stored without
compression and some compressed, so both ways of reading a member are exercised.
Member dates are fixed, so the archives are byte-for-byte reproducible.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
TARGET = "eg/sales_reporting.egp"
DATE = (2026, 1, 1, 0, 0, 0)

EXTRACT = """/* Clean the raw orders loaded overnight. */
libname raw "/data/raw";
libname stage "/data/stage";
libname ora oracle user=desk password="fixture-not-a-real-password-0001" path=orcl;

data stage.orders_clean;
  set raw.orders;
  where status ne 'CANCELLED';
  if quantity <= 0 then delete;
  net = quantity * unit_price * (1 - discount);
run;
"""

SUMMARISE = """libname stage "/data/stage";
libname rpt "/data/reports";

proc sql;
  create table rpt.sales_summary as
  select region, product, sum(net) as net_sales, count(*) as orders
  from stage.orders_clean
  group by region, product;
quit;
"""

REGION_SPLIT = """libname rpt "/data/reports";

data rpt.sales_by_region;
  set rpt.sales_summary;
  by region;
  if first.region then region_total = 0;
  region_total + net_sales;
  if last.region then output;
run;
"""

WEEKLY_CHECK = """libname rpt "/data/reports";

proc means data=rpt.sales_summary noprint;
  var net_sales;
  output out=work.weekly_stats mean=avg_sales;
run;
"""


def element(kind: str, eid: str, label: str, container: str | None, body: str = "") -> str:
    head = f"<Element><Label>{escape(label)}</Label><Type>{kind.upper()}</Type><ID>{eid}</ID>"
    head += f"<Container>{container}</Container>" if container else ""
    head += "</Element>"
    return f'  <Element Type="SAS.EG.ProjectElements.{kind}">{head}{body}</Element>\n'


def code_task(eid: str, label: str, flow: str, embedded: bool, full_path: str | None = None) -> str:
    dna = escape(f"<DNA><Type>LocalFile</Type><FullPath>{full_path}</FullPath></DNA>") if full_path else ""
    body = f"<CodeTask><Embedded>{'True' if embedded else 'False'}</Embedded><DNA>{dna}</DNA></CodeTask>"
    return element("CodeTask", eid, label, flow, body)


def data_node(eid: str, label: str, flow: str, library: str, member: str) -> str:
    dna = escape(f"<DNA><Type>Dataset</Type><Server>SASApp</Server><Library>{library}</Library><Member>{member}</Member></DNA>")
    return element("ShortCutToData", eid, label, flow, f"<DNA>{dna}</DNA>")


def link(eid: str, src: str, dst: str) -> str:
    return element("Link", eid, f"{src} to {dst}", None, f"<LinkFrom>{src}</LinkFrom><LinkTo>{dst}</LinkTo>")


def project(layer: int) -> tuple[str, dict[str, tuple[str, int]]]:
    """project.xml and the code members of the project as it is at `layer` (0 is the base)."""
    parts = [element("PFD", "PFD-nightly", "Nightly load", None)]
    if layer < 3:
        parts.append(element("PFD", "PFD-monthly", "Monthly report", None))
    if layer >= 2:
        parts.append(element("PFD", "PFD-weekly", "Weekly check", None))
    parts += [
        data_node("SDS-raw-orders", "ORDERS", "PFD-nightly", "RAW", "ORDERS"),
        code_task("CodeTask-extract", "Extract orders", "PFD-nightly", True),
        code_task("CodeTask-summarise", "Summarise", "PFD-nightly", True),
        data_node("SDS-sales-summary", "SALES_SUMMARY", "PFD-nightly", "RPT", "SALES_SUMMARY"),
        element("EGTask", "EGTask-top", "Top customers", "PFD-nightly", "<TaskID>SAS.EG.Query</TaskID>"),
        element("Note", "Note-1", "Run after the warehouse load", "PFD-nightly", "<Text>Ask the data team before changing the schedule.</Text>"),
        link("Link-1", "SDS-raw-orders", "CodeTask-extract"),
        link("Link-2", "CodeTask-extract", "CodeTask-summarise"),
        link("Link-3", "CodeTask-summarise", "SDS-sales-summary"),
        link("Link-4", "SDS-sales-summary", "EGTask-top"),
    ]
    members = {"CodeTask-extract/code.sas": (EXTRACT, zipfile.ZIP_STORED), "CodeTask-summarise/code.sas": (SUMMARISE, zipfile.ZIP_DEFLATED)}
    if layer < 3:
        parts.append(code_task("CodeTask-monthly", "Monthly report", "PFD-monthly", False, "C:\\Users\\analyst\\reports\\monthly.sas"))
        parts.append(element("Gauge", "Gauge-1", "Dial", "PFD-monthly"))
    if layer >= 1:
        parts.append(code_task("CodeTask-region", "Region split", "PFD-nightly", True))
        parts.append(link("Link-5", "SDS-sales-summary", "CodeTask-region"))
        members["CodeTask-region/code.sas"] = (REGION_SPLIT, zipfile.ZIP_STORED)
    if layer >= 2:
        parts.append(code_task("CodeTask-weekly", "Weekly totals", "PFD-weekly", True))
        members["CodeTask-weekly/code.sas"] = (WEEKLY_CHECK, zipfile.ZIP_DEFLATED)
    xml = '<?xml version="1.0" encoding="utf-16"?>\n<Project EGVersion="8.3">\n <Elements>\n' + "".join(parts) + " </Elements>\n</Project>\n"
    return xml, members


def write(path: Path, layer: int) -> None:
    xml, members = project(layer)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo("project.xml", DATE)
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, xml.encode("utf-16"))
        for name, (text, method) in sorted(members.items()):
            info = zipfile.ZipInfo(name, DATE)
            info.compress_type = method
            zf.writestr(info, text.encode("utf-8"))


if __name__ == "__main__":
    layers = ["base", "commits/01-region-split", "commits/02-weekly-flow", "commits/03-retire-monthly"]
    for i, layer in enumerate(layers):
        write(HERE / layer / TARGET, i)
