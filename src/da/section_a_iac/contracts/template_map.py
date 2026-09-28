"""Two-pass parse of a module's terragrunt.template.hcl.

Pass 1 (hcl2)      - the live `inputs` map and the `terraform { source }` line.
Pass 2 (raw lines) - commented keys, which pass 1 cannot see.

Both are required. A key that is commented out AND live is a *blueprint*: the
comment shows the shape of the live value, not an alternative default. Commented
only means OMIT; commented and live means UNWRAP (01-algorithm.md section 3).
"""

import os
import re
from typing import Dict, List, Optional, Tuple

from . import hcl_loader as H
from .models import EntryKind, EntryState, TemplateEntry, TemplateMap

TEMPLATE_FILENAME = "terragrunt.template.hcl"
EXAMPLES_DIR = "docs/examples"

_RE_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.*)$")
_RE_POINTER = re.compile(
    r"^\$?\{?\s*include\.common\.locals\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}?$")
_RE_SOURCE = re.compile(r'source\s*=\s*"([^"]*)"')


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def build_template_map(template_path: str,
                       module_slug: Optional[str] = None) -> TemplateMap:
    """Parse one terragrunt.template.hcl into a TemplateMap."""
    template_path = os.path.abspath(template_path)
    if module_slug is None:
        module_slug = os.path.basename(os.path.dirname(template_path))

    result = TemplateMap(module_slug=module_slug, template_path=template_path)
    if not os.path.isfile(template_path):
        result.message = "no template at %s" % template_path
        return result

    lines = H.read_lines(template_path)

    # --- pass 1: structure -------------------------------------------------
    live: Dict[str, Tuple[str, int]] = {}
    parsed: Dict[str, object] = {}
    try:
        data = H.load_hcl(template_path)
        inputs = data.get("inputs")
        if isinstance(inputs, list):
            inputs = inputs[0] if inputs else {}
        if isinstance(inputs, dict):
            for key, value in inputs.items():
                if key.startswith("__"):
                    continue
                parsed[key] = H.unquote_deep(value)
                live[key] = (_stringify(value), 0)
    except H.HclParseError as exc:
        result.message = "hcl2 pass failed: %s" % exc

    # Line numbers and the raw source text come from the raw pass; hcl2 has
    # already normalised the value by the time we see it.
    live_lines = _live_assignment_lines(lines)
    for key, (text, lineno) in live_lines.items():
        previous = live.get(key, ("", 0))[0]
        live[key] = (text or previous, lineno)
    for key in list(live):
        if key not in live_lines and live[key][1] == 0:
            # hcl2 saw it but the raw pass did not (multi-line value); keep it.
            pass

    # --- pass 2: comments --------------------------------------------------
    commented = _commented_assignments(lines)

    # --- merge -------------------------------------------------------------
    for key in sorted(set(live) | set(commented)):
        in_live = key in live
        in_comment = key in commented

        if in_live and in_comment:
            state = EntryState.BOTH
        elif in_live:
            state = EntryState.LIVE
        else:
            state = EntryState.COMMENTED

        raw_value, lineno = live.get(key, ("", 0))
        comment_info = commented.get(key)
        if not in_live and comment_info:
            raw_value = comment_info["value"]
            lineno = comment_info["line"]

        entry = TemplateEntry(
            key=key,
            state=state,
            kind=_classify(raw_value, parsed.get(key)),
            raw_value=raw_value,
            line=lineno,
        )
        if key in parsed:
            entry.parsed_value = parsed[key]
            entry.has_parsed_value = True
        entry.pointer_target = _pointer_target(raw_value)

        if lineno and lineno <= len(lines):
            entry.inline_comment = lines[lineno - 1].comment
        if comment_info:
            entry.blueprint_lines = comment_info["lines"]
            entry.blueprint_text = comment_info["text"]
            if not entry.inline_comment:
                entry.inline_comment = comment_info["comment"]
        entry.documented_default = H.documented_default(entry.inline_comment)
        entry.example = H.documented_example(entry.inline_comment)

        result.entries[key] = entry

    headings, standalone = _group_headings(lines)
    for key, heading in headings.items():
        if key in result.entries:
            result.entries[key].group_comment = heading
    result.standalone_comments = standalone

    result.declared_module_path, result.declared_ref_expr = _parse_source(lines)
    if not result.message:
        result.message = "%s: %d keys (%d live, %d blueprint)" % (
            module_slug, len(result.entries),
            len(result.live_keys), len(result.blueprint_keys))
    return result


# --------------------------------------------------------------------------
# Pass 1 helpers
# --------------------------------------------------------------------------

