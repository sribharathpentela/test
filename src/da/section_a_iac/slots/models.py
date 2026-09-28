"""Models for slot filling (Phase 3). `02-contracts-and-trust.md` section 2.

The pipeline never searches for a variable. It hands the LLM the target names,
generated from the module contract, and the LLM only fills them in.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SlotTier(str, Enum):
    """`02-contracts-and-trust.md` section 2.

    MAY is what reaches the **invisible variables** -- declared in variables.tf
    but absent from the template. Without it, storage_account's 13 extra
    variables would be unreachable (ADR 0008).
    """

    ASK = "ASK"            # blocking: required, unresolved, cannot render without
    CONFIRM = "CONFIRM"    # non-blocking: has a fallback that deserves disclosure
    MAY = "MAY"            # non-blocking: every other declared variable

    @property
    def blocking(self) -> bool:
        return self is SlotTier.ASK


class Slot(BaseModel):
    """One property in the generated schema.

    `name` is the property the LLM sees. `path` is where an accepted value lands
    in the facts store. They differ only for leaves inside a payload map, where
    the property is the attribute name and the path carries the map key.
    """

    name: str
    path: str
    tier: SlotTier
    json_type: List[str] = Field(default_factory=list)   # always includes "null"
    description: str = ""
    enum: List[Any] = Field(default_factory=list)
    variable: str = ""
    # Secrets are typed as an object naming an environment variable, never a
    # string -- it must be structurally impossible to return a password.
    is_secret: bool = False


class SlotSchema(BaseModel):
    """The generated JSON Schema, plus the mapping back to fact paths."""

    module_path: str
    slots: Dict[str, Slot] = Field(default_factory=dict)
    message: str = ""

    @property
    def blocking(self) -> List[str]:
        return sorted(n for n, s in self.slots.items() if s.tier.blocking)

    def path_for(self, name: str) -> Optional[str]:
        slot = self.slots.get(name)
        return slot.path if slot else None

    def json_schema(self) -> Dict[str, Any]:
        """The schema handed to the LLM.

        Three properties make this work (section 2):
          * names generated from the contract -- the LLM cannot rename
          * additionalProperties: false -- invented keys are rejected
          * every slot nullable -- "not stated" is expressible, so the model is
            never forced to invent
        """
        properties: Dict[str, Any] = {}
        for name, slot in sorted(self.slots.items()):
            if slot.is_secret:
                # Never a string slot. There is no place to put a password.
                properties[name] = {
                    "type": ["object", "null"],
                    "properties": {"env_var_name": {"type": "string"}},
                    "required": ["env_var_name"],
                    "additionalProperties": False,
                    "description": slot.description,
                }
                continue
            entry: Dict[str, Any] = {"type": list(slot.json_type)}
            if slot.description:
                entry["description"] = slot.description
            if slot.enum:
                entry["enum"] = list(slot.enum) + [None]
            properties[name] = entry

        return {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
        }

    def evidence_schema(self) -> Dict[str, Any]:
        """Schema for the parallel evidence object: slot name -> quoted span."""
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {name: {"type": ["string", "null"]}
                           for name in sorted(self.slots)},
        }


# --------------------------------------------------------------------------
# Filling
# --------------------------------------------------------------------------

class DropReason(str, Enum):
    NO_EVIDENCE = "no_evidence"              # the model quoted nothing
    EVIDENCE_NOT_FOUND = "evidence_not_found"  # the quote is not in the source
    UNKNOWN_SLOT = "unknown_slot"            # a key the schema does not define
    NULL = "null"                            # legitimately "not stated"


class FilledSlot(BaseModel):
    name: str
    path: str
    value: Any = None
    evidence: str = ""
    accepted: bool = True
    drop_reason: Optional[DropReason] = None


class FillResult(BaseModel):
    module_path: str
    accepted: List[FilledSlot] = Field(default_factory=list)
    dropped: List[FilledSlot] = Field(default_factory=list)
    # Keys the model returned that the schema does not define. Should always be
    # empty -- additionalProperties: false -- so a non-empty list is a signal.
    invented_keys: List[str] = Field(default_factory=list)
    raw_response: str = ""
    message: str = ""

    @property
    def unfilled(self) -> List[str]:
        return sorted(s.name for s in self.dropped)
