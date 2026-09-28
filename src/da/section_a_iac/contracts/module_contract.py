"""Build a ModuleContract from a module's .tf files.

Never reads README.md -- Managed-Redis's contradicts its own variables.tf on six
keys (CLAUDE.md rule 8). The .tf files are the contract.

Structure comes from hcl2. Raw text is used only for two things that are not
structure: scanning `var.X` references, and reading comments.
"""

import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from . import hcl_loader as H
from .models import (ModuleContract, Precondition, RequiredTier, TypeKind,
                     Validation, VariableSpec)

# Files that declare or use variables. README.md is deliberately absent.
SPEC_FILE = "variables.tf"
CONTRACT_FILES = ("variables.tf", "main.tf", "data.tf", "local.tf", "locals.tf")

_RE_VAR_REF = re.compile(r"\bvar\.([A-Za-z_][A-Za-z0-9_-]*)")
_RE_FOR_EACH = re.compile(r"for_each\s*=\s*var\.([A-Za-z_][A-Za-z0-9_-]*)")
_RE_NAMING_BLOCK = re.compile(
    r'module\s+"naming"\s*\{(.*?)\n\}', re.DOTALL)


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def build_contract(module_dir: str, module_path: Optional[str] = None) -> ModuleContract:
    """Build the contract for one module directory.

    `module_path` is the case-sensitive repo-relative path, e.g.
    "Databases/Managed-Redis". Derived from the directory name when omitted.
    Never normalise its case -- ADR 0004.
    """
    module_dir = os.path.abspath(module_dir)
    if module_path is None:
        parent, name = os.path.split(module_dir)
        module_path = "%s/%s" % (os.path.basename(parent), name)
    category, _, name = module_path.rpartition("/")

    contract = ModuleContract(module_path=module_path, category=category, name=name)

    spec_path = os.path.join(module_dir, SPEC_FILE)
    if not os.path.isfile(spec_path):
        contract.message = "no %s in %s" % (SPEC_FILE, module_path)
        return contract

    contract.variables = _parse_variables(spec_path)
    contract.files_parsed.append(SPEC_FILE)

    # Every .tf file, so references in private-endpoint.tf / linkedservers.tf
    # are seen. Reference scanning is not structure parsing.
    tf_files = sorted(f for f in os.listdir(module_dir) if f.endswith(".tf"))
    referenced, source_text = _scan_references(module_dir, tf_files)
    for filename in tf_files:
        if filename not in contract.files_parsed:
            contract.files_parsed.append(filename)

    declared = set(contract.variables)
    contract.referenced_undeclared = sorted(referenced - declared)
    contract.orphan_variables = sorted(declared - referenced)

    contract.primary_payload_map = _find_primary_payload_map(module_dir)
    contract.naming_inputs = _find_naming_inputs(module_dir)
    contract.preconditions = _parse_preconditions(module_dir)

    _apply_tiers(contract, source_text)

    contract.message = "%s: %d variables, %d required" % (
        module_path, len(contract.variables), len(contract.required))
    return contract


# --------------------------------------------------------------------------
# variables.tf
# --------------------------------------------------------------------------

def _parse_variables(spec_path: str) -> Dict[str, VariableSpec]:
    data = H.load_hcl(spec_path)
    specs = {}
    for name, body in H.iter_blocks(data, "variable"):
        type_expr = body.get("type")
        type_expr = type_expr if isinstance(type_expr, str) else ""
        spec = VariableSpec(
            name=name,
            type_expr=type_expr,
            type_tree=H.parse_type_expr(type_expr),
            has_default="default" in body,
            default=H.unquote(body.get("default")),
            description=str(H.unquote(body.get("description", "")) or ""),
            sensitive=bool(body.get("sensitive", False)),
            validations=_parse_validations(body, name),
            declared_in=SPEC_FILE,
        )
        specs[name] = spec
    return specs


_RE_CONTAINS = re.compile(r"contains\s*\(\s*\[(?P<items>.*?)\]\s*,", re.DOTALL)