def _stringify(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, dict) and not value:
        return "{}"
    if isinstance(value, list) and not value:
        return "[]"
    return str(value)


def _live_assignment_lines(lines: List[H.RawLine]) -> Dict[str, Tuple[str, int]]:
    """Top-level `key = value` inside the inputs block, from live code."""
    found = {}
    depth = 0
    in_inputs = False
    for line in lines:
        if line.is_comment_only:
            continue
        code = line.code
        stripped = code.strip()

        if not in_inputs:
            if re.match(r"^inputs\s*=\s*\{", stripped):
                in_inputs = True
                depth = 1
            continue

        opens = code.count("{") + code.count("[") + code.count("(")
        closes = code.count("}") + code.count("]") + code.count(")")

        if depth == 1:
            match = _RE_ASSIGNMENT.match(stripped)
            if match:
                found[match.group(1)] = (match.group(2).strip(), line.lineno)

        depth += opens - closes
        if depth <= 0:
            break
    return found


def _group_headings(lines: List[H.RawLine]):
    """Which section comment each top-level key sits under.

    A comment-only line at the top level of `inputs` opens a section; every key
    after it belongs to that section until the next one. A comment that is
    itself a commented-out assignment is not a heading.

    Returns (headings, standalone). A section that never gets a key -- live or
    commented -- is *standalone*: prose the template author wrote to explain
    something, such as why there is no `tags` line.
    """
    headings: Dict[str, str] = {}
    standalone: List[Tuple[int, str]] = []
    current = ""
    current_line = 0
    current_used = True
    depth = 0
    in_inputs = False

    def close_section():
        if current and not current_used:
            standalone.append((current_line, current))

    for line in lines:
        if line.is_comment_only:
            if in_inputs and depth == 1:
                body = _comment_body(line.text)
                stripped = body.strip()
                if not stripped:
                    continue
                if _RE_ASSIGNMENT.match(stripped):
                    current_used = True      # a commented key belongs to it
                elif _is_prose(body):
                    close_section()
                    current, current_line, current_used = (stripped, line.lineno,
                                                           False)
                else:
                    # Indented, or pure punctuation: the body of a commented
                    # blueprint (`#   "cache1" = {`, `#   }`), not a heading.
                    current_used = True
            continue

        code = line.code
        stripped = code.strip()
        if not in_inputs:
            if re.match(r"^inputs\s*=\s*\{", stripped):
                in_inputs, depth = True, 1
            continue

        if depth == 1:
            match = _RE_ASSIGNMENT.match(stripped)
            if match:
                headings[match.group(1)] = current
                current_used = True

        depth += (code.count("{") + code.count("[") + code.count("(")
                  - code.count("}") - code.count("]") - code.count(")"))
        if depth <= 0:
            break
    close_section()
    return headings, standalone


def _parse_source(lines: List[H.RawLine]) -> Tuple[str, str]:
    """`"${...iac_modules_repo}//Databases/Managed-Redis?ref=${...}"`.

    Returns (module_path, ref_expression). The module path keeps its case --
    ADR 0004 -- because comparing it to the discovered path is the whole point.
    """
    for line in lines:
        if line.is_comment_only:
            continue
        match = _RE_SOURCE.search(line.code)
        if not match:
            continue
        source = match.group(1)
        if "//" not in source:
            continue
        _, _, tail = source.partition("//")
        path, _, ref = tail.partition("?ref=")
        return path.strip(), ref.strip()
    return "", ""


# --------------------------------------------------------------------------
# Pass 2 -- commented keys
# --------------------------------------------------------------------------

