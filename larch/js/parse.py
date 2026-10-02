"""A small, dependency-free JavaScript/TypeScript lexer and top-level structure reader.

Larch needs much less than a full parser: where each top-level function (or static
method) starts and ends, its parameters and their type annotations, its JSDoc, the
imports and top-level declarations it refers to, and the operator tokens of its body
(for mutation analysis). The lexer handles strings, template literals (with nested
`${...}`), comments, regular-expression literals and numeric literals, so braces
and operators inside them are never mistaken for code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_PUNCT = sorted(
    """>>>= ... === !== **= <<= >>= >>> &&= ||= ??= => == != <= >= && || ?? ?. ++ -- += -= *= /= %= &= |= ^= ** << >>
    { } ( ) [ ] ; , < > + - * / % & | ^ ! ~ ? : = . @ #""".split(),
    key=len,
    reverse=True,
)
_REGEX_PREV_KEYWORDS = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do", "else",
    "yield", "await",
}
_REGEX_PREV_PUNCT = set("( , = : [ ! & | ? { } ; + - * % < > ~ ^ => == != === !== <= >= && || ?? += -= *= /= %= **".split())
_ID_START = re.compile(r"[A-Za-z_$\u0080-￿]")
_ID = re.compile(r"[A-Za-z0-9_$\u0080-￿]*")
_NUM = re.compile(r"(?:0[xX][0-9a-fA-F_]+|0[oO][0-7_]+|0[bB][01_]+|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d[\d_]*)?)n?")


class JSParseError(ValueError):
    pass


@dataclass
class Tok:
    kind: str  # id | num | str | tmpl | regex | punct | comment
    text: str
    start: int
    end: int
    line: int  # 1-based line of the first character


def tokenize(src: str) -> list[Tok]:
    toks: list[Tok] = []
    i, n, line = 0, len(src), 1
    prev_sig: Tok | None = None

    def regex_allowed() -> bool:
        if prev_sig is None:
            return True
        if prev_sig.kind == "punct":
            return prev_sig.text in _REGEX_PREV_PUNCT
        if prev_sig.kind == "id":
            return prev_sig.text in _REGEX_PREV_KEYWORDS
        return False

    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r\f\v﻿ ":
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j == -1 else j
            toks.append(Tok("comment", src[i:j], i, j, line))
            i = j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j == -1:
                raise JSParseError(f"unterminated comment at line {line}")
            j += 2
            toks.append(Tok("comment", src[i:j], i, j, line))
            line += src.count("\n", i, j)
            i = j
            continue
        if c in "'\"":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                elif src[j] == "\n":
                    raise JSParseError(f"unterminated string at line {line}")
                j += 1
            if j >= n:
                raise JSParseError(f"unterminated string at line {line}")
            j += 1
            tok = Tok("str", src[i:j], i, j, line)
        elif c == "`":
            j = _skip_template(src, i)
            tok = Tok("tmpl", src[i:j], i, j, line)
            line += src.count("\n", i, j)
        elif c == "/" and regex_allowed():
            j = i + 1
            in_class = False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "\n":
                    raise JSParseError(f"unterminated regular expression at line {line}")
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                j += 1
            j += 1
            m = _ID.match(src, j)
            j = m.end() if m else j
            tok = Tok("regex", src[i:j], i, j, line)
        elif c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            m = _NUM.match(src, i)
            j = m.end() if m and m.end() > i else i + 1
            tok = Tok("num", src[i:j], i, j, line)
        elif _ID_START.match(c) or c == "\\":
            m = _ID.match(src, i + 1)
            j = m.end() if m else i + 1
            tok = Tok("id", src[i:j], i, j, line)
        else:
            for p in _PUNCT:
                if src.startswith(p, i):
                    break
            else:
                raise JSParseError(f"unexpected character {c!r} at line {line}")
            j = i + len(p)
            tok = Tok("punct", p, i, j, line)
        toks.append(tok)
        prev_sig = tok
        i = j
    return toks


