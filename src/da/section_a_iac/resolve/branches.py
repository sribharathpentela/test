"""The 6 branches. `01-algorithm.md` section 3.

    1. ticket / HITL facts     ->  LITERAL          (the user always wins)
    2. auto-injected (8 keys)  ->  OMIT
    3. template says something ->  POINTER | ASK | SECRET | USE+disclose | UNWRAP | OMIT
    4. pointer catalogue match ->  POINTER
    5. required (T1-T4)        ->  ASK
    6. otherwise               ->  OMIT             (module default applies)

Evaluated in order; first match wins. **Branch 1 is first** -- nothing outranks
an explicit user statement (ADR 0002). Any document showing auto-injected first
is stale.

Nothing in here decides anything the inputs do not state. The algorithm decides
what to ask; the LLM only answers.
"""

import re
from typing import List, Optional

from ..contracts.models import (EntryKind, EntryState, ModuleContract,
                                PlatformContext, RequiredTier, TemplateEntry,
                                TemplateMap, TypeKind, VariableSpec)
from .facts import FactsStore
from .models import Action, Branch, FactSource, Resolution

# A variable whose name looks like a credential must never be filled from a
# literal. It is typed as an object naming an environment variable instead, so
# it is structurally impossible for the LLM to return a password (section 7).
SECRET_HINTS = ("password", "secret", "connection_string", "access_key",
                "api_key", "client_secret", "sas_token", "primary_key")


def resolve(spec: VariableSpec,
            facts: FactsStore,
            contract: ModuleContract,
            template: Optional[TemplateMap],
            platform: PlatformContext) -> Resolution:
    """Resolve one declared variable through the branches, in order."""
    name = spec.name
    entry = template.entries.get(name) if template else None

    # ---- Branch 1: ticket / HITL facts --------------------------------
    # First, for every variable including the 8 auto-injected ones. A real unit
    # overrides `env` (shared/StorageAccount/terragrunt.hcl:40) and a ticket
    # that moves `location` must also emit `region` -- both are impossible if
    # Branch 2 runs first.
    stated = facts.user_stated(name)
    if stated:
        record = sorted(stated, key=lambda r: r.round)[-1]
        value, assembled = _assemble(name, stated)
        resolution = Resolution(
            variable=name, branch=Branch.USER_FACT, action=Action.LITERAL,
            value=value, source=record.source, evidence=record.evidence,
        )
        if _is_secret(spec):
            resolution.action = Action.SECRET
        elif assembled and entry is not None and entry.state == EntryState.BOTH:
            # Section 10: "3 UNWRAP + 1" -- the template blueprint gives the
            # shape, the ticket gives the contents. Branch 1 still owns it,
            # because the values came from the user.
            resolution.action = Action.UNWRAP
            resolution.blueprint = entry.blueprint_text
            resolution.disclosure = (
                "shape from the template blueprint at line %d; entries from the "
                "ticket" % entry.line)
        if name in contract.naming_inputs and record.source != FactSource.HITL:
            # This value decides what the resource is called, and it was read
            # out of prose rather than confirmed. Every sample ticket says
            # "Target service repo is X", and `service` is a naming component,
            # so a grounded-but-wrong value ends up in the resource name.
            # Disclosing it puts it in the CONFIRM bucket for a human to check.
            resolution.disclose = True
            resolution.disclosure = (
                "affects the resource name; read from the ticket (%r) -- confirm"
                % (value,))
        elif name in platform.auto_injected:
            resolution.disclose = True
            resolution.disclosure = (
                "overrides the value common.hcl injects (%s)"
                % platform.auto_injected[name])
        elif entry is not None and entry.kind == EntryKind.POINTER:
            # Forced LITERAL: a key whose provenance is not `platform` can never
            # render as a pointer (ADR 0003 rule 2).
            resolution.disclose = True
            resolution.disclosure = (
                "stated in the ticket, so it is written literally instead of "
                "pointing at include.common.locals.%s" % entry.pointer_target)
        return resolution

    # ---- Branch 2: auto-injected ---------------------------------------
    if name in platform.auto_injected:
        return Resolution(
            variable=name, branch=Branch.AUTO_INJECTED, action=Action.OMIT,
            source=FactSource.PLATFORM,
            disclosure="injected by common.hcl as %s"
                       % platform.auto_injected[name],
        )

    # ---- Branch 3: the template ----------------------------------------
    if entry is not None:
        resolution = _from_template(spec, entry, platform)
        if resolution is not None:
            return resolution

    # ---- Branch 4: the pointer catalogue -------------------------------
    if name in platform.pointer_locals:
        return Resolution(
            variable=name, branch=Branch.POINTER_CATALOGUE,
            action=Action.POINTER, pointer_target=name,
            source=FactSource.PLATFORM,
            disclosure="resolved from common.hcl locals",
        )

    # ---- Branch 5: required (T1-T4) ------------------------------------
    if spec.is_required and _still_required(spec, facts, contract):
        return Resolution(
            variable=name, branch=Branch.REQUIRED, action=Action.ASK,
            ask_reason="required (%s) and neither the ticket nor the template "
                       "supplies it" % "/".join(t.value for t in spec.tiers),
        )

    # ---- Branch 6: otherwise -------------------------------------------
    return Resolution(
        variable=name, branch=Branch.OTHERWISE, action=Action.OMIT,
        source=FactSource.MODULE_DEFAULT,
        disclosure="module default applies (%r)" % (spec.default,),
    )


