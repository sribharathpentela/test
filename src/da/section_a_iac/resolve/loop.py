"""Step 4a — the resolution loop. `01-algorithm.md` section 8.

    round 0 = the Jira ticket
    repeat:
        resolve every declared variable through the 6 branches
        if the ASK set is empty: break
        emit a slot schema -> HITL round N -> facts.append(...)
    render

Driven as a **state machine**, not a blocking loop. DA is invoked by a
supervisor, so it returns a question and waits to be called again rather than
reading stdin. `hitl2_basic_iac.py` blocks on `input()`; that suits a Gate B
console prompt and does not suit a step that a supervisor drives.

Every round **recomputes from scratch** from the facts store. The store is
append-only, so there is no partial state for a late answer to corrupt and no
ordering dependency between rounds -- that is what makes multi-round HITL safe
rather than merely convenient (ADR 0010).
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..contracts.models import ModuleContract, PlatformContext, TemplateMap
from ..slots.models import SlotSchema, SlotTier
from ..slots.schema import build_slot_schema
from .engine import resolve_module
from .facts import FactsStore
from .models import ClosureProof, FactSource

MAX_HITL_ROUNDS = 3          # matches HITL2BasicIaCHandler.MAX_RETRIES


class Step4AStatus(str, Enum):
    RENDER_READY = "render_ready"    # closure proven -- Step 4b may run
    NEEDS_HITL = "needs_hitl"        # questions are waiting for a human
    HALT = "halt"                    # unrecoverable, or the retry budget is out


class HitlQuestion(BaseModel):
    """One thing DA needs a human to supply.

    `path` is where the answer lands in the facts store; `variable` is what it
    is called in `variables.tf`. Both are shown, because a person answering
    "sku_name" for a payload map should see which collection it belongs to.
    """

    path: str
    variable: str
    prompt: str
    json_type: List[str] = Field(default_factory=list)
    allowed_values: List[Any] = Field(default_factory=list)
    example: Optional[str] = None
    why: str = ""
    is_secret: bool = False


class HitlRequest(BaseModel):
    module_path: str
    round: int
    questions: List[HitlQuestion] = Field(default_factory=list)
    # Values DA chose that the human may want to change. Not blocking.
    confirmations: List[Dict[str, str]] = Field(default_factory=list)
    message: str = ""


class Step4AResult(BaseModel):
    status: Step4AStatus
    module_path: str
    round: int = 0
    proof: Optional[ClosureProof] = None
    hitl_request: Optional[HitlRequest] = None
    message: str = ""

    @property
    def may_render(self) -> bool:
        return self.status is Step4AStatus.RENDER_READY


class Step4A(object):
    """Holds the facts store across rounds and answers "what now?"."""

    def __init__(self, contract: ModuleContract,
                 template: Optional[TemplateMap],
                 platform: PlatformContext,
                 facts: Optional[FactsStore] = None,
                 max_rounds: int = MAX_HITL_ROUNDS):
        self.contract = contract
        self.template = template
        self.platform = platform
        self.facts = facts if facts is not None else FactsStore(contract)
        self.max_rounds = max_rounds
        self.round = 0
        self.history: List[Dict[str, Any]] = []

    # -- the state machine --------------------------------------------------

    def advance(self) -> Step4AResult:
        """Resolve with what is known, and say what is needed next."""
        proof = resolve_module(self.contract, self.template, self.platform,
                               self.facts)
        schema = build_slot_schema(self.contract, proof, self.template,
                                   self.platform)
        self._record(proof, schema)

        if proof.halts:
            return Step4AResult(
                status=Step4AStatus.HALT, module_path=self.contract.module_path,
                round=self.round, proof=proof,
                message="halted: %s" % "; ".join(proof.halts[:3]))

        if not proof.ask_set:
            return Step4AResult(
                status=Step4AStatus.RENDER_READY,
                module_path=self.contract.module_path, round=self.round,
                proof=proof,
                message="closure proven: %d variables, ASK empty"
                        % len(proof.resolutions))

        if self.round >= self.max_rounds:
            # The budget is spent and the ticket is still short. Escalate rather
            # than invent -- an unanswered required variable is not a value to
            # guess (section 8).
            return Step4AResult(
                status=Step4AStatus.HALT,
                module_path=self.contract.module_path, round=self.round,
                proof=proof,
                message="HITL exhausted after %d rounds; still missing %s. "
                        "Escalate to human DevOps."
                        % (self.max_rounds, ", ".join(proof.ask_set)))

        return Step4AResult(
            status=Step4AStatus.NEEDS_HITL,
            module_path=self.contract.module_path, round=self.round,
            proof=proof,
            hitl_request=self._build_request(proof, schema),
            message="%d question(s) for round %d"
                    % (len(proof.ask_set), self.round + 1))

    def submit(self, answers: Dict[str, Any]) -> Step4AResult:
        """Take one round of human answers and re-resolve.

        Answers are keyed by the `path` each question carried. They are written
        through `facts.append()`, so the ladder runs on them exactly as it does
        on anything else -- a human is authoritative about intent, not about
        whether a value is legal for the module.
        """
        self.round += 1
        rejected = []
        for path, value in sorted(answers.items()):
            if value is None:
                continue
            outcome = self.facts.append(path, value, FactSource.HITL,
                                        evidence="hitl round %d" % self.round,
                                        round=self.round)
            if not outcome.accepted:
                rejected.append("%s: %s" % (path, outcome.message))

        result = self.advance()
        if rejected:
            result.message = "%s (rejected: %s)" % (result.message,
                                                    "; ".join(rejected))
        return result

    # -- building the request ----------------------------------------------

    def _build_request(self, proof: ClosureProof,
                       schema: SlotSchema) -> HitlRequest:
        request = HitlRequest(module_path=self.contract.module_path,
                              round=self.round + 1)
        asked_paths = set()

        for name, slot in sorted(schema.slots.items()):
            if slot.tier is not SlotTier.ASK or slot.path in asked_paths:
                continue
            asked_paths.add(slot.path)
            resolution = proof.resolutions.get(slot.variable)
            entry = (self.template.entries.get(slot.variable)
                     if self.template else None)
            request.questions.append(HitlQuestion(
                path=slot.path,
                variable=slot.variable,
                prompt=_prompt_for(slot, entry),
                json_type=list(slot.json_type),
                allowed_values=list(slot.enum),
                example=entry.example if entry is not None else None,
                why=(resolution.ask_reason if resolution else "") or
                    "required by the module contract",
                is_secret=slot.is_secret,
            ))

        for name, slot in sorted(schema.slots.items()):
            if slot.tier is not SlotTier.CONFIRM:
                continue
            resolution = proof.resolutions.get(slot.variable)
            if resolution is None:
                continue
            request.confirmations.append({
                "variable": slot.variable,
                "value": str(resolution.value),
                "note": resolution.disclosure,
            })

        request.message = "%d question(s), %d value(s) to confirm" % (
            len(request.questions), len(request.confirmations))
        return request

    # -- bookkeeping --------------------------------------------------------

    def _record(self, proof: ClosureProof, schema: SlotSchema) -> None:
        counts = {"ASK": 0, "CONFIRM": 0, "MAY": 0}
        for slot in schema.slots.values():
            counts[slot.tier.value] += 1
        self.history.append({
            "round": self.round,
            "tiers": counts,
            "ask": list(proof.ask_set),
            "unclassified": list(proof.unclassified),
            "facts": len(self.facts),
        })


# --------------------------------------------------------------------------
# Prompt wording
# --------------------------------------------------------------------------

def _prompt_for(slot, entry) -> str:
    """A question a human can answer without reading variables.tf."""
    if slot.is_secret:
        return ("Name of the environment variable holding %s (never the value "
                "itself)" % slot.name)

    text = "Value for %s" % slot.name
    if slot.path != slot.name:
        text += " (entry in %s)" % slot.variable
    if slot.enum:
        text += " -- one of: %s" % ", ".join(str(v) for v in slot.enum[:8])
    elif entry is not None and entry.example:
        text += " -- e.g. %s" % entry.example
    return text
