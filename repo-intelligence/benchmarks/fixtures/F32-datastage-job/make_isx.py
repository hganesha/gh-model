"""Write the Information Server archive of each layer of this fixture that has one.

    python benchmarks/fixtures/F32-datastage-job/make_isx.py

Run once; the `.isx` files it writes are committed. Each is a zip archive, as `istool
export` writes one: a manifest under `META-INF/`, the job `LoadOrders` as a DataStage
export in the XML form (stored without compression, so evidence spans are exact) and a
parameter set file the pack does not read (compressed). Member dates are fixed, so the
archives are byte-for-byte reproducible.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
TARGET = "datastage/isx/orders.isx"
DATE = (2024, 3, 4, 10, 0, 0)
LAYERS = ["base", "commits/01-filter-orders"]

SELECT = ("SELECT ORDER_ID, CUST_ID, AMOUNT, STATUS, ORDER_DATE FROM SALES.ORDERS "
          "WHERE ORDER_DATE >= TO_DATE('#RUN_DATE#', 'YYYY-MM-DD')")


def prop(name: str, value: str) -> str:
    return f'<Property Name="{name}">{escape(value)}</Property>'


def subrecords(name: str, kind: str, rows: list[dict[str, str]]) -> str:
    body = "".join("<SubRecord>" + "".join(prop(k, v) for k, v in row.items()) + "</SubRecord>" for row in rows)
    return f'<Collection Name="{name}" Type="{kind}">{body}</Collection>'


def record(ident: str, kind: str, props: dict[str, str], *collections: str) -> str:
    return f'<Record Identifier="{ident}" Type="{kind}" Readonly="0">' + "".join(prop(k, v) for k, v in props.items()) + "".join(collections) + "</Record>"


def connector(select: str) -> str:
    return ("<?xml version='1.0' encoding='UTF-16'?><Properties version='1.1'><Connection>"
            "<Server modified='1' type='string'><![CDATA[#DW_SERVER#]]></Server></Connection><Usage>"
            f"<SQL><SelectStatement modified='1' type='string'><![CDATA[{select}]]></SelectStatement></SQL></Usage></Properties>")


def export(layer: int) -> str:
    stages = ["V0S0", "V0S1", "V0S2"] + (["V0S3"] if layer >= 1 else [])
    agg_input = "V0S3" if layer >= 1 else "V0S0"
    records = [
        record("ROOT", "CJobDefn", {"Name": "LoadOrders", "Description": "Order totals per customer", "Category": "\\Jobs\\Orders", "JobType": "3"},
               subrecords("Parameters", "CParameters", [{"Name": "RUN_DATE", "Prompt": "Business date", "Default": "2024-01-01", "ParamType": "0"}])),
        record("V0", "CContainerView", {"Name": "Job", "StageList": "|".join(stages)}),
        record("V0S0", "CCustomStage", {"Name": "src_orders", "OutputPins": "V0S0P1", "StageType": "OracleConnectorPX"}),
        record("V0S0P1", "CCustomOutput", {"Name": "lnk_orders", "Partner": f"{agg_input}|{agg_input}P1"},
               subrecords("Properties", "CCustomProperty", [{"Name": "XMLProperties", "Value": connector(SELECT)}])),
        record("V0S1", "CCustomStage", {"Name": "agg_totals", "InputPins": "V0S1P1", "OutputPins": "V0S1P2", "StageType": "PxAggregator"},
               subrecords("Properties", "CCustomProperty", [{"Name": "key", "Value": "\x02\x01\x03key\x02CUST_ID\x01"}])),
        record("V0S1P1", "CCustomInput", {"Name": "lnk_valid" if layer >= 1 else "lnk_orders", "Partner": "V0S3|V0S3P2" if layer >= 1 else "V0S0|V0S0P1"}),
        record("V0S1P2", "CCustomOutput", {"Name": "lnk_totals", "Partner": "V0S2|V0S2P1"},
               subrecords("Columns", "COutputColumn", [{"Name": "CUST_ID", "SqlType": "4"}, {"Name": "TOTAL_AMOUNT", "SqlType": "3"}])),
        record("V0S2", "CCustomStage", {"Name": "tgt_totals", "InputPins": "V0S2P1", "StageType": "PxDataSet"}),
        record("V0S2P1", "CCustomInput", {"Name": "lnk_totals", "Partner": "V0S1|V0S1P2"},
               subrecords("Properties", "CCustomProperty", [{"Name": "dataset", "Value": "\x02\x01\x03dataset\x02/data/work/order_totals.ds\x01"}])),
    ]
    if layer >= 1:
        records += [
            record("V0S3", "CCustomStage", {"Name": "flt_open", "InputPins": "V0S3P1", "OutputPins": "V0S3P2", "StageType": "PxFilter"},
                   subrecords("Properties", "CCustomProperty", [{"Name": "where", "Value": "\x02\x01\x03where\x02STATUS <> 'CANCELLED'\x01"}])),
            record("V0S3P1", "CCustomInput", {"Name": "lnk_orders", "Partner": "V0S0|V0S0P1"}),
            record("V0S3P2", "CCustomOutput", {"Name": "lnk_valid", "Partner": "V0S1|V0S1P1"}),
        ]
    # A DataStage XML export writes control characters in property values as \(hh).
    body = "".join(records).replace("\x01", "\\(1)").replace("\x02", "\\(2)").replace("\x03", "\\(3)")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<DSExport>'
            '<Header CharacterSet="UTF-8" ExportingTool="IBM InfoSphere DataStage Export" ToolVersion="8" ServerVersion="11.7"/>'
            f'<Job Identifier="LoadOrders">{body}</Job></DSExport>\n')


def write(path: Path, layer: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo("META-INF/IS-MANIFEST.MF", DATE)
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, "<manifest><asset type='DataStage Job' name='LoadOrders'/></manifest>\n")
        info = zipfile.ZipInfo("DWH/Jobs/Orders/LoadOrders.pjb", DATE)
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, export(layer).encode("utf-8"))
        info = zipfile.ZipInfo("DWH/ParameterSets/ps_dw.pst", DATE)
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, "PARAMSET ps_dw\n  DW_SCHEMA\n")


if __name__ == "__main__":
    for i, layer in enumerate(LAYERS):
        write(HERE / layer / TARGET, i)
