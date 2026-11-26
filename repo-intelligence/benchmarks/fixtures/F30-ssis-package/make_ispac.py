"""Write the project deployment file of this fixture's second commit.

    python benchmarks/fixtures/F30-ssis-package/make_ispac.py

Run once; the `.ispac` it writes is committed. It is a zip archive holding
`@Project.manifest`, the project's packages, its connection manager and `Project.params`,
as the project stands after the first commit. Packages are compressed and the other
members are stored, so both ways of reading a member are exercised; the stored
connection manager carries the fixture's password literal in plain bytes, which
admission masks. Member dates are fixed, so the archive is byte-for-byte reproducible.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCES = [HERE / "base" / "etl" / "SalesLoad", HERE / "commits" / "01-returns-package" / "etl" / "SalesLoad"]
TARGET = HERE / "commits" / "02-deployment-archive" / "deploy" / "SalesLoad.ispac"
DATE = (2026, 1, 1, 0, 0, 0)
ENTRY_POINTS = {"LoadSales.dtsx": True, "LoadReturns.dtsx": True, "Publish.dtsx": False}

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="dtsx" ContentType="text/xml" />
  <Default Extension="conmgr" ContentType="text/xml" />
  <Default Extension="params" ContentType="text/xml" />
  <Default Extension="manifest" ContentType="text/xml" />
</Types>
"""


def manifest(packages: list[str]) -> str:
    rows = "\n".join(f'    <SSIS:Package SSIS:Name="{p}" SSIS:EntryPoint="{int(ENTRY_POINTS.get(p, False))}" />' for p in packages)
    return f"""<?xml version="1.0"?>
<SSIS:Project SSIS:ProtectionLevel="DontSaveSensitive" xmlns:SSIS="www.microsoft.com/SqlServer/SSIS">
  <SSIS:Properties>
    <SSIS:Property SSIS:Name="Name">SalesLoad</SSIS:Property>
    <SSIS:Property SSIS:Name="FormatVersion">1</SSIS:Property>
  </SSIS:Properties>
  <SSIS:Packages>
{rows}
  </SSIS:Packages>
  <SSIS:ConnectionManagers>
    <SSIS:ConnectionManager SSIS:Name="Warehouse.conmgr" />
  </SSIS:ConnectionManagers>
</SSIS:Project>
"""


def members() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for source in SOURCES:
        for path in sorted(source.iterdir()):
            if path.is_file():
                files[path.name] = path.read_bytes()
    packages = sorted(n for n in files if n.endswith(".dtsx"))
    files["@Project.manifest"] = manifest(packages).encode()
    files["[Content_Types].xml"] = CONTENT_TYPES.encode()
    return files


def build() -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in sorted(members().items()):
            info = zipfile.ZipInfo(name, DATE)
            info.compress_type = zipfile.ZIP_DEFLATED if name.endswith(".dtsx") else zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return buf.getvalue()


if __name__ == "__main__":
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(build())
    print(TARGET.relative_to(HERE))
