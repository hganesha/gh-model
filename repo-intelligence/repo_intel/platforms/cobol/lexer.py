"""Reference format and tokens of COBOL source.

`read(text)` decides the source format and cuts the file into tokens.

- **Fixed format** (the default for z/OS and IBM i sources): columns 1 to 6 are the
  sequence area, column 7 the indicator, columns 8 to 72 the program text and columns 73
  to 80 the identification area. Indicator `*` or `/` makes a comment line, `D` or `d` a
  debugging line (read as a comment, as the compiler does without `WITH DEBUGGING MODE`),
  and `-` a continuation line: a word continues the last word of the line before, and an
  alphanumeric literal left open at column 72 continues after the first quote of the
  continuation line. A leading tab stands for the sequence and indicator areas.
- **Free format** (`>>SOURCE FORMAT IS FREE`, or a file whose lines do not keep the
  sequence and indicator columns): the whole line is program text.

In both, `*>` outside a literal starts a comment to the end of the line, and `>>`
compiler directives and `CBL` / `PROCESS` option cards are skipped. A file may switch
format with `>>SOURCE FORMAT`.

`Source.code` is the text with everything that is not program text blanked, so it has the
same length and every offset stays an offset into the file (the SQL scanner reads it).
Tokens keep their character span in the file. Separator commas and semicolons are
dropped; a picture string after `PIC` or `PICTURE` is one token. Nothing is evaluated;
the scan is linear in the size of the text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

WORD, NUM, STR, PIC, PUNCT, PERIOD, PSEUDO = "word", "num", "str", "pic", "punct", "period", "pseudo"

_WORD_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_$#@")
_LETTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz$#@_")
_DIGITS = frozenset("0123456789")
_SPACE = frozenset(" \t\r\f\v")
_LITERAL_PREFIXES = ("NX", "X", "N", "G", "Z", "U", "B")  # hex, national, DBCS, null-terminated, UTF-8, boolean


@dataclass(frozen=True)
class Tok:
    kind: str
    text: str  # as written; a literal's value for STR
    start: int  # character offsets into the source text
    end: int
    prefix: str = ""  # a literal's prefix (X, N, G, Z...)
    col: int = 0  # column of the token in its line, counted from 1 in the program text area

    @property
    def up(self) -> str:
        return self.text.upper() if self.kind == WORD else self.text

    def is_(self, *words: str) -> bool:
        return self.kind == WORD and self.text.upper() in words

    def punct(self, *marks: str) -> bool:
        return self.kind == PUNCT and self.text in marks


@dataclass
class Source:
    text: str
    code: str  # the text with non-program text blanked; same length
    format: str  # "fixed" or "free" (the format of the first line of program text)
    tokens: list[Tok]
    comment_lines: int = 0
    debug_lines: int = 0
    continuation_lines: int = 0
    directives: list[tuple[int, int]] = field(default_factory=list)  # spans of >> directives and CBL/PROCESS cards
    area_a: set[int] = field(default_factory=set)  # token start offsets that begin in Area A (fixed format, columns 8 to 11)


def _is_fixed_line(line: str) -> bool:
    """A line that keeps the sequence area (blank, digits or a short label) and an indicator column."""
    if line.startswith("\t"):
        return True
    if len(line) <= 6:
        return line.strip().isdigit()
    head = line[:6]
    return (head.strip() == "" or head.strip().isdigit() or head.isalnum()) and line[6] in " *-/Dd"


def detect_fixed(lines: list[str]) -> bool:
    """Whether a file keeps the sequence and indicator columns: most non-blank lines do."""
    meaningful = [l for l in lines if l.strip()]
    if not meaningful:
        return True
    return sum(1 for l in meaningful if _is_fixed_line(l)) * 2 >= len(meaningful)


def _directive(area: str) -> str | None:
    """The format a `>>SOURCE` directive switches to, or "" for another directive, None for program text."""
    s = area.lstrip()
    if s.startswith(">>"):
        up = s.upper().replace(" ", "")
        if up.startswith(">>SOURCE"):
            if "FREE" in up:
                return "free"
            if "FIXED" in up:
                return "fixed"
        return ""
    if s[:5].upper() == "$SET " and "SOURCEFORMAT" in s.upper():  # Micro Focus
        return "free" if "FREE" in s.upper() else "fixed"
    return None


class _Lexer:
    def __init__(self, text: str):
        self.text = text
        self.code = list(" " * len(text))
        self.toks: list[Tok] = []
        self.src = Source(text, "", "fixed", self.toks)
        self.pending: tuple[str, int, str, list[str]] | None = None  # (quote, start, prefix, parts) of a literal left open at the end of a line
        self.glue = False  # the next token continues the last one (a continuation line)
        self.after_pic = 0  # 1 after PIC/PICTURE, 2 after PIC IS: the next run of characters is a picture string
        self.last_line_tokens = 0

    # ------------------------------------------------------------------ lines

    def run(self) -> Source:
        text = self.text
        lines = text.split("\n")
        fixed = detect_fixed(lines)
        first_format: str | None = None
        pos = 0
        cbl_cards_allowed = True
        for line in lines:
            start, end = pos, pos + len(line)
            pos = end + 1
            for i in range(start, end):
                if text[i] == "\n":
                    self.code[i] = "\n"
            if pos - 1 < len(text):
                self.code[pos - 1] = "\n"
            if fixed:
                if line.startswith("\t"):
                    a, b, indicator = start + 1, end, " "
                elif len(line) <= 6:
                    continue
                else:
                    a, b, indicator = start + 7, start + min(len(line), 72), line[6]
            else:
                a, b, indicator = start, end, " "
                stripped = line.lstrip()
                if stripped.startswith("*>"):
                    self.src.comment_lines += 1
                    continue
                if stripped[:2].upper() == "D " and not self.pending:
                    self.src.debug_lines += 1
                    continue
            if indicator in "*/":
                self.src.comment_lines += 1
                continue
            if indicator in "Dd":
                self.src.debug_lines += 1
                continue
            area = text[a:b]
            switch = _directive(area)
            if switch is not None:
                self.src.directives.append((a, b))
                if switch:
                    fixed = switch == "fixed"
                continue
            word = area.lstrip()[:8].upper()
            if cbl_cards_allowed and (word.startswith("CBL ") or word.startswith("PROCESS ") or word in ("CBL", "PROCESS")):
                self.src.directives.append((a, b))
                continue
            if area.strip():
                if first_format is None:
                    first_format = "fixed" if fixed else "free"
                cbl_cards_allowed = False
            continuation = indicator == "-"
            if continuation:
                self.src.continuation_lines += 1
            self.line(a, b, continuation, fixed)
        if self.pending:
            self.close_pending(len(text))
        self.src.code = "".join(self.code)
        self.src.format = first_format or ("fixed" if detect_fixed(lines) else "free")
        return self.src

    def close_pending(self, at: int) -> None:
        quote, start, prefix, parts = self.pending
        self.toks.append(Tok(STR, "".join(parts), start, at, prefix))
        self.pending = None

    # ------------------------------------------------------------------ tokens of one line

    def emit(self, kind: str, text: str, a: int, b: int, prefix: str = "", col: int = 0) -> None:
        if self.glue and self.toks and kind in (WORD, NUM) and self.toks[-1].kind in (WORD, NUM):
            prev = self.toks.pop()
            joined = prev.text + text
            kind = WORD if any(c in _LETTERS for c in joined) else NUM
            self.toks.append(Tok(kind, joined, prev.start, b, col=prev.col))
            self.glue = False
            return
        self.glue = False
        self.toks.append(Tok(kind, text, a, b, prefix, col))

    def line(self, a: int, b: int, continuation: bool, fixed: bool) -> None:
        text = self.text
        i = a
        if continuation:
            if self.pending:
                while i < b and text[i] in _SPACE:
                    i += 1
                quote = self.pending[0]
                if i < b and text[i] == quote:
                    i = self._literal(i + 1, b, quote, resume=True, fixed=fixed)
                else:
                    self.close_pending(a)
            else:
                self.glue = True
        elif self.pending:
            self.close_pending(a)
        while i < b:
            c = text[i]
            if c in _SPACE:
                i += 1
                continue
            col = i - a + 1
            if c == "*" and i + 1 < b and text[i + 1] == ">":
                break  # inline comment to the end of the line
            self.code[i] = c
            if self.after_pic:
                j = i
                while j < b and text[j] not in _SPACE:
                    j += 1
                pic_end = j
                if pic_end - 1 > i and text[pic_end - 1] in ".,;" and (pic_end == b or text[pic_end] in _SPACE):
                    pic_end -= 1  # the separator period after a picture string
                if text[i:pic_end].upper() == "IS" and self.after_pic == 1:
                    self._copy(i, pic_end)
                    self.emit(WORD, text[i:pic_end], i, pic_end, col=col)
                    self.after_pic = 2
                    i = pic_end
                    continue
                self._copy(i, pic_end)
                self.emit(PIC, text[i:pic_end], i, pic_end, col=col)
                self.after_pic = 0
                i = pic_end
                continue
            if c in "'\"":
                i = self._literal(i + 1, b, c, start=i, fixed=fixed)
                continue
            if c in _WORD_CHARS:
                j = i
                while j < b and text[j] in _WORD_CHARS:
                    j += 1
                word = text[i:j]
                if not any(ch in _LETTERS or ch in _DIGITS for ch in word):  # a minus sign, not a word
                    self._copy(i, j)
                    for k in range(i, j):
                        self.emit(PUNCT, text[k], k, k + 1, col=k - a + 1)
                    i = j
                    continue
                if j < b and text[j] in "'\"" and word.upper() in _LITERAL_PREFIXES:
                    self._copy(i, j)
                    i = self._literal(j + 1, b, text[j], start=i, prefix=word.upper(), fixed=fixed)
                    continue
                # A decimal number: digits, a point, digits (the point is not a separator period).
                if not any(ch in _LETTERS for ch in word) and j + 1 < b and text[j] in ".," and text[j + 1] in _DIGITS and "-" not in word.strip("-"):
                    k = j + 1
                    while k < b and text[k] in _DIGITS:
                        k += 1
                    self._copy(i, k)
                    self.emit(NUM, text[i:k], i, k, col=col)
                    i = k
                    continue
                self._copy(i, j)
                kind = WORD if any(ch in _LETTERS for ch in word) else NUM
                self.emit(kind, word, i, j, col=col)
                if kind == WORD and word.upper() in ("PIC", "PICTURE"):
                    self.after_pic = 1
                i = j
                continue
            if c == ".":
                if i + 1 >= b or text[i + 1] in _SPACE or text[i + 1] == "\n":
                    self.emit(PERIOD, ".", i, i + 1, col=col)
                    i += 1
                    continue
                if text[i + 1] in _DIGITS:  # .5
                    k = i + 1
                    while k < b and text[k] in _DIGITS:
                        k += 1
                    self._copy(i, k)
                    self.emit(NUM, text[i:k], i, k, col=col)
                    i = k
                    continue
                self.emit(PUNCT, ".", i, i + 1, col=col)
                i += 1
                continue
            if c in ",;" and (i + 1 >= b or text[i + 1] in _SPACE):
                i += 1  # a separator, like a space
                continue
            if c == "=" and i + 1 < b and text[i + 1] == "=":
                k = text.find("==", i + 2, b)
                stop = k if k >= 0 else b
                self._copy(i, min(b, stop + 2))
                self.emit(PSEUDO, text[i + 2 : stop].strip(), i, min(b, stop + 2), col=col)
                i = min(b, stop + 2)
                continue
            two = text[i : i + 2]
            if two in ("**", "<=", ">=", "<>"):
                self._copy(i, i + 2)
                self.emit(PUNCT, two, i, i + 2, col=col)
                i += 2
                continue
            self.emit(PUNCT, c, i, i + 1, col=col)
            i += 1
        if self.glue and not continuation:
            self.glue = False

    def _copy(self, a: int, b: int) -> None:
        for k in range(a, b):
            self.code[k] = self.text[k]

    def _literal(self, i: int, b: int, quote: str, start: int | None = None, prefix: str = "", resume: bool = False, fixed: bool = True) -> int:
        """Scan a literal's content from `i`; returns the offset after it."""
        text = self.text
        first = i - 1 if resume else start  # the first character of this line's part, its quote or prefix
        if resume:
            q, start, prefix, parts = self.pending
            self.pending = None
        else:
            parts = []
        seg = i
        while i < b:
            if text[i] == quote:
                if i + 1 < b and text[i + 1] == quote:
                    parts.append(text[seg : i + 1])
                    i += 2
                    seg = i
                    continue
                parts.append(text[seg:i])
                self._copy(first, i + 1)
                self.toks.append(Tok(STR, "".join(parts), start, i + 1, prefix))
                return i + 1
            i += 1
        # Left open at the end of the line: a continuation line may carry it on. In fixed
        # format the literal runs to column 72, so a short line's missing columns are spaces.
        parts.append(text[seg:b])
        if fixed:
            line_start = text.rfind("\n", 0, b) + 1
            width = b - line_start
            if width < 72 and text[line_start : line_start + 1] != "\t":
                parts.append(" " * (72 - width))
        self._copy(first, b)
        self.pending = (quote, start, prefix, parts)
        return b


def read(text: str) -> Source:
    """The source format, program text and tokens of a COBOL file."""
    src = _Lexer(text).run()
    if src.format == "fixed":
        for t in src.tokens:
            if 1 <= t.col <= 4:
                src.area_a.add(t.start)
    return src


__all__ = ["NUM", "PERIOD", "PIC", "PSEUDO", "PUNCT", "STR", "WORD", "Source", "Tok", "detect_fixed", "read"]
