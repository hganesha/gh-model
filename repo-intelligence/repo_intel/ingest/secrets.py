"""Secret scanning at admission and masking at render time (spec §5 Stage 0, §13).

Findings record a detector and a byte span, never the value. Every excerpt
rendered for a consumer or a model passes through `mask`.
"""

from __future__ import annotations

import re
from typing import Any

SCAN_VERSION = "5"
# URL user information `scheme://user:password@`: group 1 is the user, group 2 the password.
URL_CREDENTIALS = re.compile(rb"://([^/\s:@]+):([^@\s/]{3,})@")
# Passwords that come with a product and are published in its documentation (the `postgres`
# superuser, RabbitMQ `guest`, MinIO `minioadmin`) or that only say a password goes here.
_DEFAULT_PASSWORDS = frozenset({
    b"admin", b"changeit", b"changeme", b"guest", b"minioadmin", b"pass", b"passw0rd", b"password",
    b"postgres", b"root", b"secret", b"test",
})

# A servlet `<init-param>` or `<context-param>` (web.xml) whose name says it holds a
# password, a secret or a key: the `<param-value>` that follows the `<param-name>`. The
# name must end with the word (`mail.password`, `clientSecret`, `apiKey`), so a name that
# only mentions it (`passwordParameter`, `keyGenerator`) names something else.
_PARAM_VALUE = re.compile(
    rb"(?i)<param-name>\s*[^<]*?(?:passw(?:or)?d|pwd|secret|key)\s*</param-name>\s*(?:<!--(?:(?!-->)[\s\S]){0,500}-->\s*)*"
    rb"<param-value>\s*([^<\s][^<]*?)\s*</param-value>"
)
# `Password=` or `Pwd=` inside a connection string (ADO.NET, ODBC, JDBC-style key=value
# lists, as in Web.config `connectionStrings` and `appSettings`): the pair starts a quoted
# string or follows a `;`, and its value runs to the next `;` or quote (an XML character
# reference such as `&amp;` inside an attribute value is part of the value).
_CONNECTION_PASSWORD = re.compile(rb"(?i)[;\"'][ \t]*(?:password|pwd)[ \t]*=[ \t]*((?:&[#\w]{1,10};|[^;\"'\r\n<>])+?)[ \t]*(?=[;\"'\r\n]|$)")
_PLACEHOLDER = re.compile(rb"^(?:\$\{|\#\{|\$\(|%|\{|@|\[)")
_DOTTED_NAME = re.compile(rb"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+$")


def _secret_like(value: bytes) -> bool:
    """A value that may be a real secret, rather than a placeholder, a reference or a word.

    A flagged value is masked before extraction and the publish gate fails a snapshot whose
    output contains it anywhere, so a short or common value (`desk`, `password`, `true`, a
    class name, a file path) would block publishing whenever the same text names something
    else. Kept: at least 8 characters with no space, not a placeholder (`${x}`, `%X%`,
    `{0}`), not a dotted name or a path, and either holding a character that is not a
    letter or at least 16 characters long.
    """
    if len(value) < 8 or len(value) > 200 or re.search(rb"[\s/\\:]", value):
        return False
    if _PLACEHOLDER.match(value) or _DOTTED_NAME.match(value):
        return False
    return len(value) >= 16 or not value.isalpha()


def _any(value: bytes) -> bool:
    return True


def url_password(user: bytes, password: bytes) -> bool:
    """Whether the password in `scheme://user:password@` may be a real one. Not: a reference
    to another value (`${DB_PASSWORD}`, `$PGPASS`, `%PW%`), the user name again
    (`postgres:postgres`), or a product default (`password`, `guest`). A local compose file
    or test fixture full of `postgres://postgres:postgres@db` holds no secret, and treating
    the word as one fails the publish gate wherever the same word names a driver, a service
    or a test. A short password that is none of these (`hunter2`) is still found."""
    if _PLACEHOLDER.match(password) or password.startswith(b"$"):
        return False
    return password.lower() != user.lower() and password.lower() not in _DEFAULT_PASSWORDS


