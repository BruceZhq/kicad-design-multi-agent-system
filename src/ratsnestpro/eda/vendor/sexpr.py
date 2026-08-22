"""A small, dependency-free S-expression engine for KiCAD files.

KiCAD stores schematics (``.kicad_sch``), PCBs (``.kicad_pcb``) and several
other files as S-expressions. This module implements a parser and a writer
that preserve enough structure for lossless round-tripping of the data we
care about.

Design notes
------------
* Bare tokens (symbols, numbers) are kept as :class:`Atom` so that the exact
  textual form survives a parse -> write cycle. We never coerce ``100`` into
  the float ``100.0`` because KiCAD is sensitive to formatting in some fields.
* Double-quoted strings become plain :class:`str`. The writer always quotes
  them again and escapes the characters KiCAD expects.
* Lists become plain Python :class:`list` objects, so callers can freely
  inspect and mutate the tree with ordinary list operations.

This implementation is original work; it follows the publicly documented
KiCAD S-expression grammar and does not reuse code from any other project.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Union


class SexprError(ValueError):
    """Raised when the input is not a well-formed S-expression."""


@dataclass(frozen=True)
class Atom:
    """A bare (unquoted) token such as a symbol or a number.

    The original text is preserved verbatim so that writing the tree back
    out reproduces the token exactly.
    """

    value: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    def as_float(self) -> float:
        return float(self.value)

    def as_int(self) -> int:
        return int(self.value)


# A node is a quoted string, a bare atom, or a nested list of nodes.
Node = Union[str, Atom, List["Node"]]


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

_WHITESPACE = " \t\r\n"


def _tokenize(text: str) -> Iterator[str]:
    """Yield ``(``, ``)``, quoted strings (with quotes) and bare tokens."""
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in _WHITESPACE:
            i += 1
            continue
        if c == "(" or c == ")":
            yield c
            i += 1
            continue
        if c == '"':
            # Quoted string: consume until the matching unescaped quote.
            start = i
            i += 1
            buf = ['"']
            while i < n:
                ch = text[i]
                if ch == "\\" and i + 1 < n:
                    buf.append(text[i : i + 2])
                    i += 2
                    continue
                if ch == '"':
                    buf.append('"')
                    i += 1
                    break
                buf.append(ch)
                i += 1
            else:
                raise SexprError(f"unterminated string starting at offset {start}")
            yield "".join(buf)
            continue
        # Bare token: run until whitespace or a paren or a quote.
        start = i
        while i < n and text[i] not in _WHITESPACE and text[i] not in '()"':
            i += 1
        yield text[start:i]


_ESCAPES_IN = {
    "\\n": "\n",
    "\\t": "\t",
    "\\r": "\r",
    '\\"': '"',
    "\\\\": "\\",
}


def _unescape(quoted: str) -> str:
    """Decode a quoted token (with surrounding quotes) into a Python str."""
    inner = quoted[1:-1]
    out: List[str] = []
    i = 0
    n = len(inner)
    while i < n:
        if inner[i] == "\\" and i + 1 < n:
            pair = inner[i : i + 2]
            out.append(_ESCAPES_IN.get(pair, inner[i + 1]))
            i += 2
        else:
            out.append(inner[i])
            i += 1
    return "".join(out)


def loads(text: str) -> Node:
    """Parse a single top-level S-expression from ``text``."""
    tokens = list(_tokenize(text))
    if not tokens:
        raise SexprError("empty input")

    pos = 0

    def parse() -> Node:
        nonlocal pos
        if pos >= len(tokens):
            raise SexprError("unexpected end of input")
        tok = tokens[pos]
        if tok == "(":
            pos += 1
            items: List[Node] = []
            while pos < len(tokens) and tokens[pos] != ")":
                items.append(parse())
            if pos >= len(tokens):
                raise SexprError("missing closing parenthesis")
            pos += 1  # consume ")"
            return items
        if tok == ")":
            raise SexprError("unexpected ')'")
        pos += 1
        if tok.startswith('"'):
            return _unescape(tok)
        return Atom(tok)

    result = parse()
    if pos != len(tokens):
        raise SexprError("trailing data after top-level expression")
    return result


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

_ESCAPES_OUT = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\t": "\\t",
    "\r": "\\r",
}


def _escape(s: str) -> str:
    return "".join(_ESCAPES_OUT.get(ch, ch) for ch in s)


def _dump_node(node: Node, indent: int, out: List[str], pretty: bool) -> None:
    pad = "\t" * indent if pretty else ""
    if isinstance(node, Atom):
        out.append(node.value)
        return
    if isinstance(node, str):
        out.append(f'"{_escape(node)}"')
        return
    if isinstance(node, list):
        if not node:
            out.append("()")
            return
        # A list whose children are all leaves is written on a single line;
        # otherwise each child that is itself a list goes on its own line.
        has_list_child = any(isinstance(child, list) for child in node)
        if not has_list_child or not pretty:
            parts: List[str] = []
            for child in node:
                sub: List[str] = []
                _dump_node(child, 0, sub, pretty=False)
                parts.append("".join(sub))
            out.append("(" + " ".join(parts) + ")")
            return
        out.append("(")
        # First element (usually the tag) stays on the opening line.
        head_parts: List[str] = []
        first = node[0]
        head: List[str] = []
        _dump_node(first, 0, head, pretty=False)
        head_parts.append("".join(head))
        out.append(" ".join(head_parts))
        for child in node[1:]:
            out.append("\n")
            out.append("\t" * (indent + 1))
            _dump_node(child, indent + 1, out, pretty=True)
        out.append("\n")
        out.append(pad)
        out.append(")")
        return
    raise SexprError(f"cannot serialize node of type {type(node)!r}")


def dumps(node: Node, pretty: bool = True) -> str:
    """Serialize a node tree back into S-expression text."""
    out: List[str] = []
    _dump_node(node, 0, out, pretty=pretty)
    text = "".join(out)
    if pretty and not text.endswith("\n"):
        text += "\n"
    return text


# --------------------------------------------------------------------------- #
# Convenience helpers for navigating a parsed tree
# --------------------------------------------------------------------------- #


def tag_of(node: Node) -> str | None:
    """Return the leading symbol of a list node, e.g. ``symbol`` in ``(symbol ...)``."""
    if isinstance(node, list) and node and isinstance(node[0], Atom):
        return node[0].value
    return None


def find_all(node: Node, tag: str) -> List[list]:
    """Return all direct child lists of ``node`` whose tag equals ``tag``."""
    if not isinstance(node, list):
        return []
    return [c for c in node if isinstance(c, list) and tag_of(c) == tag]


def find_first(node: Node, tag: str) -> list | None:
    matches = find_all(node, tag)
    return matches[0] if matches else None