def _parse_validations(body: Dict[str, Any],
                       variable: str = "") -> List[Validation]:
    """Pull `validation { condition, error_message }` blocks off a variable."""
    node = body.get("validation")
    if node is None:
        return []
    blocks = node if isinstance(node, list) else [node]

    found = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        condition = str(H.unquote(block.get("condition", "")) or "")
        found.append(Validation(
            condition=condition,
            error_message=str(H.unquote(block.get("error_message", "")) or ""),
            allowed_values=_enum_from_contains(condition, variable),
            attribute_enums=_attribute_enums(condition, variable),
        ))
    return found


# The attribute is often reached through try(), e.g.
#   contains(["11", ..., "16"], try(server.version, "16"))
_RE_ATTR_ENUM = re.compile(
    r"contains\s*\(\s*\[(?P<items>[^\]]*)\]\s*,\s*(?:try\s*\(\s*)?"
    r"(?P<bind>[A-Za-z_][A-Za-z0-9_]*)\.(?P<attr>[A-Za-z_][A-Za-z0-9_]*)")


def _attribute_enums(condition: str, variable: str) -> Dict[str, List[Any]]:
    """Enums that constrain an *attribute of each entry* in a collection.

        alltrue([for _, server in var.postgresql_flexible_servers :
                 contains(["11", ..., "16"], server.version)])

    yields {"version": ["11", ..., "16"]}. These belong to the leaf, not to the
    map, and applying them at the map level rejects every other leaf.
    """
    text = H.strip_interpolation(condition)
    if ("var.%s" % variable) not in text:
        return {}
    found: Dict[str, List[Any]] = {}
    for match in _RE_ATTR_ENUM.finditer(text):
        if match.group("bind") == "var":
            continue            # `var.x` is the variable itself, not a binding
        values = _literal_list(match.group("items"))
        if values:
            found[match.group("attr")] = values
    return found


def _literal_list(items: str) -> List[Any]:
    values = []
    for raw in items.split(","):
        item = raw.strip()
        if not item:
            continue
        if item.startswith('"') and item.endswith('"'):
            values.append(item[1:-1])
        elif item in ("true", "false"):
            values.append(item == "true")
        else:
            try:
                values.append(int(item))
            except ValueError:
                return []
    return values


def _enum_from_contains(condition: str, variable: str = "") -> List[Any]:
    """`contains(["a", "b"], var.x)` -> ["a", "b"], for THIS variable only.

    The second argument must be `var.<variable>` itself. PostgreSQL's map is
    validated with

        alltrue([for _, server in var.postgresql_flexible_servers :
                 contains(["11", ..., "16"], server.version)])

    which constrains the `version` **attribute of each entry**, not the map. An
    earlier version of this function matched the inner `contains(...)` and
    claimed ["11".."16"] as the map's own enum, so every value written to any
    leaf of the map -- sku_name, storage_mb, administrator_login -- was rejected
    for not being a Postgres version. Attribute-level enums are collected
    separately by `_attribute_enums`.
    """
    text = H.strip_interpolation(condition)
    match = _RE_CONTAINS.search(text)
    if not match:
        return []
    tail = text[match.end():].strip()
    target = tail.split(")")[0].strip()
    if variable and target != "var.%s" % variable:
        return []
    return _literal_list(match.group("items"))


# --------------------------------------------------------------------------
# Reference scan
# --------------------------------------------------------------------------

def _scan_references(module_dir: str, tf_files: List[str]) -> Tuple[Set[str], str]:
    """Every `var.X` across the module's .tf files, excluding variables.tf."""
    found = set()
    chunks = []
    for filename in tf_files:
        if filename == SPEC_FILE:
            continue
        lines = H.read_lines(os.path.join(module_dir, filename))
        # Only live code -- a var referenced solely in a comment is not a use.
        text = "\n".join(line.code for line in lines if not line.is_comment_only)
        chunks.append(text)
        found.update(_RE_VAR_REF.findall(text))
    return found, "\n".join(chunks)