def url_passwords(text: bytes) -> list[bytes]:
    """The passwords in `text`'s URL user information that `url_password` keeps."""
    return [m[2] for m in URL_CREDENTIALS.finditer(text) if url_password(m[1], m[2])]


def _credential_value(value: bytes) -> bool:
    """`_secret_like`, and not a reference to another value: a SQL*Plus substitution
    (`&pwd`), a bind variable (`:pwd`) or another parameter (`$PMPassword`, `$$DB_PWD`)."""
    return _secret_like(value) and not value.startswith((b"$", b"&", b":"))


# Oracle `CREATE USER x IDENTIFIED BY pw` and `CREATE DATABASE LINK ... IDENTIFIED BY "pw"`;
# `IDENTIFIED BY VALUES '<hash>'` carries a hash, not the password, and is left alone.
_IDENTIFIED_BY = re.compile(rb"(?i)\bidentified\s+by\s+(\"?)(?!values\b)([^\s\";]+)\1")
# An Informatica parameter file (.par, .prm, .parm) line `$$DB_PASSWORD=value` or
# `$DBConnection_Pwd=value`: a parameter whose name says it holds a credential.
_PARAMETER_FILE_CREDENTIAL = re.compile(rb"(?im)^\s*\$\$?\w*(?:password|passwd|pwd|secret)\w*\s*=\s*(\S+)")

# Mainframe and midrange sources (docs/solution-ir-contract.md, section 11): a quoted literal
# MOVEd to, or given as the VALUE of, a COBOL data item whose name says it holds a credential;
# a PL/I INIT of such a variable; a CL password keyword (RMTPWD, PASSWORD, PWD).
_CREDENTIAL_NAME = rb"[\w#@$-]*(?:PASSW|PWD|SECRET|TOKEN)[\w#@$-]*"
_COBOL_MOVE_CREDENTIAL = re.compile(rb"(?i)\bMOVE\s+(['\"])([^'\"\r\n]{8,160})\1\s+TO\s+" + _CREDENTIAL_NAME)
_COBOL_VALUE_CREDENTIAL = re.compile(rb"(?i)\b\d{2}\s+" + _CREDENTIAL_NAME + rb"\s+PIC(?:TURE)?\s+(?:IS\s+)?\S+(?:\s+USAGE\s+(?:IS\s+)?\S+)?\s+VALUE\s+(?:IS\s+)?(['\"])([^'\"\r\n]{8,160})\1")
_PLI_INIT_CREDENTIAL = re.compile(rb"(?i)\b" + _CREDENTIAL_NAME + rb"\s[^;'\r\n]{0,80}?\bINIT(?:IAL)?\s*\(\s*'([^'\r\n]{8,160})'")
_CL_PASSWORD_KEYWORD = re.compile(rb"(?i)\b(?:RMTPWD|PASSWORD|PWD)\(\s*'?([^)'\s]{8,})'?\s*\)")