def _commented_assignments(lines: List[H.RawLine]) -> Dict[str, Dict]:
    """Commented-out top-level keys, with their blueprint bodies.

    Indentation *inside* the comment is what separates a commented top-level key
    from a nested line of a blueprint:

        # managed_redis_instances = {      <- key, comment-indent 0
        #   "cache1" = {                   <- body
        #     sku_name = "Balanced_B0"     <- body, and an assignment too
        #   }
        # }

    Tracking brace depth inside the commented block keeps `sku_name` from being
    read as a template key in its own right.
    """
    found: Dict[str, Dict] = {}
    current: Optional[str] = None
    depth = 0
    live_depth = 0
    in_inputs = False

    for line in lines:
        if not line.is_comment_only:
            # A live line ends any open commented block.
            current = None
            depth = 0
            code = line.code
            if not in_inputs:
                if re.match(r"^inputs\s*=\s*\{", code.strip()):
                    in_inputs = True
                    live_depth = 1
                continue
            live_depth += (code.count("{") + code.count("[") + code.count("(")
                           - code.count("}") - code.count("]") - code.count(")"))
            if live_depth <= 0:
                in_inputs = False
            continue

        # Only comments sitting directly inside `inputs { ... }` can be template
        # keys. The AKS template comments out `nodepool_vnet` three levels deep,
        # inside a live additional_nodepools block -- that is a hint about a pool
        # object, not a template input, and promoting it makes the module look
        # like it sets three variables it never declares.
        if not in_inputs or live_depth != 1:
            continue

        body = _comment_body(line.text)
        stripped = body.strip()
        if not stripped:
            continue

        if current is not None and depth > 0:
            found[current]["lines"].append(line.lineno)
            found[current]["text"] += "\n" + stripped
            depth += _depth_delta(stripped)
            if depth <= 0:
                current = None
                depth = 0
            continue

        indent = len(body) - len(body.lstrip())
        if indent > 0:
            continue                       # nested, but no block open: prose

        code, comment = H.split_inline_comment(stripped)
        match = _RE_ASSIGNMENT.match(code.strip())
        if not match:
            current = None
            depth = 0
            continue

        key, value = match.group(1), match.group(2).strip()
        found[key] = {
            "value": value,
            "line": line.lineno,
            "lines": [line.lineno],
            "text": stripped,
            "comment": comment,
        }
        delta = _depth_delta(value)
        if delta > 0:
            current, depth = key, delta
        else:
            current, depth = None, 0
    return found


def _is_prose(body: str) -> bool:
    """Is this comment a section heading rather than blueprint innards?

    A heading sits flush with the keys it introduces and reads as words. An
    indented comment, or one that is only punctuation, is part of a commented
    block -- the `#   }` lines closing Managed-Redis's blueprint would otherwise
    each be read as opening a new section.
    """
    if body[:1].isspace():
        return False
    stripped = body.strip()
    return any(char.isalpha() for char in stripped)


def _comment_body(text: str) -> str:
    """Strip one leading `#` (or `//`) plus the single space that conventionally
    follows it, preserving any indent beyond that.

    The one space matters: `# key = value` is a top-level commented key, while
    `#   nested = value` is three spaces in and belongs to a blueprint body.
    Counting the conventional space as indent would hide every commented key.
    """
    stripped = text.lstrip()
    if stripped.startswith("//"):
        body = stripped[2:]
    elif stripped.startswith("#"):
        body = stripped[1:]
    else:
        return stripped
    return body[1:] if body.startswith(" ") else body


def _depth_delta(text: str) -> int:
    opens = text.count("{") + text.count("[")
    closes = text.count("}") + text.count("]")
    return opens - closes


# --------------------------------------------------------------------------
# Value classification -- Branch 3's resolution table
# --------------------------------------------------------------------------

def _classify(raw_value: str, parsed=None) -> EntryKind:
    value = (raw_value or "").strip()
    if value in ("{", "[") and parsed is not None:
        # The raw pass saw only the opening brace of a multi-line block.
        if isinstance(parsed, dict):
            return EntryKind.EMPTY_MAP if not parsed else EntryKind.LITERAL
        if isinstance(parsed, list):
            return EntryKind.EMPTY_LIST if not parsed else EntryKind.LITERAL
    if not value:
        return EntryKind.UNKNOWN
    if value in ('"<update-value>"', "<update-value>"):
        return EntryKind.UPDATE_VALUE
    if _pointer_target(value):
        return EntryKind.POINTER
    if value == "null":
        return EntryKind.NULL
    if value == "{}":
        return EntryKind.EMPTY_MAP
    if value == "[]":
        return EntryKind.EMPTY_LIST
    if value.startswith("${") and value.endswith("}"):
        return EntryKind.INTERPOLATION
    return EntryKind.LITERAL


def _pointer_target(raw_value: str) -> str:
    match = _RE_POINTER.match((raw_value or "").strip())
    return match.group(1) if match else ""


# --------------------------------------------------------------------------
# Repo-wide helper
# --------------------------------------------------------------------------

def discover_templates(repo_root: str) -> Dict[str, str]:
    """{slug: path} for every docs/examples/<slug>/terragrunt.template.hcl."""
    examples = os.path.join(repo_root, *EXAMPLES_DIR.split("/"))
    found = {}
    if not os.path.isdir(examples):
        return found
    for slug in sorted(os.listdir(examples)):
        path = os.path.join(examples, slug, TEMPLATE_FILENAME)
        if os.path.isfile(path):
            found[slug] = path
    return found