# --------------------------------------------------------------------------
# The primary payload map
# --------------------------------------------------------------------------

def _find_primary_payload_map(module_dir: str) -> Optional[str]:
    """The map that `module "naming" { for_each = ... }` iterates.

    That identifies the collection whose entries become named Azure resources,
    distinguishing it from secondary collections like private_endpoints.
    Flat modules have none.
    """
    main_path = os.path.join(module_dir, "main.tf")
    lines = H.read_lines(main_path)
    if not lines:
        return None
    text = "\n".join(line.code for line in lines if not line.is_comment_only)

    match = _RE_NAMING_BLOCK.search(text)
    if not match:
        return None
    inner = _RE_FOR_EACH.search(match.group(1))
    return inner.group(1) if inner else None


def _find_naming_inputs(module_dir: str) -> List[str]:
    """Variables passed into `module "naming"` -- the ones that decide the name.

    The module states this itself, so nothing is hardcoded. A value that lands
    in one of these changes what the Azure resource is called, which is worth a
    human glance even when it came from the ticket.
    """
    lines = H.read_lines(os.path.join(module_dir, "main.tf"))
    if not lines:
        return []
    text = "\n".join(l.code for l in lines if not l.is_comment_only)
    match = _RE_NAMING_BLOCK.search(text)
    if not match:
        return []
    return sorted(set(_RE_VAR_REF.findall(match.group(1))))


# --------------------------------------------------------------------------
# Preconditions  (tier T2)
# --------------------------------------------------------------------------

def _parse_preconditions(module_dir: str) -> List[Precondition]:
    """Pull `precondition { condition = ... }` out of every .tf file.

    hcl2 nests these deep inside resource/lifecycle bodies and the nesting shape
    varies, so walk the parsed tree generically rather than guessing a path.
    """
    found = []
    for filename in sorted(f for f in os.listdir(module_dir) if f.endswith(".tf")):
        try:
            data = H.load_hcl(os.path.join(module_dir, filename))
        except H.HclParseError:
            continue
        for condition, message in _walk_for_preconditions(data):
            found.append(Precondition(
                variable="",                 # attributed in _apply_tiers
                condition=condition,
                error_message=message,
                found_in=filename,
            ))
    return found


