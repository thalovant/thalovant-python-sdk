"""Reading the Thalovant config file without making PyYAML a dependency.

PyYAML is used whenever it is installed, so behaviour is exactly what it was.
Without it, a small reader handles what a config file holds: block mappings
and sequences, flow lists and maps, comments, plain and quoted scalars, and
YAML 1.1's null, boolean, integer and float spellings, resolved the way
PyYAML's ``safe_load`` resolves them. Anything beyond that -- anchors, tags,
block scalars, several documents -- is refused with a message that names the
extra to install rather than guessed at.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

__all__ = ["YAMLError", "safe_load"]


class YAMLError(ValueError):
    """The text is not YAML this reader understands."""


_NULL = re.compile(r"^(?:~|null|Null|NULL)$")
_BOOL_TRUE = re.compile(r"^(?:yes|Yes|YES|true|True|TRUE|on|On|ON)$")
_BOOL_FALSE = re.compile(r"^(?:no|No|NO|false|False|FALSE|off|Off|OFF)$")
_INT_DEC = re.compile(r"^[-+]?(?:0|[1-9][0-9_]*)$")
_INT_OCT = re.compile(r"^[-+]?0[0-7_]+$")
_INT_HEX = re.compile(r"^[-+]?0x[0-9a-fA-F_]+$")
_INT_BIN = re.compile(r"^[-+]?0b[0-1_]+$")
_FLOAT = re.compile(
    r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?"
    r"|\.[0-9_]+(?:[eE][-+][0-9]+)?"
    r"|[-+]?\.(?:inf|Inf|INF)"
    r"|\.(?:nan|NaN|NAN))$"
)
_DATE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$")
_TIMESTAMP = re.compile(
    r"^([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})(?:[Tt]|[ \t]+)([0-9]{1,2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]*))?(?:[ \t]*(Z|[-+][0-9]{1,2}(?::?[0-9]{2})?))?$"
)
_UNSUPPORTED = ("&", "*", "!", "|", ">", "%", "@", "`")


def _timestamp(token: str) -> Any:
    date = _DATE.match(token)
    if date:
        return datetime.date(*(int(part) for part in date.groups()))
    match = _TIMESTAMP.match(token)
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    micro = int((fraction or "0")[:6].ljust(6, "0"))
    tzinfo = None
    if zone and zone != "Z":
        sign = -1 if zone[0] == "-" else 1
        digits = zone[1:].replace(":", "")
        hours, minutes = int(digits[:2] if len(digits) > 2 else digits), int(digits[2:] or 0)
        tzinfo = datetime.timezone(sign * datetime.timedelta(hours=hours, minutes=minutes))
    elif zone == "Z":
        tzinfo = datetime.timezone.utc
    return datetime.datetime(
        int(year), int(month), int(day), int(hour), int(minute), int(second), micro, tzinfo
    )


def safe_load(text: str) -> Any:
    """Parse ``text``; with PyYAML installed, exactly ``yaml.safe_load``."""
    try:
        import yaml
    except ImportError:
        return _Reader(text).document()
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise YAMLError(str(exc)) from None


def _resolve(token: str) -> Any:
    if token == "" or _NULL.match(token):
        return None
    if _BOOL_TRUE.match(token):
        return True
    if _BOOL_FALSE.match(token):
        return False
    cleaned = token.replace("_", "")
    if _INT_DEC.match(token):
        return int(cleaned, 10)
    if _INT_HEX.match(token):
        return int(cleaned, 16)
    if _INT_BIN.match(token):
        return int(cleaned.replace("0b", ""), 2)
    if _INT_OCT.match(token):
        sign = -1 if cleaned.startswith("-") else 1
        return sign * int(cleaned.lstrip("+-"), 8)
    stamp = _timestamp(token)
    if stamp is not None:
        return stamp
    if _FLOAT.match(token):
        lowered = cleaned.lower()
        if lowered.endswith("inf"):
            return float("-inf") if lowered.startswith("-") else float("inf")
        if lowered.endswith("nan"):
            return float("nan")
        return float(cleaned)
    if ": " in token or token.endswith(":"):
        raise YAMLError(f"Mapping values are not allowed here: {token!r}")
    if token[0] in _UNSUPPORTED:
        raise YAMLError(
            f"Unsupported YAML syntax {token[0]!r}; install thalovant[yaml] to read this file."
        )
    return token


def _unquote(token: str) -> str:
    quote = token[0]
    body = token[1:-1]
    if quote == "'":
        return body.replace("''", "'")
    out: list[str] = []
    index = 0
    escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "0": "\0",
               "a": "\a", "b": "\b", "e": "\x1b", "f": "\f", "v": "\v", " ": " "}
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        index += 1
        if index >= len(body):
            raise YAMLError("A double-quoted string ends in a lone backslash.")
        code = body[index]
        if code in escapes:
            out.append(escapes[code])
            index += 1
        elif code in "xuU":
            width = {"x": 2, "u": 4, "U": 8}[code]
            digits = body[index + 1 : index + 1 + width]
            if len(digits) != width:
                raise YAMLError("A truncated escape in a double-quoted string.")
            out.append(chr(int(digits, 16)))
            index += 1 + width
        else:
            raise YAMLError(f"Unknown escape \\{code} in a double-quoted string.")
    return "".join(out)


def _strip_comment(line: str) -> str:
    quote: str | None = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index].rstrip()
    return line.rstrip()


def _split_key(text: str) -> tuple[str, str] | None:
    """``key: rest`` at the top level of a line, quotes respected."""
    quote: str | None = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == ":" and (index + 1 == len(text) or text[index + 1] in " \t"):
            return text[:index].strip(), text[index + 1 :].strip()
        elif char in "[{" and index == 0:
            return None
    return None


class _Reader:
    def __init__(self, text: str) -> None:
        if text.startswith("﻿"):
            text = text[1:]
        self.lines: list[tuple[int, str]] = []
        for raw in text.splitlines():
            if "\t" in raw[: len(raw) - len(raw.lstrip())]:
                raise YAMLError("Tabs cannot indent YAML.")
            stripped = _strip_comment(raw)
            if not stripped.strip():
                continue
            if stripped.strip() in ("---", "..."):
                if self.lines:
                    raise YAMLError(
                        "Several YAML documents; install thalovant[yaml] to read this file."
                    )
                continue
            self.lines.append((len(stripped) - len(stripped.lstrip(" ")), stripped.strip()))
        self.index = 0

    def document(self) -> Any:
        if not self.lines:
            return None
        value = self.block(self.lines[0][0])
        if self.index != len(self.lines):
            raise YAMLError(f"Unexpected YAML at line: {self.lines[self.index][1]!r}")
        return value

    def block(self, indent: int) -> Any:
        _, text = self.lines[self.index]
        if text == "-" or text.startswith("- "):
            return self.sequence(indent)
        if _split_key(text) is not None:
            return self.mapping(indent)
        self.index += 1
        return self.scalar(text)

    def sequence(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while self.index < len(self.lines):
            level, text = self.lines[self.index]
            if level < indent:
                break
            if level != indent or not (text == "-" or text.startswith("- ")):
                break
            rest = text[1:].strip()
            if rest == "-" or rest.startswith("- "):
                # "- - item" opens a sequence nested in this one.
                inner = indent + (len(text) - len(rest))
                self.lines[self.index] = (inner, rest)
                items.append(self.sequence(inner))
                continue
            if not rest:
                self.index += 1
                if self.index < len(self.lines) and self.lines[self.index][0] > indent:
                    items.append(self.block(self.lines[self.index][0]))
                else:
                    items.append(None)
                continue
            if _split_key(rest) is not None:
                # "- key: value" opens a mapping indented past the dash.
                inner = indent + (len(text) - len(rest))
                self.lines[self.index] = (inner, rest)
                items.append(self.mapping(inner))
                continue
            self.index += 1
            items.append(self.scalar(rest))
        return items

    def mapping(self, indent: int) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        while self.index < len(self.lines):
            level, text = self.lines[self.index]
            if level < indent:
                break
            if level != indent:
                raise YAMLError(f"Unexpected indentation at line: {text!r}")
            split = _split_key(text)
            if split is None:
                break
            key_text, rest = split
            key = self.scalar(key_text)
            if key in result:
                raise YAMLError(f"Duplicate key {key!r}.")
            self.index += 1
            if rest:
                result[key] = self.scalar(rest)
                continue
            if self.index < len(self.lines):
                child_level, child = self.lines[self.index]
                if child_level > indent or (
                    child_level == indent and (child == "-" or child.startswith("- "))
                ):
                    result[key] = self.block(child_level)
                    continue
            result[key] = None
        return result

    def scalar(self, text: str) -> Any:
        if not text:
            return None
        if text[0] in "[{":
            value, end = self.flow(text, 0)
            if text[end:].strip():
                raise YAMLError(f"Unexpected text after a flow collection: {text!r}")
            return value
        if text[0] in "'\"":
            if len(text) < 2 or text[-1] != text[0]:
                raise YAMLError(f"Unterminated quoted string: {text!r}")
            return _unquote(text)
        return _resolve(text)

    def flow(self, text: str, start: int) -> tuple[Any, int]:
        opener = text[start]
        closer = "]" if opener == "[" else "}"
        items: list[Any] = []
        mapping: dict[Any, Any] = {}
        index = start + 1
        while True:
            while index < len(text) and text[index] in " \t":
                index += 1
            if index >= len(text):
                raise YAMLError(f"Unterminated flow collection: {text!r}")
            if text[index] == closer:
                return (items if opener == "[" else mapping), index + 1
            value, index = self.flow_item(text, index, closer)
            if opener == "{":
                if not isinstance(value, tuple):
                    raise YAMLError(f"A flow mapping entry needs a key: {text!r}")
                mapping[value[0]] = value[1]
            else:
                items.append(value)
            while index < len(text) and text[index] in " \t":
                index += 1
            if index < len(text) and text[index] == ",":
                index += 1

    def flow_item(self, text: str, index: int, closer: str) -> tuple[Any, int]:
        if text[index] in "[{":
            return self.flow(text, index)
        end = index
        quote: str | None = None
        depth = 0
        while end < len(text):
            char = text[end]
            if quote:
                if char == quote:
                    quote = None
            elif char in "'\"":
                quote = char
            elif char in "[{":
                depth += 1
            elif char in "]}" and depth:
                depth -= 1
            elif char in f",{closer}" and not depth:
                break
            end += 1
        token = text[index:end].strip()
        if closer == "}":
            split = _split_key(token) or (
                (token[:-1].strip(), "") if token.endswith(":") else None
            )
            if split is None:
                raise YAMLError(f"A flow mapping entry needs a key: {text!r}")
            return (self.scalar(split[0]), self.scalar(split[1])), end
        return self.scalar(token), end
