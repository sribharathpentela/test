"""`validate_value()` -- the ladder from `01-algorithm.md` section 6.

Seven rungs. Rungs 1-4 run offline against the module contract. Rungs 5 and 6
need a usage scan and Azure, so they are *reported as skipped* rather than
silently passed -- a rung that did not run must never look like a rung that
passed.

Rung 7 is WARN only, deliberately: descriptions carry guidance, not law, and
treating prose as a constraint would reject legal values (ADR 0010).

Platform values are validated too. Being platform-sourced is not a trust
exemption -- that is how storage_account's `account_tier = null` surfaces.
"""

import re
from typing import Any, List, Optional

from ..contracts.models import ModuleContract, TypeKind, VariableSpec
from .models import Rung, ValidationOutcome, Verdict

# Rungs this phase cannot run. Recorded on every outcome so a caller can see
# that the ladder was not fully climbed.
OFFLINE_SKIPS = [Rung.UNIQUENESS, Rung.EXISTENCE]

_RE_PROSE_ENUM = re.compile(
    r"(?:possible|valid|allowed|accepted)\s+(?:values|options)\s*(?:are|:)\s*(?P<body>[^.\n]+)",
    re.IGNORECASE)


def validate_value(path: str,
                   value: Any,
                   contract: Optional[ModuleContract],
                   spec: Optional[VariableSpec] = None) -> ValidationOutcome:
    """Run the ladder. Called only from `facts.append()` -- never at a call site.

    `path` may be dotted (`managed_redis_instances.primary.sku_name`); the
    variable is its first segment.
    """
    outcome = ValidationOutcome(skipped=list(OFFLINE_SKIPS))

    if spec is None and contract is not None:
        spec = contract.variables.get(path.split(".", 1)[0])
    if spec is None:
        # No contract yet -- this is the intake stage, where only shape can be
        # checked. Nothing downstream may treat this as verified.
        outcome.message = "shape stage: no contract available for %r" % path
        return outcome

    # --- rung 1: the declared Terraform type -------------------------------
    failure = _check_type(path, value, spec)
    if failure:
        return ValidationOutcome(verdict=Verdict.REJECT, rung=Rung.TYPE,
                                 message=failure, skipped=list(OFFLINE_SKIPS))

    # --- rung 2: authoritative enum, from a validation block ---------------
    #
    # A dotted path addresses a leaf inside a collection, so the map's own enum
    # does not apply to it. PostgreSQL validates `server.version` against
    # ["11".."16"] across every entry; applied at the map level that rejected
    # sku_name, storage_mb and administrator_login for not being versions.
    allowed = _allowed_for(path, spec)
    if allowed and value is not None and value not in allowed:
        return ValidationOutcome(
            verdict=Verdict.REJECT, rung=Rung.AUTHORITATIVE_ENUM,
            message="%s=%r is not one of %s" % (path, value, allowed),
            skipped=list(OFFLINE_SKIPS))

    # --- rung 3: derived constraint ----------------------------------------
    # Length / regex / charset come from the naming recipe, which lives in
    # Shared/naming and is resolved at render time. Not available here.
    # Deliberately not claimed as passed.

    # --- rung 4: a precondition in main.tf ---------------------------------
    failure = _check_preconditions(value, spec, contract)
    if failure:
        return ValidationOutcome(verdict=Verdict.REJECT, rung=Rung.PRECONDITION,
                                 message=failure, skipped=list(OFFLINE_SKIPS))

    # --- rungs 5 and 6 -- not runnable offline; already in `skipped` -------

    # --- rung 7: advisory enum from description prose -- WARN ONLY ---------
    advisory = _advisory_values(spec.description)
    if advisory and value is not None and str(value) not in advisory:
        return ValidationOutcome(
            verdict=Verdict.WARN, rung=Rung.ADVISORY_ENUM,
            message="%s=%r is not among the values the description mentions "
                    "(%s). Advisory only -- the description is guidance, not law."
                    % (path, value, ", ".join(advisory[:6])),
            skipped=list(OFFLINE_SKIPS))

    return outcome


def _allowed_for(path: str, spec: VariableSpec) -> List[Any]:
    """The enum that governs this exact path.

    A bare variable name gets the variable's own enum. A dotted leaf gets the
    attribute enum for its final segment, if the module declared one, and
    otherwise nothing -- never the parent's.
    """
    if "." not in path:
        return spec.allowed_values

    attribute = path.rsplit(".", 1)[-1]
    for validation in spec.validations:
        values = validation.attribute_enums.get(attribute)
        if values:
            return values
    return []


# --------------------------------------------------------------------------
# Rung 1 -- type
# --------------------------------------------------------------------------

def _check_type(path: str, value: Any, spec: VariableSpec) -> str:
    """Check `value` against the declared type.

    `null` is always allowed at this rung: it means "not set, module default
    applies", which is a distinct state from "" and from a value.
    """
    if value is None:
        return ""
    node = spec.type_tree
    if node is None:
        return ""

    # A dotted path addresses a leaf inside the type tree; resolving which leaf
    # is the engine's business. Here we only check whole-variable assignments.
    if "." in path:
        return ""

    kind = node.kind
    if kind == TypeKind.STRING and not isinstance(value, str):
        return "%s is declared string but got %s" % (path, type(value).__name__)
    if kind == TypeKind.BOOL and not isinstance(value, bool):
        return "%s is declared bool but got %r" % (path, value)
    if kind == TypeKind.NUMBER and (isinstance(value, bool)
                                    or not isinstance(value, (int, float))):
        return "%s is declared number but got %r" % (path, value)
    if kind in (TypeKind.MAP, TypeKind.OBJECT) and not isinstance(value, dict):
        return "%s is declared %s but got %s" % (path, kind.value,
                                                 type(value).__name__)
    if kind in (TypeKind.LIST, TypeKind.SET, TypeKind.TUPLE) and \
            not isinstance(value, (list, tuple)):
        return "%s is declared %s but got %s" % (path, kind.value,
                                                 type(value).__name__)
    return ""


# --------------------------------------------------------------------------
# Rung 4 -- preconditions
# --------------------------------------------------------------------------

def _check_preconditions(value: Any, spec: VariableSpec,
                         contract: Optional[ModuleContract]) -> str:
    """Re-evaluate the module's preconditions with this value substituted.

    Reuses the same evaluator that assigns tier T2, so the two can never
    disagree about what a precondition means (ADR 0012).
    """
    if contract is None:
        return ""
    from ..contracts.module_contract import _UNKNOWN, _evaluate
    from ..contracts.hcl_loader import strip_interpolation

    for precondition in contract.preconditions:
        if ("var.%s" % spec.name) not in precondition.condition:
            continue
        env = {}
        for name, other in contract.variables.items():
            if name == spec.name:
                env[name] = value
            elif other.has_default:
                env[name] = other.default
            else:
                env[name] = _UNKNOWN
        if _evaluate(strip_interpolation(precondition.condition), env) is False:
            return precondition.error_message or (
                "precondition rejects %s=%r" % (spec.name, value))
    return ""


# --------------------------------------------------------------------------
# Rung 7 -- advisory enum
# --------------------------------------------------------------------------

def _advisory_values(description: str) -> List[str]:
    """Pull `Possible values: A, B, C` out of prose. Guidance, never law."""
    match = _RE_PROSE_ENUM.search(description or "")
    if not match:
        return []
    body = match.group("body")
    values = []
    for chunk in re.split(r",|\bor\b|\band\b", body):
        item = chunk.strip().strip('`"\'').rstrip(".")
        if item and " " not in item:
            values.append(item)
    return values
