"""Values the SAS pack never copies (docs/solution-ir-contract.md, section 2, rule 7).

A URL keeps its scheme, host, path and query parameter names; its user information,
query values and fragment are dropped, so `https://u:p@host/api?token=abc#top` is
written `https://host/api?token=`. A reference without a scheme is treated the same
way when it has a query string (`do?_program=x&region=N` is written
`do?_program=&region=`). A mail address is replaced by `<email>`.

Everything the pack writes into behaviour units, entity attributes and diagnostics
goes through `scrub` or `scrub_tree`, so a literal copied from the source cannot carry
a value through.
"""

from __future__ import annotations

import re
from typing import Any

_STOP = "\\s\"'<>`"
# scheme://authority/path?query#fragment, up to whitespace, a quote or an angle bracket.
_ABSOLUTE = re.compile(rf"(?i)\b[a-z][a-z0-9+.\-]*://[^{_STOP}]*")
# A reference without a scheme whose query string starts with `name=`.
_RELATIVE = re.compile(rf"[^{_STOP}?#]*\?(?=[A-Za-z_\x00][\w.\[\]\x00-]*=)[^{_STOP}]*")
_MAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
MAIL = "<email>"


def _url(text: str) -> str:
    text = text.split("#", 1)[0]
    base, question, query = text.partition("?")
    if "://" in base:
        scheme, _, rest = base.partition("://")
        authority, slash, path = rest.partition("/")
        base = f"{scheme}://{authority.rpartition('@')[2]}{slash}{path}"
    if not question:
        return base
    names = [p.split("=", 1)[0] + ("=" if "=" in p else "") for p in re.split(r"[&;]", query) if p]
    return base + "?" + "&".join(names)


def scrub(text: str) -> str:
    """`text` with every URL reduced to its scheme, host, path and parameter names, and no mail address."""
    if "://" in text:
        text = _ABSOLUTE.sub(lambda m: _url(m.group()), text)
    if "?" in text:
        text = _RELATIVE.sub(lambda m: m.group() if "://" in m.group() else _url(m.group()), text)
    if "@" in text:
        text = _MAIL.sub(MAIL, text)
    return text


def scrub_tree(value: Any) -> Any:
    """Every string in a JSON-like value scrubbed; keys and other values unchanged.

    A query's `sql` is left as it is: it is scrubbed where it is made, before macro
    references become `:name` parameters, and scrubbing it again could drop a parameter
    from the text while it stays in `params`."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: v if k == "sql" and value.get("op") == "query" else scrub_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_tree(v) for v in value]
    return value