def _walk_for_preconditions(node: Any):
    """Yield (condition, error_message) from anywhere in the parsed tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "precondition":
                blocks = value if isinstance(value, list) else [value]
                for block in blocks:
                    if isinstance(block, dict):
                        yield (
                            str(H.unquote(block.get("condition", "")) or ""),
                            str(H.unquote(block.get("error_message", "")) or ""),
                        )
            else:
                for item in _walk_for_preconditions(value):
                    yield item
    elif isinstance(node, list):
        for value in node:
            for item in _walk_for_preconditions(value):
                yield item


# --------------------------------------------------------------------------
# Tier assignment
# --------------------------------------------------------------------------

def _apply_tiers(contract: ModuleContract, source_text: str) -> None:
    for_each_vars = set(_RE_FOR_EACH.findall(source_text))

    for name, spec in contract.variables.items():
        tiers = []

        # T1 -- declared with no default.
        if not spec.has_default:
            tiers.append(RequiredTier.T1)

        # T3 -- the PRIMARY payload map defaulting to {}, so the module creates
        # nothing at all unless the caller fills it.
        #
        # Only the primary map counts. Section 4 defines it as the collection
        # `module "naming"` iterates, "distinguishing it from secondary
        # collections like private_endpoints" -- and that distinction is the
        # whole point: a load balancer with no `nat_rules` is a perfectly good
        # load balancer, while a Redis with no `managed_redis_instances`
        # provisions nothing. Treating every empty map as required made DA
        # demand 5 collections from a load-balancer ticket that needed none.
        if (spec.has_default
                and isinstance(spec.default, dict)
                and not spec.default
                and name in for_each_vars
                and name == contract.primary_payload_map):
            tiers.append(RequiredTier.T3)

        # T4 -- nested object attributes not wrapped in optional().
        #
        # T4 refines a variable that is going to be filled; it does not make an
        # optional variable mandatory. `redis_modules` defaults to [] and its
        # element type has required attributes, but a caller who supplies no
        # elements owes nothing. So record the paths always, and only count T4
        # as a tier when the variable is already required by T1 or T3.
        if spec.type_tree is not None:
            paths = H.required_leaf_paths(spec.type_tree)
            nested = [p for p in paths if p and p != "*"]
            if nested and spec.type_tree.kind in (
                    TypeKind.MAP, TypeKind.OBJECT, TypeKind.LIST, TypeKind.SET):
                spec.required_paths = nested
                if tiers:
                    tiers.append(RequiredTier.T4)

        spec.tiers = tiers

    # T2 -- has a default, but a precondition rejects that default.
    for precondition in contract.preconditions:
        for name in _vars_rejected_by(precondition.condition, contract.variables):
            precondition.variable = name
            spec = contract.variables[name]
            if RequiredTier.T2 not in spec.tiers:
                spec.tiers.append(RequiredTier.T2)


def _vars_rejected_by(condition: str,
                      variables: Dict[str, VariableSpec]) -> List[str]:
    """Which variables' declared defaults make this precondition fail?

    The definition of T2 is "has a default, but a precondition rejects it", so
    the honest test is to substitute the defaults and evaluate. A condition that
    passes on defaults constrains combinations rather than demanding a value --
    Managed-Redis has two of those, and attributing them would overstate the
    required set.

    Returns [] when the expression uses anything the evaluator does not model.
    """
    referenced = [v for v in set(_RE_VAR_REF.findall(condition)) if v in variables]
    if not referenced:
        return []

    # A variable with no default has no value to substitute. Treat it as unknown
    # rather than abandoning the whole condition: Managed-Redis's naming
    # precondition mentions var.app (no default) alongside var.resource_prefix
    # (default ""), and bailing out would leave resource_prefix unclassified.
    env = {}
    for name in referenced:
        spec = variables[name]
        env[name] = spec.default if spec.has_default else _UNKNOWN

    verdict = _evaluate(H.strip_interpolation(condition), env)
    if verdict is not False:
        return []              # passes on defaults, or not evaluable

    # The condition fails on defaults. Blame the variables it compares against
    # their own default -- those are the ones the caller must supply. A variable
    # with no default is already T1 and is not blamed here.
    blamed = [n for n in referenced
              if env[n] is not _UNKNOWN
              and _compared_to_own_default(condition, n, env[n])]
    return sorted(blamed)


def _compared_to_own_default(condition: str, name: str, default: Any) -> bool:
    literal = '""' if default == "" else (
        "null" if default is None else str(default))
    pattern = r"var\.%s\s*[!=]=\s*%s" % (re.escape(name), re.escape(literal))
    return re.search(pattern, condition) is not None


# --- a deliberately small expression evaluator -----------------------------
# Models only what preconditions in these repos actually use: comparisons
# against literals, && || !, and parentheses. Anything else -> None (unknown),
# which means "do not mark T2".
#
# Logic is three-valued (Kleene), because a variable with no default has no
# value to substitute. `unknown && false` is still false, which is what lets
# Managed-Redis's naming precondition resolve despite var.app being unknown.


class _Unknown(object):
    def __repr__(self):
        return "UNKNOWN"


_UNKNOWN = _Unknown()


def _kleene_or(left, right):
    if left is True or right is True:
        return True
    if left is None or right is None:
        return None
    return bool(left) or bool(right)


def _kleene_and(left, right):
    if left is False or right is False:
        return False
    if left is None or right is None:
        return None
    return bool(left) and bool(right)

_RE_TOKEN = re.compile(r"""
      (?P<space>\s+)
    | (?P<var>var\.[A-Za-z_][A-Za-z0-9_-]*)
    | (?P<string>"(?:[^"\\]|\\.)*")
    | (?P<op>&&|\|\||==|!=|<=|>=|!|\(|\)|<|>)
    | (?P<number>-?\d+(?:\.\d+)?)
    | (?P<word>[A-Za-z_][A-Za-z0-9_.]*)
