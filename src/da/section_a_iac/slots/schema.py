"""Generate the slot schema from the module contract. ADR 0007.

The pipeline never searches for a variable name. It computes the ASK set from
the contract and emits a JSON Schema whose property names **are** the
`variables.tf` names, so there is nothing for the LLM to rename and nothing for
the pipeline to fuzzy-match.

Nothing here reads the ticket. The schema depends only on the module and on what
resolution already settled -- which is what makes "the algorithm decides what to
ask, the LLM only answers" true rather than aspirational.
"""

from typing import Dict, List, Optional

from ..contracts.hcl_loader import walk_type
from ..contracts.models import (ModuleContract, PlatformContext, TemplateMap,
                                TypeKind, TypeNode)
from ..resolve.branches import SECRET_HINTS, _is_secret
from ..resolve.models import Action, ClosureProof
from .models import Slot, SlotSchema, SlotTier

# Where a payload-map entry lands when the ticket does not name the key.
# Q8 ("map key naming convention") is open with DevOps -- section 10's worked
# example uses "primary", so that is the placeholder, not a decision.
DEFAULT_MAP_KEY = "primary"

_JSON_TYPES = {
    TypeKind.STRING: "string",
    TypeKind.NUMBER: "number",
    TypeKind.BOOL: "boolean",
    TypeKind.ANY: "string",
    TypeKind.MAP: "object",
    TypeKind.OBJECT: "object",
    TypeKind.LIST: "array",
    TypeKind.SET: "array",
    TypeKind.TUPLE: "array",
}


def build_slot_schema(contract: ModuleContract,
                      proof: ClosureProof,
                      template: Optional[TemplateMap] = None,
                      platform: Optional[PlatformContext] = None,
                      include_may: bool = True) -> SlotSchema:
    """Build the schema for one module, given what resolution already decided."""
    schema = SlotSchema(module_path=contract.module_path)
    auto_injected = set(platform.auto_injected) if platform else set()
    ask_set = set(proof.ask_set)

    # Collect candidates as (property_name, Slot) before resolving collisions.
    candidates: List[Slot] = []

    for name in sorted(contract.variables):
        spec = contract.variables[name]
        resolution = proof.resolutions.get(name)
        tier = _tier_for(name, spec, resolution, auto_injected, ask_set)
        if tier is None:
            continue
        if tier is SlotTier.MAY and not include_may:
            continue

        node = spec.type_tree
        payload_leaves = _payload_leaves(name, node, contract)

        if payload_leaves:
            # A map of objects: the slots are its attributes, not the map.
            for leaf_path, leaf_node in payload_leaves:
                attribute = leaf_path.split(".")[-1]
                candidates.append(Slot(
                    name=attribute,
                    path="%s.%s.%s" % (name, DEFAULT_MAP_KEY, attribute),
                    tier=tier,
                    json_type=_json_type(leaf_node),
                    description=_describe(spec, attribute),
                    enum=_attribute_enum(spec, attribute),
                    variable=name,
                    # Secrecy is decided per attribute. Inheriting it from the
                    # parent made every leaf of PostgreSQL's sensitive map --
                    # sku_name, storage_mb, version -- an env-var-name object.
                    is_secret=_leaf_is_secret(attribute, leaf_node),
                ))
            continue

        candidates.append(Slot(
            name=name,
            path=name,
            tier=tier,
            json_type=_json_type(node),
            description=_describe(spec),
            enum=list(spec.allowed_values),
            variable=name,
            is_secret=_is_secret(spec),
        ))

    for slot in _disambiguate(candidates):
        schema.slots[slot.name] = slot

    schema.message = "%s: %d slots (%d blocking)" % (
        contract.module_path, len(schema.slots), len(schema.blocking))
    return schema


# --------------------------------------------------------------------------
# Tiers
# --------------------------------------------------------------------------

def _tier_for(name, spec, resolution, auto_injected,
              ask_set) -> Optional[SlotTier]:
    """ASK blocks; CONFIRM has a fallback worth disclosing; MAY is the rest.

    The blocking set is taken straight from the closure proof rather than
    recomputed here. The engine already decides that an UNWRAP with no contents
    and a SECRET with no value are blocking, and two places deciding the same
    thing is two places to disagree.

    An auto-injected key is not offered as a slot unless resolution left it
    asking: common.hcl supplies it, and inviting the model to fill it is
    inviting an override nobody requested.
    """
    if name in ask_set:
        return SlotTier.ASK
    if resolution is None:
        return SlotTier.MAY
    if name in auto_injected:
        return None
    # A value DA chose on the ticket's behalf -- the user may want a say.
    if resolution.disclose:
        return SlotTier.CONFIRM
    return SlotTier.MAY


# --------------------------------------------------------------------------
# Payload maps
# --------------------------------------------------------------------------

def _payload_leaves(name: str, node: Optional[TypeNode],
                    contract: ModuleContract):
    """Leaf attributes of a map-of-object, as (path, node) pairs.

    Only for the primary payload map and other map(object(...)) variables: those
    are the ones whose *contents* come from the ticket. A plain map(string) has
    no attributes to ask about.
    """
    if node is None or node.kind is not TypeKind.MAP:
        return []
    if node.of is None or node.of.kind is not TypeKind.OBJECT:
        return []
    return [(path, leaf) for path, leaf in walk_type(node) if path]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _leaf_is_secret(attribute: str, node: Optional[TypeNode]) -> bool:
    """Is this one attribute of a collection entry a credential?

    `administrator_password` is; `sku_name` in the same map is not. Only
    string-typed attributes qualify -- a number or a bool cannot carry one.
    """
    if node is not None and node.kind != TypeKind.STRING:
        return False
    lowered = attribute.lower()
    return any(hint in lowered for hint in SECRET_HINTS)


def _attribute_enum(spec, attribute: str) -> List:
    for validation in spec.validations:
        values = validation.attribute_enums.get(attribute)
        if values:
            return list(values)
    return []


def _json_type(node: Optional[TypeNode]) -> List[str]:
    """Always nullable -- "not stated in the ticket" must be expressible."""
    if node is None:
        return ["string", "null"]
    return [_JSON_TYPES.get(node.kind, "string"), "null"]


def _describe(spec, attribute: str = "") -> str:
    """One line of guidance. The description is prose, never a constraint."""
    text = " ".join((spec.description or "").split())
    if attribute:
        text = "%s of each %s entry. %s" % (attribute, spec.name, text)
    return text[:300]


def _disambiguate(candidates: List[Slot]) -> List[Slot]:
    """Two variables must never offer the same property name.

    A collision would make the mapping back to a fact path ambiguous, which is
    the renaming problem the inversion exists to remove. On collision both slots
    fall back to their full dotted path.
    """
    counts: Dict[str, int] = {}
    for slot in candidates:
        counts[slot.name] = counts.get(slot.name, 0) + 1

    resolved = []
    for slot in candidates:
        if counts[slot.name] > 1:
            slot = slot.model_copy(update={"name": slot.path})
        resolved.append(slot)
    return resolved
