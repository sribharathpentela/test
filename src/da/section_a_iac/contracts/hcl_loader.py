"""HCL loading: the structural pass, the raw-line pass, and type expressions.

Two passes are required (ADR 0005):

* **Structural** - `python-hcl2`. Correct on heredocs, which line-based parsing
  gets wrong: Managed-Redis has a `description` heredoc containing both
  `managed_redis_instances = {` and a `}` at column 0.
* **Raw lines** - hcl2 does surface a `__comments__` key, but it carries only
  comment text with no line numbers and no association to the block it sat in.
  The blueprint test needs to know whether a commented key also appears live, so
  comment *position* matters and the raw pass stays.

Never regex the structure. The raw pass is for comments only.
"""

import io
import os
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

import hcl2

from .models import ObjectAttr, TypeKind, TypeNode

# hcl2 injects this marker into every block body.
_BLOCK_MARKER = "__is_block__"
_SCALARS = {
    "string": TypeKind.STRING,
    "number": TypeKind.NUMBER,
    "bool": TypeKind.BOOL,
    "any": TypeKind.ANY,
}


# --------------------------------------------------------------------------
# Structural pass
# --------------------------------------------------------------------------

def unquote(value: Any) -> Any:
    """hcl2 8.x keeps the source quotes on block labels and string values."""
    if isinstance(value, str) and len(value) >= 2:
        if value[0] == value[-1] and value[0] in ('"', "'"):
            return value[1:-1]
    return value


def unquote_deep(value):
    """`unquote`, applied through nested maps and lists.

    hcl2 keeps the source quotes on every string it returns, including the ones
    inside a map value: `{"network_plugin": '"azure"'}`.
    """
    if isinstance(value, dict):
        return {unquote(k): unquote_deep(v) for k, v in value.items()
                if k != "__is_block__"}
    if isinstance(value, list):
        return [unquote_deep(v) for v in value]
    return unquote(value)


def strip_interpolation(value: str) -> str:
    """`${map(object({...}))}` -> `map(object({...}))`."""
    v = value.strip()
    if v.startswith("${") and v.endswith("}"):
        return v[2:-1].strip()
    return v


def load_hcl(path: str) -> Dict[str, Any]:
    """Parse one HCL file. Returns {} if the file does not exist.

    Raises HclParseError if the file exists but will not parse -- a module we
    cannot read is not a module we may guess about.
    """
    if not os.path.isfile(path):
        return {}
    with io.open(path, encoding="utf-8") as handle:
        return load_hcl_text(handle.read(), label=path)


def load_hcl_text(text: str, label: str = "<text>") -> Dict[str, Any]:
    """Parse HCL held in memory -- e.g. a file fetched over the GitHub API.

    Same contract as `load_hcl`: structure comes from hcl2, never from regex
    (ADR 0005). `label` only names the source in the error message.
    """
    try:
        return hcl2.loads(text)
    except Exception as exc:                      # noqa: BLE001 - re-raised typed
        raise HclParseError("%s: %s" % (label, exc))


class HclParseError(Exception):
    """A file exists but could not be parsed."""


def iter_blocks(data: Dict[str, Any], block_type: str) -> Iterator[Tuple[str, Dict]]:
    """Yield (label, body) for each block of `block_type`.

    hcl2 returns `{"variable": [{'"name"': {...}}, ...]}`; labels keep their
    quotes and bodies carry the `__is_block__` marker.
    """
    for block in data.get(block_type, []) or []:
        if not isinstance(block, dict):
            continue
        for label, body in block.items():
            if not isinstance(body, dict):
                continue
            clean = {k: v for k, v in body.items() if k != _BLOCK_MARKER}
            yield unquote(label), clean


# --------------------------------------------------------------------------
# Raw-line pass  (comments only)
# --------------------------------------------------------------------------