# --------------------------------------------------------------------------
# Branch 3 -- the template resolution table
# --------------------------------------------------------------------------

def _from_template(spec: VariableSpec, entry: TemplateEntry,
                   platform: PlatformContext) -> Optional[Resolution]:
    """`01-algorithm.md` section 3, Branch 3.

    | template shape                          | action           |
    |-----------------------------------------|------------------|
    | "<update-value>"                        | ASK              |
    | include.common.locals.X                 | POINTER          |
    | concrete value, variable HAS a default  | OMIT             |
    | concrete value, variable has NO default | USE + disclose   |
    | commented out only                      | OMIT             |
    | commented AND live                      | UNWRAP           |
    | secret-shaped                           | SECRET           |

    Returns None when the template says nothing usable, so the caller falls
    through to Branch 4.
    """
    name = spec.name

    # Commented only -> the template is not setting it.
    if entry.state == EntryState.COMMENTED:
        return Resolution(
            variable=name, branch=Branch.TEMPLATE, action=Action.OMIT,
            source=FactSource.TEMPLATE,
            disclosure="commented out in the template (documented default %s)"
                       % (entry.documented_default or "unspecified"),
        )

    # Commented AND live -> a blueprint showing the shape of the live value.
    if entry.state == EntryState.BOTH:
        # Structure comes from hcl2; the raw pass is comments and line numbers
        # only (ADR 0005). This was the one place in the branch that used
        # `raw_value` as a *value*, and the raw pass records a single line --
        # so a live `{}` arrived as the string "{}" and rendered quoted, into a
        # module declaring `map(object({...}))`. `null` arrived as "null" the
        # same way. Terraform cannot convert either, so the generated unit did
        # not apply. 22 entries across 9 of the 27 templates reach here with a
        # structure hcl2 had already parsed correctly.
        literal = (entry.parsed_value if entry.has_parsed_value
                   else _coerce(entry.raw_value))
        return Resolution(
            variable=name, branch=Branch.TEMPLATE, action=Action.UNWRAP,
            value=literal, source=FactSource.TEMPLATE,
            blueprint=entry.blueprint_text,
            ask_reason="the template shows the shape but no content; "
                       "entries come from the ticket",
            disclosure="shape taken from the template blueprint at line %d"
                       % entry.line,
        )

    if _is_secret(spec):
        return Resolution(
            variable=name, branch=Branch.TEMPLATE, action=Action.SECRET,
            source=FactSource.TEMPLATE,
            ask_reason="secret-shaped: must be supplied as an environment "
                       "variable name, never a literal",
        )

    if entry.kind == EntryKind.UPDATE_VALUE:
        return Resolution(
            variable=name, branch=Branch.TEMPLATE, action=Action.ASK,
            source=FactSource.TEMPLATE,
            ask_reason="template marks it <update-value>%s"
                       % ((", e.g. " + entry.example) if entry.example else ""),
        )

    if entry.kind == EntryKind.POINTER:
        return Resolution(
            variable=name, branch=Branch.TEMPLATE, action=Action.POINTER,
            pointer_target=entry.pointer_target, source=FactSource.PLATFORM,
            disclosure="template points at include.common.locals.%s"
                       % entry.pointer_target,
        )

    # A live concrete literal -> copy it, and disclose.
    #
    # Template literals are examples, not policy (ADR 0009), but the fleet stays
    # consistent *because* engineers copy the template, so DA copies it too.
    # Both real Redis units match their template on all three divergent keys.
    #
    # Note: section 3's table says "concrete value, variable HAS a default ->
    # OMIT". That row is stale -- it loses `persistence_rdb_backup_frequency =
    # "12h"` against a module default of null, i.e. a Redis with no backups.
    # Q1 in 05-open-questions records the current design as **copy + disclose**,
    # and section 10 lists all five of Managed-Redis's literals as USE+disclose.
    if entry.kind in (EntryKind.LITERAL, EntryKind.NULL, EntryKind.EMPTY_MAP,
                      EntryKind.EMPTY_LIST):
        literal = (entry.parsed_value if entry.has_parsed_value
                   else _coerce(entry.raw_value))

        # A block still carrying an unfilled placeholder is an example, not a
        # value. AKS's `additional_nodepools` is keyed `<nodepool-name>` and its
        # taints read `dedicated=<nodepool-name>:NoSchedule`; copying it emits
        # HCL that does not parse. `<update-value>` is caught above only when it
        # is the whole value -- nested inside a block it needs finding too.
        if _contains_placeholder(literal):
            if spec.is_required:
                return Resolution(
                    variable=name, branch=Branch.TEMPLATE, action=Action.ASK,
                    source=FactSource.TEMPLATE,
                    ask_reason="the template shows an example containing "
                               "placeholders (%s); the real values must come "
                               "from the ticket" % _placeholders(literal)[0],
                )
            return Resolution(
                variable=name, branch=Branch.TEMPLATE, action=Action.OMIT,
                source=FactSource.TEMPLATE,
                disclosure="template value is an unfilled example (%s); left at "
                           "the module default %r"
                           % (_placeholders(literal)[0], spec.default),
            )

        if not spec.has_default:
            return Resolution(
                variable=name, branch=Branch.TEMPLATE,
                action=Action.USE_DISCLOSE, value=literal,
                source=FactSource.TEMPLATE, disclose=True,
                disclosure="taken from the template (line %d); the module "
                           "declares no default, and the ticket did not state "
                           "one" % entry.line,
            )

        if _differs(literal, spec.default):
            # The case that matters: copying silently changes behaviour away
            # from the module default. Always in the PR disclosure table.
            return Resolution(
                variable=name, branch=Branch.TEMPLATE,
                action=Action.USE_DISCLOSE, value=literal,
                module_default=spec.default, has_module_default=True,
                source=FactSource.TEMPLATE, disclose=True,
                disclosure="template (line %d) sets %r; the module default is "
                           "%r -- using the template value"
                           % (entry.line, literal, spec.default),
            )

        # Matches the module default: still copied, so the unit stays pinned if
        # the module default later changes upstream. Nothing to disclose.
        return Resolution(
            variable=name, branch=Branch.TEMPLATE,
            action=Action.USE_DISCLOSE, value=literal,
            source=FactSource.TEMPLATE, disclose=False,
            disclosure="template (line %d) matches the module default (%r)"
                       % (entry.line, literal),
        )

    return None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _assemble(name, records):
    """Build a variable's value from its fact records.

    Facts are path-keyed and a payload map arrives as dotted leaves --
    `managed_redis_instances.primary.sku_name`. Taking one leaf's value as the
    whole variable would emit `managed_redis_instances = "Balanced_B3"` instead
    of the map the module declares.

    Returns (value, was_assembled). A record whose path is exactly the variable
    name is a whole-variable assignment and wins outright.
    """
    exact = [r for r in records if r.path == name]
    if exact:
        return sorted(exact, key=lambda r: r.round)[-1].value, False

    root = {}
    for record in sorted(records, key=lambda r: r.round):
        segments = record.path.split(".")[1:]
        if not segments:
            continue
        cursor = root
        for segment in segments[:-1]:
            nested = cursor.get(segment)
            if not isinstance(nested, dict):
                nested = {}
                cursor[segment] = nested
            cursor = nested
        cursor[segments[-1]] = record.value
    return root, True