""", re.VERBOSE)


def _evaluate(expression: str, env: Dict[str, Any]) -> Optional[bool]:
    try:
        tokens = _tokenise(expression)
        if tokens is None:
            return None
        value, position = _parse_or(tokens, 0, env)
        if position != len(tokens):
            return None
        return value if isinstance(value, bool) else None
    except (_Unsupported, IndexError):
        return None


class _Unsupported(Exception):
    pass


def _tokenise(expression: str) -> Optional[List[Tuple[str, str]]]:
    tokens = []
    position = 0
    while position < len(expression):
        match = _RE_TOKEN.match(expression, position)
        if not match:
            return None
        kind = match.lastgroup
        text = match.group()
        position = match.end()
        if kind == "space":
            continue
        tokens.append((kind, text))
    return tokens


def _parse_or(tokens, index, env):
    value, index = _parse_and(tokens, index, env)
    while index < len(tokens) and tokens[index][1] == "||":
        right, index = _parse_and(tokens, index + 1, env)
        value = _kleene_or(value, right)
    return value, index


def _parse_and(tokens, index, env):
    value, index = _parse_unary(tokens, index, env)
    while index < len(tokens) and tokens[index][1] == "&&":
        right, index = _parse_unary(tokens, index + 1, env)
        value = _kleene_and(value, right)
    return value, index


def _parse_unary(tokens, index, env):
    if index < len(tokens) and tokens[index][1] == "!":
        value, index = _parse_unary(tokens, index + 1, env)
        return (None if value is None else not bool(value)), index
    return _parse_comparison(tokens, index, env)


def _parse_comparison(tokens, index, env):
    left, index = _parse_atom(tokens, index, env)
    if index < len(tokens) and tokens[index][1] in ("==", "!=", "<", ">", "<=", ">="):
        operator = tokens[index][1]
        right, index = _parse_atom(tokens, index + 1, env)
        if left is _UNKNOWN or right is _UNKNOWN:
            return None, index
        if operator == "==":
            return left == right, index
        if operator == "!=":
            return left != right, index
        raise _Unsupported(operator)    # ordering on defaults is not modelled
    return left, index


def _parse_atom(tokens, index, env):
    if index >= len(tokens):
        raise _Unsupported("end of expression")
    kind, text = tokens[index]
    if text == "(":
        value, index = _parse_or(tokens, index + 1, env)
        if index >= len(tokens) or tokens[index][1] != ")":
            raise _Unsupported("unbalanced")
        return value, index + 1
    if kind == "var":
        name = text.split(".", 1)[1]
        if name not in env:
            raise _Unsupported(text)
        return env[name], index + 1
    if kind == "string":
        return text[1:-1], index + 1
    if kind == "number":
        return float(text) if "." in text else int(text), index + 1
    if kind == "word":
        if text == "null":
            return None, index + 1
        if text == "true":
            return True, index + 1
        if text == "false":
            return False, index + 1
        raise _Unsupported(text)        # a function call or an attribute path
    raise _Unsupported(text)


# --------------------------------------------------------------------------
# Repo-wide helper
# --------------------------------------------------------------------------

def discover_modules(repo_root: str) -> List[str]:
    """Every `<Category>/<Module>` holding a variables.tf, case preserved."""
    found = []
    for category in sorted(os.listdir(repo_root)):
        category_dir = os.path.join(repo_root, category)
        if not os.path.isdir(category_dir) or category.startswith("."):
            continue
        for name in sorted(os.listdir(category_dir)):
            module_dir = os.path.join(category_dir, name)
            if os.path.isfile(os.path.join(module_dir, SPEC_FILE)):
                found.append("%s/%s" % (category, name))
    return found
