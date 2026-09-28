"""Models for the precedence engine (Phase 2).

The facts store record shape is fixed by `02-contracts-and-trust.md` section 1.
The branches and actions are `01-algorithm.md` section 3.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Facts store
# --------------------------------------------------------------------------

class FactSource(str, Enum):
    """Provenance is functional, not cosmetic (02-contracts-and-trust 1).

    It drives three behaviours: a key whose source is not `platform` can never
    render as a POINTER; the PR provenance table; and what needs
    re-confirmation on a later round.
    """

    JIRA = "jira"
    HITL = "hitl"
    SUPERVISOR = "supervisor"
    PLATFORM = "platform"
    TEMPLATE = "template"
    MODULE_DEFAULT = "module_default"

    @property
    def is_user_stated(self) -> bool:
        """Branch 1 sources -- an explicit human statement. The user always wins."""
        return self in (FactSource.JIRA, FactSource.HITL, FactSource.SUPERVISOR)


class Trust(str, Enum):
    UNTRUSTED = "untrusted"      # anything that came from outside DA
    VERIFIED = "verified"


class FactRecord(BaseModel):
    path: str                    # dotted, from the type-tree walk
    value: Any = None
    source: FactSource
    trust: Trust = Trust.UNTRUSTED
    evidence: Optional[str] = None   # verbatim substring of the source text
    round: int = 0                   # 0 = Jira, 1..N = HITL turns
    ts: str = ""

    @property
    def variable(self) -> str:
        """The top-level variable this path belongs to."""
        return self.path.split(".", 1)[0]


# --------------------------------------------------------------------------
# Validation -- the ladder (01-algorithm.md section 6)
# --------------------------------------------------------------------------

class Verdict(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    WARN = "warn"
    HALT = "halt"          # rung 6 on 403/401 -- cannot verify, never "absent"


class Rung(int, Enum):
    TYPE = 1               # declared Terraform type
    AUTHORITATIVE_ENUM = 2  # a validation block
    DERIVED_CONSTRAINT = 3  # length / regex / charset from the naming recipe
    PRECONDITION = 4        # a precondition in main.tf
    UNIQUENESS = 5          # scoped per section 1 rule 3
    EXISTENCE = 6           # in Azure, via EffectiveContext
    ADVISORY_ENUM = 7       # prose in a description -- WARN ONLY


class ValidationOutcome(BaseModel):
    verdict: Verdict = Verdict.ACCEPT
    rung: Optional[Rung] = None
    message: str = ""
    # Rungs that could not run here (no Azure, no usage scan). Never silent.
    skipped: List[Rung] = Field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.verdict in (Verdict.ACCEPT, Verdict.WARN)


# --------------------------------------------------------------------------
# Resolution -- the 6 branches
# --------------------------------------------------------------------------

class Branch(int, Enum):
    USER_FACT = 1          # ticket / HITL   -> LITERAL
    AUTO_INJECTED = 2      # the 8 keys      -> OMIT
    TEMPLATE = 3           # the resolution table
    POINTER_CATALOGUE = 4  # the 7 locals    -> POINTER
    REQUIRED = 5           # T1-T4           -> ASK
    OTHERWISE = 6          # module default  -> OMIT


class Action(str, Enum):
    LITERAL = "LITERAL"
    OMIT = "OMIT"
    POINTER = "POINTER"
    ASK = "ASK"
    SECRET = "SECRET"
    USE_DISCLOSE = "USE+disclose"
    UNWRAP = "UNWRAP"


class Resolution(BaseModel):
    """What happened to one declared variable, and why."""

    variable: str
    branch: Branch
    action: Action
    value: Any = None
    pointer_target: str = ""          # for POINTER: the common.hcl local
    source: Optional[FactSource] = None
    evidence: Optional[str] = None

    # Disclosure -- the PR body line. Set whenever DA used a value the ticket
    # did not state, or used a template literal that differs from the module
    # default (ADR 0009).
    disclose: bool = False
    disclosure: str = ""

    # The module default this value departs from, kept raw so the renderer can
    # print it as HCL (`false`, `null`) rather than as a Python repr.
    module_default: Any = None
    has_module_default: bool = False

    # For UNWRAP: the blueprint that shows the shape to fill.
    blueprint: str = ""
    # For ASK: what the user must be asked about.
    ask_reason: str = ""

    validation: Optional[ValidationOutcome] = None

    @property
    def renders(self) -> bool:
        """Does this variable emit a line in the generated HCL?"""
        return self.action in (Action.LITERAL, Action.POINTER, Action.SECRET,
                               Action.USE_DISCLOSE, Action.UNWRAP)


# --------------------------------------------------------------------------
# Closure proof (01-algorithm.md section 8)
# --------------------------------------------------------------------------

class ClosureProof(BaseModel):
    """Render only if every variable landed in exactly one branch and ASK is empty."""

    module_path: str
    resolutions: Dict[str, Resolution] = Field(default_factory=dict)
    ask_set: List[str] = Field(default_factory=list)
    # A declared variable that reached no branch. This is a bug in the
    # algorithm, not a value to guess -- halt.
    unclassified: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    halts: List[str] = Field(default_factory=list)
    message: str = ""

    @property
    def complete(self) -> bool:
        return (not self.unclassified) and (not self.ask_set) and (not self.halts)

    @property
    def may_render(self) -> bool:
        return self.complete

    def by_branch(self) -> Dict[int, List[str]]:
        grouped: Dict[int, List[str]] = {}
        for name, resolution in self.resolutions.items():
            grouped.setdefault(int(resolution.branch), []).append(name)
        return {k: sorted(v) for k, v in sorted(grouped.items())}

    def provenance_table(self) -> List[Dict[str, str]]:
        """The PR provenance table -- the closure proof, made readable."""
        rows = []
        for name in sorted(self.resolutions):
            resolution = self.resolutions[name]
            if resolution.action == Action.OMIT:
                shown = "(omitted)"
            elif resolution.action == Action.POINTER:
                shown = "include.common.locals.%s" % resolution.pointer_target
            else:
                shown = str(resolution.value)
            rows.append({
                "variable": name,
                "branch": str(int(resolution.branch)),
                "source": resolution.source.value if resolution.source else "-",
                "value": shown,
                "note": resolution.disclosure,
            })
        return rows