def _still_required(spec: VariableSpec, facts: FactsStore,
                    contract: ModuleContract) -> bool:
    """Is this variable *still* required, given the facts gathered so far?

    T1 and T3 are properties of the declaration and never lapse. T2 is not: it
    says a precondition rejects the declared default, and a precondition can be
    satisfied by a *different* variable.

    Managed-Redis is the worked case. Its naming precondition is a disjunction:
    `resource_prefix != "" || (app != "" && service != "")`. With nothing
    supplied, both `resource_prefix` and `service` are T2 and both are asked.
    Once the ticket supplies `resource_prefix`, the precondition passes and
    `service` is no longer required -- section 10 puts it as "also clears the T2
    precondition". Re-checking here is what makes the ASK set shrink between
    rounds instead of demanding a value the module stopped needing.
    """
    from ..contracts.hcl_loader import strip_interpolation
    from ..contracts.module_contract import _UNKNOWN, _evaluate

    tiers = {tier for tier in spec.tiers}
    if any(t in tiers for t in (RequiredTier.T1, RequiredTier.T3)):
        return True
    if RequiredTier.T2 not in tiers:
        return True

    env = {}
    for other_name, other in contract.variables.items():
        record = facts.get(other_name)
        if record is not None:
            env[other_name] = record.value
        elif other.has_default:
            env[other_name] = other.default
        else:
            env[other_name] = _UNKNOWN

    for precondition in contract.preconditions:
        if ("var.%s" % spec.name) not in precondition.condition:
            continue
        if _evaluate(strip_interpolation(precondition.condition), env) is False:
            return True          # still rejected -> still required
    return False