def _skip_template(src: str, i: int) -> int:
    """Index just past the template literal starting at src[i] == '`'."""
    j, n = i + 1, len(src)
    while j < n:
        ch = src[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "`":
            return j + 1
        if ch == "$" and j + 1 < n and src[j + 1] == "{":
            j = _skip_braced(src, j + 2)
            continue
        j += 1
    raise JSParseError("unterminated template literal")


def _skip_braced(src: str, j: int) -> int:
    """Index just past the `}` closing a `${` whose body starts at src[j]."""
    depth, n = 1, len(src)
    while j < n:
        ch = src[j]
        if ch in "'\"":
            k = j + 1
            while k < n and src[k] != ch:
                k += 2 if src[k] == "\\" else 1
            j = k + 1
            continue
        if ch == "`":
            j = _skip_template(src, j)
            continue
        if src.startswith("//", j):
            k = src.find("\n", j)
            j = n if k == -1 else k
            continue
        if src.startswith("/*", j):
            k = src.find("*/", j + 2)
            j = n if k == -1 else k + 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise JSParseError("unterminated template expression")


# ---------------------------------------------------------------------------
# Top-level structure
# ---------------------------------------------------------------------------

_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = {")", "]", "}"}
_CONTINUES_AFTER = set("=> + - * / % ? : , && || ?? ( [ { . ?. = == != === !== < > <= >= & | ^ ** in instanceof extends implements".split())
_CONTINUES_BEFORE = set(". ?. ? : + - * / % && || ?? ) ] , = == != === !== <= >= & | ^ ** => { as satisfies extends implements".split())
_MODIFIERS = {"export", "default", "declare", "abstract", "async"}
_DECL_KW = {"function", "class", "interface", "type", "enum", "namespace", "module", "const", "let", "var", "import"}


@dataclass
class Param:
    name: str
    annotation: str | None
    optional: bool = False
    has_default: bool = False
    rest: bool = False
    pattern: bool = False  # destructuring


@dataclass
class FunctionDecl:
    name: str  # "f" or "Class.method"
    binding: str  # top-level binding to import
    member: str | None
    start_tok: int  # first token of the declaration (after JSDoc)
    end_tok: int  # last token (inclusive)
    body_start_tok: int  # first token of the body (`{` or the arrow expression)
    params: list[Param]
    returns: str | None
    is_async: bool = False
    is_generator: bool = False
    is_static: bool = True
    jsdoc: str | None = None
    jsdoc_tok: int | None = None
    kind: str = "function"  # function | arrow | function-expression | method


@dataclass
class ClassDecl:
    name: str
    start_tok: int  # first token of the declaration (after JSDoc)
    end_tok: int  # closing brace
    jsdoc: str | None = None
    jsdoc_tok: int | None = None
    members: list[FunctionDecl] = field(default_factory=list)  # methods, getters, constructor


@dataclass
class Statement:
    start: int  # token index
    end: int  # inclusive token index
    names: list[str] = field(default_factory=list)
    is_import: bool = False


class Module:
    def __init__(self, src: str):
        self.src = src
        self.all_toks = tokenize(src)
        self.toks = [t for t in self.all_toks if t.kind != "comment"]
        self._match = self._matching()
        self.statements = self._statements()
        self.classes: list[ClassDecl] = []
        self.functions = self._functions()

    # -- helpers ------------------------------------------------------------------------
    def _matching(self) -> dict[int, int]:
        stack: list[int] = []
        match: dict[int, int] = {}
        for idx, t in enumerate(self.toks):
            if t.kind != "punct":
                continue
            if t.text in _OPEN:
                stack.append(idx)
            elif t.text in _CLOSE:
                if not stack or _OPEN[self.toks[stack[-1]].text] != t.text:
                    raise JSParseError(f"unbalanced {t.text!r} at line {t.line}")
                o = stack.pop()
                match[o] = idx
                match[idx] = o
        if stack:
            raise JSParseError(f"unclosed {self.toks[stack[-1]].text!r} at line {self.toks[stack[-1]].line}")
        return match

    def text(self, a: int, b: int) -> str:
        """Source text from token a to token b (inclusive)."""
        return self.src[self.toks[a].start : self.toks[b].end]

    def _is(self, i: int, text: str) -> bool:
        return 0 <= i < len(self.toks) and self.toks[i].text == text and self.toks[i].kind in ("punct", "id")

    def _skip_group(self, i: int) -> int:
        """Index after the bracket group opening at i (or i+1 for a plain token)."""
        if self.toks[i].kind == "punct" and self.toks[i].text in _OPEN:
            return self._match[i] + 1
        return i + 1

    def _statement_end(self, i: int) -> int:
        """Last token index of the top-level statement starting at i."""
        toks = self.toks
        n = len(toks)
        j = i
        k = i
        while k < n and toks[k].kind == "id" and toks[k].text in _MODIFIERS:
            k += 1
        block_decl = k < n and toks[k].text in ("function", "class", "interface", "enum", "namespace", "module")
        while j < n:
            t = toks[j]
            if t.kind == "punct" and t.text == ";":
                return j
            if t.kind == "punct" and t.text in _OPEN:
                close = self._match[j]
                if t.text == "{" and block_decl and (close + 1 >= n or toks[close + 1].line > toks[close].line or toks[close + 1].text == ";"):
                    return close + 1 if close + 1 < n and toks[close + 1].text == ";" else close
                j = close
            nxt = toks[j + 1] if j + 1 < n else None
            if nxt is None:
                return j
            if nxt.line > toks[j].line and not (toks[j].kind == "punct" and toks[j].text in _CONTINUES_AFTER) \
                    and not (nxt.kind in ("punct", "id") and nxt.text in _CONTINUES_BEFORE) and not block_decl:
                return j
            j += 1
        return n - 1

    def _statements(self) -> list[Statement]:
        out = []
        i, n = 0, len(self.toks)
        while i < n:
            end = self._statement_end(i)
            st = Statement(i, end)
            k = i
            while k <= end and self.toks[k].kind == "id" and self.toks[k].text in _MODIFIERS:
                k += 1
            if k <= end:
                kw = self.toks[k].text
                if kw in ("const", "let", "var") and any(
                        t.kind == "id" and t.text == "require" and self._is(q + 1, "(")
                        for q, t in enumerate(self.toks[k:end + 1], start=k)):
                    st.is_import = True
                if kw in ("function", "class", "interface", "type", "enum", "namespace", "module"):
                    k2 = k + 1
                    if k2 <= end and self.toks[k2].text == "*":
                        k2 += 1
                    if k2 <= end and self.toks[k2].kind == "id":
                        st.names.append(self.toks[k2].text)
                elif kw in ("const", "let", "var"):
                    k2 = k + 1
                    if k2 <= end and self.toks[k2].kind == "id":
                        st.names.append(self.toks[k2].text)
                if kw == "import":
                    st.is_import = True
                    st.names = []
            out.append(st)
            i = end + 1
        return out

    # -- functions ------------------------------------------------------------------------
    def _parse_params(self, open_i: int) -> list[Param]:
        close = self._match[open_i]
        params: list[Param] = []
        j = open_i + 1
        cur: list[int] = []
        while j < close:
            t = self.toks[j]
            if t.kind == "punct" and t.text == "," and not self._angle_open(cur):
                params.append(self._param(cur))
                cur = []
                j += 1
                continue
            if t.kind == "punct" and t.text in _OPEN:
                e = self._match[j]
                cur.extend(range(j, e + 1))
                j = e + 1
            else:
                cur.append(j)
                j += 1
        if cur:
            params.append(self._param(cur))
        return [p for p in params if p.name != "this"]

    def _angle_open(self, idxs: list[int]) -> bool:
        depth = 0
        for k in idxs:
            depth = _angle_step(depth, self.toks[k])
        return depth > 0

    def _param(self, idxs: list[int]) -> Param:
        if not idxs:
            raise JSParseError("empty parameter")
        first = self.toks[idxs[0]]
        # TS accessibility modifiers / decorators
        k = 0
        while k < len(idxs) and self.toks[idxs[k]].text in ("public", "private", "protected", "readonly", "override"):
            k += 1
        first = self.toks[idxs[k]]
        if first.text == "...":
            name = self.toks[idxs[k + 1]].text if k + 1 < len(idxs) else "rest"
            return Param(name, None, rest=True)
        if first.text in ("{", "["):
            return Param("pattern", None, pattern=True)
        name = first.text
        optional = k + 1 < len(idxs) and self.toks[idxs[k + 1]].text == "?"
        ann = None
        default = False
        rest = idxs[k + 1 + (1 if optional else 0):]
        if rest and self.toks[rest[0]].text == ":":
            # annotation until a top-level `=`
            ann_idx = []
            for q in rest[1:]:
                if self.toks[q].text == "=" and not self._angle_open(ann_idx):
                    default = True
                    break
                ann_idx.append(q)
            if ann_idx:
                ann = self.text(ann_idx[0], ann_idx[-1])
        elif rest and self.toks[rest[0]].text == "=":
            default = True
        return Param(name, ann, optional=optional, has_default=default)

    def _return_type(self, close_paren: int, stop: set[str]) -> tuple[str | None, int]:
        """Parse `: Type` after a parameter list; returns (type, index of the next token)."""
        j = close_paren + 1
        if not self._is(j, ":"):
            return None, j
        start = j + 1
        k = start
        if self._is(k, "{"):  # object type literal
            k = self._match[k] + 1
        depth_angle = 0
        while k < len(self.toks):
            t = self.toks[k]
            if t.kind == "punct" and t.text in stop and depth_angle == 0:
                break
            depth_angle = _angle_step(depth_angle, t)
            if t.kind == "punct" and t.text in _OPEN and not (t.text == "{" and "{" in stop and depth_angle == 0):
                k = self._match[k] + 1
                continue
            k += 1
        return (self.text(start, k - 1) if k > start else None), k

    def _jsdoc_before(self, tok_index: int) -> tuple[str | None, int | None]:
        target = self.toks[tok_index]
        pos = self.all_toks.index(target)
        if pos == 0:
            return None, None
        prev = self.all_toks[pos - 1]
        if prev.kind == "comment" and prev.text.startswith("/**") and not self.src[prev.end:target.start].strip():
            return clean_jsdoc(prev.text), pos - 1
        return None, None

    def _functions(self) -> list[FunctionDecl]:
        out: list[FunctionDecl] = []
        for st in self.statements:
            k = st.start
            mods = set()
            while k <= st.end and self.toks[k].kind == "id" and self.toks[k].text in _MODIFIERS:
                mods.add(self.toks[k].text)
                k += 1
            if k > st.end:
                continue
            kw = self.toks[k].text
            try:
                if kw == "function":
                    f = self._function_decl(k, st, mods)
                    if f:
                        out.append(f)
                elif kw in ("const", "let", "var"):
                    f = self._var_function(k, st, mods)
                    if f:
                        out.append(f)
                elif kw == "class":
                    out.extend(self._class_methods(k, st))
            except (JSParseError, KeyError, IndexError):
                continue
        return out

    def _function_decl(self, k: int, st: Statement, mods: set[str]) -> FunctionDecl | None:
        j = k + 1
        gen = False
        if self._is(j, "*"):
            gen = True
            j += 1
        if self.toks[j].kind != "id":
            return None
        name = self.toks[j].text
        j += 1
        if self._is(j, "<"):  # generic parameters
            while not self._is(j, "("):
                j += 1
        if not self._is(j, "("):
            return None
        close = self._match[j]
        params = self._parse_params(j)
        ret, nxt = self._return_type(close, {"{"})
        if not self._is(nxt, "{"):
            return None  # overload signature / declare function
        doc, doc_tok = self._jsdoc_before(st.start)
        return FunctionDecl(name, name, None, st.start, self._match[nxt], nxt, params, ret,
                            is_async="async" in mods, is_generator=gen, jsdoc=doc, jsdoc_tok=doc_tok)

    def _var_function(self, k: int, st: Statement, mods: set[str]) -> FunctionDecl | None:
        j = k + 1
        if self.toks[j].kind != "id":
            return None
        name = self.toks[j].text
        j += 1
        if self._is(j, ":"):  # const f: Fn = ...
            while j <= st.end and not self._is(j, "="):
                j = self._skip_group(j)
        if not self._is(j, "="):
            return None
        j += 1
        is_async = False
        if self._is(j, "async"):
            is_async = True
            j += 1
        kind = "arrow"
        gen = False
        if self._is(j, "function"):
            kind = "function-expression"
            j += 1
            if self._is(j, "*"):
                gen = True
                j += 1
            if self.toks[j].kind == "id":
                j += 1
        if self._is(j, "<"):
            while not self._is(j, "("):
                j += 1
        if self._is(j, "("):
            close = self._match[j]
            params = self._parse_params(j)
            if kind == "arrow":
                ret, nxt = self._return_type(close, {"=>"})
                if not self._is(nxt, "=>"):
                    return None
                body = nxt + 1
            else:
                ret, nxt = self._return_type(close, {"{"})
                if not self._is(nxt, "{"):
                    return None
                body = nxt
        elif self.toks[j].kind == "id" and self._is(j + 1, "=>") and kind == "arrow":
            params = [Param(self.toks[j].text, None)]
            ret, body = None, j + 2
        else:
            return None
        doc, doc_tok = self._jsdoc_before(st.start)
        return FunctionDecl(name, name, None, st.start, st.end, body, params, ret, is_async=is_async,
                            is_generator=gen, jsdoc=doc, jsdoc_tok=doc_tok, kind=kind)

    def _member_end(self, m: int, end: int) -> int:
        toks = self.toks
        j = m
        while j < end:
            t = toks[j]
            if t.kind == "punct" and t.text == ";":
                return j
            if t.kind == "punct" and t.text in _OPEN:
                j = self._match[j]
            if j + 1 >= end:
                return end - 1
            nxt = toks[j + 1]
            if nxt.line > toks[j].line and toks[j].text not in _CONTINUES_AFTER and nxt.text not in _CONTINUES_BEFORE:
                return j
            j += 1
        return end - 1

    def _class_methods(self, k: int, st: Statement) -> list[FunctionDecl]:
        j = k + 1
        if self.toks[j].kind != "id":
            return []
        cls = self.toks[j].text
        while j <= st.end and not self._is(j, "{"):
            j = self._skip_group(j) if self.toks[j].text in ("(", "[") else j + 1
        if not self._is(j, "{"):
            return []
        end = self._match[j]
        out = []
        m = j + 1
        while m < end:
            start = m
            mods = set()
            while self.toks[m].kind == "id" and self.toks[m].text in ("static", "public", "private", "protected", "async", "readonly", "override", "abstract", "declare", "get", "set") \
                    and not self._is(m + 1, "(") and not self._is(m + 1, "=") and not self._is(m + 1, ":"):
                mods.add(self.toks[m].text)
                m += 1
            gen = False
            if self._is(m, "*"):
                gen = True
                m += 1
            if self.toks[m].kind == "id" and (self._is(m + 1, "(") or self._is(m + 1, "<")):
                name = self.toks[m].text
                p = m + 1
                if self._is(p, "<"):
                    while not self._is(p, "("):
                        p += 1
                close = self._match[p]
                params = self._parse_params(p)
                ret, nxt = self._return_type(close, {"{", ";"})
                if self._is(nxt, "{"):
                    doc, doc_tok = self._jsdoc_before(start)
                    out.append(FunctionDecl(
                        f"{cls}.{name}", cls, name, start, self._match[nxt], nxt, params, ret,
                        is_async="async" in mods, is_generator=gen, is_static="static" in mods,
                        jsdoc=doc, jsdoc_tok=doc_tok,
                        kind="getter" if "get" in mods else "setter" if "set" in mods else "method",
                    ))
                    m = self._match[nxt] + 1
                    continue
                m = nxt + 1
                continue
            # property, accessor, constructor, ...: skip to the end of the member
            m = self._member_end(start, end) + 1
        doc, doc_tok = self._jsdoc_before(st.start)
        self.classes.append(ClassDecl(cls, st.start, end, doc, doc_tok, list(out)))
        # Only static methods are standalone functions; instance members belong to the class.
        return out


def _angle_step(depth: int, t: Tok) -> int:
    """Track TypeScript generic brackets (`>>` closes two)."""
    if t.kind != "punct":
        return depth
    if t.text == "<":
        return depth + 1
    if t.text in (">", ">>", ">>>"):
        return max(0, depth - len(t.text))
    return depth


def clean_jsdoc(text: str) -> str:
    body = text[3:-2] if text.endswith("*/") else text[3:]
    lines = []
    for ln in body.splitlines():
        s = ln.strip()
        if s.startswith("*"):
            s = s[1:]
            if s.startswith(" "):
                s = s[1:]
        lines.append(s.rstrip())
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


_JSDOC_PARAM = re.compile(r"@param\s+\{([^}]*)\}\s+\[?([A-Za-z_$][\w$]*)")
_JSDOC_RETURNS = re.compile(r"@returns?\s+\{([^}]*)\}")


def jsdoc_types(doc: str | None) -> tuple[dict[str, str], str | None]:
    if not doc:
        return {}, None
    params = {m.group(2): m.group(1).strip() for m in _JSDOC_PARAM.finditer(doc)}
    r = _JSDOC_RETURNS.search(doc)
    return params, (r.group(1).strip() if r else None)
