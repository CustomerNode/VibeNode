"""No ``.replace()`` in the front end takes a computed string as its replacement.

``String.prototype.replace(pattern, str)`` reads ``$&``, ``$'``, ``$``` and
``$$`` inside ``str`` as special patterns. When ``str`` is computed from text
that came from an agent, a file or the user, a ``$'`` in it pastes the rest of
the subject string into the output. In ``mdParse`` that turned a copy card's
label into an attribute breakout that ran script with no click (2026-10-15),
and it has always mangled code blocks containing ``$'`` (bash ``$'\\n'``).

A function replacement (``s.replace(x, () => value)``) is taken literally, so
the rule is: the replacement is a string LITERAL (where ``$1`` is intended) or a
function. This scanner tokenises every script under ``static/js`` (vendor code
excluded) well enough to split each call's top-level arguments, and fails on
any other replacement.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "static" / "js"

_CALL = re.compile(r"\.replace(?:All)?\(")


def _skip_string(src: str, i: int) -> int:
    """i points at a quote; return the index just past the closing quote."""
    quote = src[i]
    i += 1
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if quote == "`" and src.startswith("${", i):
            i = _skip_balanced(src, i + 2, "}")
            continue
        if c == quote:
            return i + 1
        i += 1
    return i


def _skip_regex(src: str, i: int) -> int:
    """i points at the opening / of a regex literal; return the index past its flags."""
    i += 1
    in_class = False
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            i += 1
            while i < len(src) and src[i].isalpha():
                i += 1
            return i
        elif c == "\n":
            return i
        i += 1
    return i


def _skip_balanced(src: str, i: int, close: str) -> int:
    """Scan from i to just past the matching ``close`` at depth zero."""
    depth = 0
    expect_value = True
    while i < len(src):
        c = src[i]
        if c in "'\"`":
            i = _skip_string(src, i)
            expect_value = False
            continue
        if src.startswith("//", i) and expect_value is not None:
            nl = src.find("\n", i)
            i = len(src) if nl < 0 else nl
            continue
        if src.startswith("/*", i):
            end = src.find("*/", i + 2)
            i = len(src) if end < 0 else end + 2
            continue
        if c == "/" and expect_value:
            i = _skip_regex(src, i)
            expect_value = False
            continue
        if c in "([{":
            depth += 1
            expect_value = True
        elif c in ")]}":
            if depth == 0 and c == close:
                return i + 1
            depth -= 1
            expect_value = False
        elif c in ",=:;!&|?+-*%<>~^":
            expect_value = True
        elif not c.isspace():
            expect_value = False
        i += 1
    return i


def _args(src: str, start: int) -> list[str]:
    """Top-level arguments of the call whose ``(`` is at src[start - 1]."""
    end = _skip_balanced(src, start, ")")
    body = src[start:end - 1]
    parts, depth, cur, i = [], 0, [], 0
    expect_value = True
    while i < len(body):
        c = body[i]
        if c in "'\"`":
            j = _skip_string(body, i)
            cur.append(body[i:j]); i = j; expect_value = False
            continue
        if c == "/" and expect_value and not body.startswith("//", i):
            j = _skip_regex(body, i)
            cur.append(body[i:j]); i = j; expect_value = False
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur).strip()); cur = []; i += 1; expect_value = True
            continue
        expect_value = c in "(,=:;!&|?+-*%<>~^[{" or (c.isspace() and expect_value)
        cur.append(c)
        i += 1
    parts.append("".join(cur).strip())
    return parts


def _safe_replacement(arg: str) -> bool:
    a = arg.strip()
    if re.fullmatch(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", a, re.S):
        return True                       # a literal: any $1 in it is intended
    if a.startswith("`") and a.endswith("`") and "${" not in a:
        return True
    if a.startswith(("function", "async function", "async (")) or re.match(r"^\(?[\w\s,]*\)?\s*=>", a):
        return True                       # a function: its result is taken literally
    if re.fullmatch(r"[A-Za-z_$][\w$]*", a) and a.endswith(("Fn", "Fmt", "_fn", "Replacer")):
        return True                       # a named function reference
    return False


def _violations(src: str, name: str) -> list[str]:
    out = []
    for m in _CALL.finditer(src):
        args = _args(src, m.end())
        if len(args) < 2:
            continue
        if not _safe_replacement(args[1]):
            line = src.count("\n", 0, m.start()) + 1
            out.append(f"{name}:{line}: .replace(..., {args[1][:60]})")
    return out


def _scripts():
    return sorted(p for p in JS_DIR.rglob("*.js") if "vendor" not in p.parts)


def test_no_computed_string_replacement_in_the_front_end():
    found = []
    for path in _scripts():
        found += _violations(path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix())
    assert not found, ("A computed string as .replace()'s replacement treats $' $& $` $$ as "
                       "patterns; pass a function instead (s.replace(x, () => value)):\n  "
                       + "\n  ".join(found))


def test_the_scan_reaches_the_real_front_end():
    scripts = _scripts()
    assert len(scripts) >= 30, scripts
    calls = sum(len(_CALL.findall(p.read_text(encoding="utf-8"))) for p in scripts)
    assert calls >= 100, calls


def test_the_scanner_still_recognises_the_bug_shape():
    bad = [
        "html = html.replace('\\x00CODE' + idx + '\\x00', block);",
        "h = location.hash.replace(oldId, newId);",
        "s = s.replace(/x/g, `${label}`);",
        "s = s.replace(\n  /a,b/g,\n  obj.value\n);",
    ]
    for snippet in bad:
        assert _violations(snippet, "x.js"), snippet
    good = [
        "html = html.replace('\\x00CODE' + idx + '\\x00', () => block);",
        "s = s.replace(/&/g, '&amp;').replace(/(a)/g, '$1');",
        "s = s.replace(/x/g, function (m) { return m + ','; });",
        "s = s.replace(/[,(]/g, (m, a) => a);",
        "s = s.replace(/\\/\\//g, \"/\");",
        "s = s.replace(re, `plain`);",
    ]
    for snippet in good:
        assert not _violations(snippet, "x.js"), snippet