class RawLine(object):
    """One physical line, with its comment split off."""

    __slots__ = ("lineno", "text", "code", "comment", "is_comment_only", "indent")

    def __init__(self, lineno, text, code, comment, is_comment_only, indent):
        self.lineno = lineno
        self.text = text
        self.code = code
        self.comment = comment
        self.is_comment_only = is_comment_only
        self.indent = indent

    def __repr__(self):
        return "RawLine(%d, %r)" % (self.lineno, self.text[:50])


def split_inline_comment(line: str) -> Tuple[str, str]:
    """Split `code # comment`, ignoring a `#` inside a quoted string."""
    in_string = False
    quote = ""
    index = 0
    while index < len(line):
        char = line[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                in_string = False
        elif char in ('"', "'"):
            in_string = True
            quote = char
        elif char == "#":
            return line[:index], line[index + 1:].strip()
        elif char == "/" and line[index + 1:index + 2] == "/":
            return line[:index], line[index + 2:].strip()
        index += 1
    return line, ""


def read_lines(path: str) -> List[RawLine]:
    """The raw pass. Comments survive here and nowhere else."""
    if not os.path.isfile(path):
        return []
    with io.open(path, encoding="utf-8") as handle:
        source = handle.read()

    lines = []
    for number, text in enumerate(source.splitlines(), start=1):
        stripped = text.strip()
        indent = len(text) - len(text.lstrip())
        if stripped.startswith("#") or stripped.startswith("//"):
            body = stripped.lstrip("#/").strip()
            lines.append(RawLine(number, text, "", body, True, indent))
        else:
            code, comment = split_inline_comment(text)
            lines.append(RawLine(number, text, code.rstrip(), comment, False, indent))
    return lines


# Comment annotations the templates use.
_RE_DEFAULT = re.compile(r"default\s*:\s*(.+?)\s*$", re.IGNORECASE)
_RE_EXAMPLE = re.compile(r"e\.g\.\s*(.+?)\s*$", re.IGNORECASE)


def documented_default(comment: str) -> Optional[str]:
    """`# default: null` -> `null`."""
    match = _RE_DEFAULT.search(comment or "")
    return match.group(1).strip() if match else None


def documented_example(comment: str) -> Optional[str]:
    """`# REQUIRED, e.g. "wfi-core"` -> `"wfi-core"`."""
    match = _RE_EXAMPLE.search(comment or "")
    return match.group(1).strip() if match else None


# --------------------------------------------------------------------------
# Type expressions
# --------------------------------------------------------------------------
# hcl2 hands back `type` as source text, not a tree:
#     'string'
#     '${map(object({sku_name = string}))}'
# The generic type-tree walk (01-algorithm.md section 5) and tier T4 both need a
# real tree, so we parse the expression ourselves.

def _split_top_level(text: str, separator: str = ",") -> List[str]:
    """Split on `separator` at nesting depth zero, respecting quotes."""
    parts = []
    depth = 0
    in_string = False
    quote = ""
    current = []
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            current.append(char)
            if char == "\\":
                if index + 1 < len(text):
                    current.append(text[index + 1])
                    index += 2
                    continue
            elif char == quote:
                in_string = False
        elif char in ('"', "'"):
            in_string = True
            quote = char
            current.append(char)
        elif char in "([{":
            depth += 1
            current.append(char)
        elif char in ")]}":
            depth -= 1
            current.append(char)
        elif char == separator and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _call_args(text: str, name: str) -> Optional[str]:
    """If `text` is `name(...)`, return the inside. Otherwise None."""
    prefix = name + "("
    if not text.startswith(prefix) or not text.endswith(")"):
        return None
    return text[len(prefix):-1].strip()


def parse_type_expr(expr: Any) -> Optional[TypeNode]:
    """Parse an HCL type expression into a TypeNode. None if unparseable."""
    if expr is None:
        return None
    if not isinstance(expr, str):
        return None

    text = strip_interpolation(expr).strip()
    if not text:
        return None

    lowered = text.lower()
    if lowered in _SCALARS:
        return TypeNode(kind=_SCALARS[lowered], raw=text)

    for name, kind in (("map", TypeKind.MAP), ("list", TypeKind.LIST),
                       ("set", TypeKind.SET)):
        inner = _call_args(text, name)
        if inner is not None:
            return TypeNode(kind=kind, raw=text, of=parse_type_expr(inner))

    inner = _call_args(text, "object")
    if inner is not None:
        return TypeNode(kind=TypeKind.OBJECT, raw=text,
                        attributes=_parse_object_attrs(inner))

    inner = _call_args(text, "tuple")
    if inner is not None:
        body = inner.strip()
        if body.startswith("[") and body.endswith("]"):
            body = body[1:-1]
        elements = [parse_type_expr(p) for p in _split_top_level(body)]
        return TypeNode(kind=TypeKind.TUPLE, raw=text,
                        elements=[e for e in elements if e is not None])

    # A bare `{ ... }` object body, or anything we do not model.
    if text.startswith("{") and text.endswith("}"):
        return TypeNode(kind=TypeKind.OBJECT, raw=text,
                        attributes=_parse_object_attrs(text))
    return TypeNode(kind=TypeKind.ANY, raw=text)


def _parse_object_attrs(body: str) -> Dict[str, ObjectAttr]:
    """Parse the `{ name = type, ... }` body of an object() type."""
    text = body.strip()
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1]

    attrs = {}
    # Attributes are separated by commas or newlines; both appear in the repos.
    chunks = []
    for piece in _split_top_level(text, ","):
        chunks.extend(p for p in _split_top_level(piece, "\n") if p.strip())

    for chunk in chunks:
        if "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip().strip('"')
        value = value.strip()
        if not name:
            continue

        optional = False
        default = None
        opt_inner = _call_args(value, "optional")
        if opt_inner is not None:
            optional = True
            args = _split_top_level(opt_inner)
            value = args[0].strip() if args else "any"
            if len(args) > 1:
                default = unquote(args[1].strip())

        node = parse_type_expr(value) or TypeNode(kind=TypeKind.ANY, raw=value)
        attrs[name] = ObjectAttr(name=name, node=node, optional=optional,
                                 default=default)
    return attrs