def _is_secret(spec: VariableSpec) -> bool:
    """Secret-shaped, by declaration or by name.

    The type guard matters: `access_keys_authentication_enabled` contains
    "access_key" but is a `bool` toggle. A boolean cannot carry a credential, and
    treating it as a secret would route a plain feature flag through the
    env-var-name machinery and ask the user for a variable name that does not
    exist. Only string-typed variables can hold one.
    """
    # The type guard runs first, and `sensitive` does not override it.
    # PostgreSQL's `postgresql_flexible_servers` map is marked sensitive because
    # it *contains* administrator_password -- the map itself is still a map, and
    # treating it as a secret typed every one of its leaves (sku_name,
    # storage_mb, version) as an env-var-name object.
    if spec.type_tree is not None and spec.type_tree.kind != TypeKind.STRING:
        return False
    if spec.sensitive:
        return True
    lowered = spec.name.lower()
    return any(hint in lowered for hint in SECRET_HINTS)


#: The templates mark a slot a human must fill as `<something>`.
_RE_PLACEHOLDER = re.compile(r"<[A-Za-z][A-Za-z0-9_-]*>")


def _placeholders(value) -> List[str]:
    """Every `<placeholder>` anywhere in a value, including keys."""
    found: List[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key, item in node.items():
                walk(key)
                walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            for match in _RE_PLACEHOLDER.findall(node):
                if match not in found:
                    found.append(match)

    walk(value)
    return found


def _contains_placeholder(value) -> bool:
    return bool(_placeholders(value))


def _coerce(raw_value: str):
    """Turn a template's source text into a Python value."""
    text = (raw_value or "").strip()
    if text == "null":
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    if text == "{}":
        return {}
    if text == "[]":
        return []
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        return text[1:-1]
    try:
        return int(text)
    except (TypeError, ValueError):
        pass
    try:
        return float(text)
    except (TypeError, ValueError):
        return text


def _differs(literal, default) -> bool:
    """Does the template's literal differ from the module default?

    `null` and `""` are distinct states and are never collapsed (section 6).
    """
    if literal is None and default is None:
        return False
    if literal is None or default is None:
        return True
    if isinstance(literal, bool) or isinstance(default, bool):
        return bool(literal) is not bool(default)
    return literal != default