# (detector id, pattern, group whose span is the secret)
_DETECTORS: list[tuple[str, re.Pattern[bytes], int]] = [
    ("private_key_block", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), 0),
    ("aws_access_key_id", re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    ("anthropic_api_key", re.compile(rb"sk-ant-[A-Za-z0-9_\-]{20,}"), 0),
    ("github_token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), 0),
    ("url_credentials", URL_CREDENTIALS, 2),
    # A secret-named key assigned a QUOTED literal, or a .env-style line. An unquoted
    # right-hand side in code is an expression (`token = make_token(...)`), not a value.
    # Group 1 ends the key, so `_names_itself` can see it.
    ("generic_assignment", re.compile(rb"(?i)(secret|token|passw(?:or)?d|api[_-]?key)[\"']?\s*[:=]\s*[\"']([A-Za-z0-9/+_\-]{20,})[\"']"), 2),
    ("env_assignment", re.compile(rb"(?im)^([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_?KEY)[A-Z0-9_]*)=([^\s\"'#$]{20,})\s*$"), 2),
    ("servlet_param_value", _PARAM_VALUE, 1),
    ("connection_string_password", _CONNECTION_PASSWORD, 1),
    ("oracle_identified_by", _IDENTIFIED_BY, 2),
    ("parameter_file_credential", _PARAMETER_FILE_CREDENTIAL, 1),
    ("cobol_move_credential", _COBOL_MOVE_CREDENTIAL, 2),
    ("cobol_value_credential", _COBOL_VALUE_CREDENTIAL, 2),
    ("pli_init_credential", _PLI_INIT_CREDENTIAL, 1),
    ("cl_password_keyword", _CL_PASSWORD_KEYWORD, 1),
]
# Detectors whose match is a finding only when the value passes a check.
_VALUE_CHECKS = {
    "servlet_param_value": _secret_like, "connection_string_password": _secret_like,
    "oracle_identified_by": _credential_value, "parameter_file_credential": _credential_value,
    "cobol_move_credential": _credential_value, "cobol_value_credential": _credential_value,
    "pli_init_credential": _credential_value, "cl_password_keyword": _credential_value,
}


_KEY_END = re.compile(rb"[\w.-]*$")
_NOT_ALNUM = re.compile(rb"[^a-z0-9]")


def _names_itself(match: re.Match[bytes]) -> bool:
    """A value that is its own key re-cased names the secret and does not hold it:
    `DATABASE_MIGRATOR_PASSWORD: "database-migrator-password"` maps a setting to the
    Key Vault secret of that name, which the deployment templates name too."""
    data, end = match.string, match.end(1)
    key = _KEY_END.search(data, max(0, end - 200), end)[0]
    return _NOT_ALNUM.sub(b"", key.lower()) == _NOT_ALNUM.sub(b"", match[2].lower())


# Detectors whose match is a finding only when the whole match passes a check.
_MATCH_CHECKS = {
    "url_credentials": lambda m: url_password(m[1], m[2]),
    "generic_assignment": lambda m: not _names_itself(m),
    "env_assignment": lambda m: not _names_itself(m),
}


def _finding(name: str, match: re.Match[bytes], group: int) -> bool:
    check = _MATCH_CHECKS.get(name)
    return _VALUE_CHECKS.get(name, _any)(match.group(group)) and (check is None or check(match))


def scan(data: bytes) -> list[dict[str, Any]]:
    """Non-overlapping findings, ordered by position: {detector, start_byte, end_byte}."""
    found = sorted(
        ((m.start(g), m.end(g), name) for name, pattern, g in _DETECTORS for m in pattern.finditer(data) if _finding(name, m, g)),
        key=lambda f: (f[0], -f[1]),
    )
    out: list[dict[str, Any]] = []
    for start, end, name in found:
        if out and start < out[-1]["end_byte"]:
            continue
        out.append({"detector": name, "start_byte": start, "end_byte": end})
    return out


def mask(data: bytes, start: int, end: int, findings: list[dict[str, Any]]) -> tuple[bytes, bool]:
    """Bytes [start, end) of `data` with every overlapping finding replaced by a marker."""
    out, cursor, masked = bytearray(), start, False
    for f in findings:
        if f["end_byte"] <= cursor or f["start_byte"] >= end:
            continue
        out += data[cursor:max(cursor, f["start_byte"])]
        out += f"[REDACTED:{f['detector']}]".encode()
        cursor, masked = min(end, f["end_byte"]), True
    out += data[cursor:end]
    return bytes(out), masked


def mask_for_extraction(data: bytes, findings: list[dict[str, Any]]) -> bytes:
    """Remove detected values before parsing, preserving source byte and line offsets."""
    if not findings:
        return data
    masked = bytearray(data)
    for finding in findings:
        start, end = finding["start_byte"], finding["end_byte"]
        original = data[start:end]
        marker = b"~" if b"*" in original else b"*"
        masked[start:end] = re.sub(rb"[^\r\n]", marker, original)
    return bytes(masked)


def values(data: bytes, findings: list[dict[str, Any]]) -> list[bytes]:
    """The flagged byte strings, for in-memory leak checks only. Never persist these."""
    return [data[f["start_byte"]:f["end_byte"]] for f in findings]