def walk_type(node: Optional[TypeNode], path: str = "") -> Iterator[Tuple[str, TypeNode]]:
    """Yield (dotted_path, node) for every leaf. 01-algorithm.md section 5.

    map(T)     -> adds a key level (rendered as `*`)
    object({}) -> no key level, recurse per attribute
    list(T)    -> recurse into T
    scalar     -> a leaf
    """
    if node is None:
        return
    if node.kind.is_scalar:
        yield path, node
        return
    if node.kind == TypeKind.MAP:
        child = path + ".*" if path else "*"
        for item in walk_type(node.of, child):
            yield item
        return
    if node.kind in (TypeKind.LIST, TypeKind.SET):
        for item in walk_type(node.of, path):
            yield item
        return
    if node.kind == TypeKind.OBJECT:
        if not node.attributes:
            yield path, node
        for name, attr in node.attributes.items():
            child = path + "." + name if path else name
            for item in walk_type(attr.node, child):
                yield item
        return
    if node.kind == TypeKind.TUPLE:
        for index, element in enumerate(node.elements):
            for item in walk_type(element, "%s.%d" % (path, index)):
                yield item
        return
    yield path, node


def required_leaf_paths(node: Optional[TypeNode], path: str = "") -> List[str]:
    """Dotted paths of object attributes NOT wrapped in optional(). Tier T4."""
    found = []
    if node is None:
        return found
    if node.kind == TypeKind.MAP or node.kind in (TypeKind.LIST, TypeKind.SET):
        child = path + ".*" if node.kind == TypeKind.MAP and path else (
            "*" if node.kind == TypeKind.MAP else path)
        found.extend(required_leaf_paths(node.of, child))
        return found
    if node.kind == TypeKind.OBJECT:
        for name, attr in node.attributes.items():
            child = path + "." + name if path else name
            if not attr.optional:
                if attr.node.kind.is_scalar or not attr.node.attributes:
                    found.append(child)
                found.extend(required_leaf_paths(attr.node, child))
        return found
    return found
