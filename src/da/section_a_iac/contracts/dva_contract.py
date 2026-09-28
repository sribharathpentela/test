"""The DA <-> DVA wire contract (Step 4b, Phase 6).

DA is the side that knows what it can supply at 4b -- it holds the
`EffectiveContext` (ADR 0003) and the code-side flag set by 3b -- so the request
half is frozen here and DVA implements against it. Freezing it the other way
round is how R2 discovers a request DA cannot build.

Two endpoints, split by **role** -- not by data source:

    POST /v1/precheck   called by DA, mid-flow. Advisory.
                        Carries no exit code.

    POST /v1/validate   called by any CI/CD pipeline, before promotion.
                        Authoritative. Returns a verdict, an exit code that
                        halts the run, and optionally inspects `.tfstate`.

**Both read Azure Resource Graph today** (ADR 0023). ARM REST GET is a deferred
fallback for `/v1/validate`, to be added only if ARG proves unreliable in a real
run. `source` is the seam that lets it arrive without a contract change: every
response states which surface answered it.

Because both endpoints now read the same surface, the no-exit-code rule is the
only structural thing keeping them apart. `PrecheckResponse` has no `exit_code`
field at all -- not a field set to None, no field -- so wiring the advisory
answer into a gate cannot be done by accident.

`as_of` records when DVA queried, not how stale ARG's index was. It does not
prove freshness and must not be made to carry that weight.

Everything DVA returns is text from outside DA, so every model here forbids
extra keys (rule 9 / ADR 0011). Containment is structural, not a review habit.
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


# The blob every unit's state is written to. From `root.hcl`'s backend block,
# not from the doc summary of the key order, which stops at the trailing slash.
STATE_FILENAME = "terraform.tfstate"


class _Wire(BaseModel):
    """Base for every model that crosses the boundary.

    `extra="forbid"` is the containment. A field DVA invents does not silently
    become an attribute DA might later read.
    """

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Shared vocabulary
# --------------------------------------------------------------------------

class Existence(str, Enum):
    """The answer to "is it in Azure?" -- with the third case that matters.

    `CANNOT_VERIFY` is not `ABSENT`. A 403 or a 401 means the query could not be
    made, and treating that as "not there" is how a duplicate gets provisioned
    over a resource the agent simply could not see. ADR 0003 rule 4 -- it HALTs.
    """

    PRESENT = "present"
    ABSENT = "absent"
    CANNOT_VERIFY = "cannot_verify"


class NameScope(str, Enum):
    """Where a name has to be unique -- ADR 0003 rule 3.

    The scope comes from the resource type, not from the caller's preference. A
    storage account name is globally unique across all of Azure, so the
    subscription is irrelevant to the question; a resource-group-scoped name is
    only unique within the effective subscription *and* RG.
    """

    GLOBAL = "global"
    SUBSCRIPTION = "subscription"
    RESOURCE_GROUP = "resource_group"


class Source(str, Enum):
    """Which Azure surface answered. Never inferred -- always echoed back.

    Both endpoints return ARG today. ARM exists here so the deferred fallback is
    a value change rather than a schema change (ADR 0023).
    """

    ARG = "arg"      # Resource Graph -- eventually consistent
    ARM = "arm"      # ARM REST GET  -- strongly consistent, not yet used


# --------------------------------------------------------------------------
# /v1/precheck  -- DA -> DVA, advisory
# --------------------------------------------------------------------------

class PrecheckItem(_Wire):
    """One resource DA is about to render, as DA knows it after 4a."""

    # DA's own handle. The response is correlated by this, never by list order
    # and never by name -- two units in one ticket can predict the same name,
    # which is itself a finding.
    item_id: str

    # `<app>/<Resource>[/<variant>]` -- e.g. "shared/AKS", "la/FunctionApp/android".
    # Case-sensitive and never normalised (ADR 0004).
    unit_path: str

    # The ARM type, e.g. "Microsoft.ContainerService/managedClusters". DVA maps
    # this to a KQL template from a static registry -- it is never built from
    # ticket text (rule 9).
    azure_resource_type: str

    # Predictable only after 4a, which is why 4b sits where it does.
    resource_name: str

    # Both from EffectiveContext -- never read from env.hcl at this call site
    # (ADR 0003). If the ticket overrode either, these carry the overridden
    # value, which is exactly the point of ADR 0003's worked example.
    resource_group_name: str

    name_scope: NameScope


class PrecheckRequest(_Wire):
    correlation_id: str
    # From EffectiveContext. Its provenance is asserted by DA before the call;
    # a non-platform provenance still travels, because the check must follow the
    # ticket's subscription rather than the platform default.
    subscription_id: str
    items: List[PrecheckItem] = Field(min_length=1)


class PrecheckResult(_Wire):
    item_id: str
    existence: Existence
    # The full ARM resource id when PRESENT. DA does not parse it -- it is
    # carried into the HITL 7 message so a human can go look.
    resource_id: Optional[str] = None
    detail: str = ""


class PrecheckResponse(_Wire):
    """Deliberately has no `exit_code`. See the module docstring."""

    correlation_id: str
    source: Source = Source.ARG
    # ISO-8601. How stale the ARG answer may be is the caller's to judge, so the
    # timestamp is mandatory rather than convenient.
    as_of: str
    results: List[PrecheckResult] = Field(default_factory=list)
    message: str = ""


# --------------------------------------------------------------------------
# /v1/validate  -- CI/CD -> DVA, authoritative
# --------------------------------------------------------------------------

class StateAddress(_Wire):
    """Where a unit's `.tfstate` lives, after the monorepo consolidation.

    `root.hcl`'s `remote_state` block composes the key as

        ${repo_name}/${environment}/${path_relative_to_include()}/terraform.tfstate

    Read from the backend block itself, not from a doc summary -- 03-platform-
    insights.md section 1.4 records the order but stops at the trailing slash,
    and building the key from that alone drops the filename. Two things the
    merge broke for anything designed against the per-service-repo model:

    1. `repo_name` collapsed from six values to one. Every app now resolves to
       `pss_fabric_app_iac`, so a state location derived from a service repo
       name no longer distinguishes anything.
    2. `path_relative_to_include()` is `<app>/<Resource>[/<variant>]` -- two
       segments or three. A fixed-depth parse breaks on `la/FunctionApp/android`.

    The older `<repo>/<folder>/<env>/terraform.tfstate` shape in docs/DA.md is
    stale; note that `environment` moved *ahead* of the path segment.

    The account and container are **one pair for the whole monorepo**, static
    literals rather than per-environment interpolations -- environment
    separation happens inside the key, which is why the container did not need
    to split when the six repos merged (Q12, answered 2026-09-23).

    They still carry no defaults. They are configuration, read from `root.hcl`,
    and fail loudly when absent (rule 13 / ADR 0019) -- the sample repo's
    literals are not guaranteed to be HON's.

    Note the backend pins its own `subscription_id`: state lives in a different
    subscription from the workloads it describes. Whoever reads a state blob
    needs data-plane access there, which control-plane Reader does not grant.
    See Q13.
    """

    repo_name: str
    environment: str
    unit_path: str
    storage_account_name: str
    container_name: str

    @property
    def key(self) -> str:
        """The blob key as `root.hcl` composes it, filename included."""
        return "%s/%s/%s/%s" % (self.repo_name, self.environment,
                                self.unit_path, STATE_FILENAME)


class ValidateItem(_Wire):
    item_id: str
    unit_path: str
    azure_resource_type: str
    resource_name: str
    resource_group_name: str
    # Present when the caller wants the state file inspected as well as the
    # live resource. A promotion gate normally sends it; a bare existence
    # question does not.
    state_address: Optional[StateAddress] = None


class ValidateRequest(_Wire):
    correlation_id: str
    subscription_id: str
    items: List[ValidateItem] = Field(min_length=1)


class CheckVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class ValidateResult(_Wire):
    item_id: str
    passed: bool
    existence: Existence
    resource_id: Optional[str] = None
    detail: str = ""


class ValidateResponse(_Wire):
    correlation_id: str
    # ARG for now -- see the module docstring and ADR 0023. When the ARM
    # fallback lands this becomes ARM per response, not per deployment.
    source: Source = Source.ARG
    as_of: str
    verdict: CheckVerdict
    # Non-zero halts the workflow. `CANNOT_VERIFY` fails the gate -- a gate that
    # cannot see is not a gate that passes.
    exit_code: int
    results: List[ValidateResult] = Field(default_factory=list)
    message: str = ""


# --------------------------------------------------------------------------
# Quadrant  (01-algorithm / DA.md Step 4b)
# --------------------------------------------------------------------------

class Quadrant(str, Enum):
    """Resolved by DA from 3b (code side) and 4b (Azure side). Not by DVA.

    DVA answers one question and has no idea what the repo says. Keeping the
    quadrant on DA's side is what stops the validation service from acquiring
    an opinion about deployment state.
    """

    Q1 = "Q1"   # code yes, azure yes -- already provisioned
    Q2 = "Q2"   # code yes, azure no  -- declared, never applied
    Q3 = "Q3"   # code no,  azure yes -- drift
    Q4 = "Q4"   # code no,  azure no  -- genuinely new, the only one that proceeds


class QuadrantOutcome(BaseModel):
    """Result object, per the repo convention -- a failure is returned, not raised."""

    quadrant: Optional[Quadrant] = None
    proceeds: bool = False
    halts: bool = False
    message: str = ""


def resolve_quadrant(declared_in_code: bool, existence: Existence) -> QuadrantOutcome:
    """Fold the 3b flag and the 4b answer into one of four quadrants.

    `CANNOT_VERIFY` resolves to no quadrant at all. Folding it into Q2 or Q4
    would let an unreadable subscription look like a clean greenfield, which is
    the silent failure ADR 0003 exists to prevent.
    """
    if existence is Existence.CANNOT_VERIFY:
        return QuadrantOutcome(
            halts=True,
            message="🛑 [STEP 4b] Azure existence could not be verified "
                    "(403/401). This is not 'absent' -- ADR 0003 rule 4.",
        )

    present = existence is Existence.PRESENT
    if declared_in_code and present:
        return QuadrantOutcome(
            quadrant=Quadrant.Q1,
            message="Q1 already provisioned -- declared in the repo and live in Azure.",
        )
    if declared_in_code and not present:
        return QuadrantOutcome(
            quadrant=Quadrant.Q2,
            message="Q2 declared, not applied -- escalate to DevOps.",
        )
    if not declared_in_code and present:
        return QuadrantOutcome(
            quadrant=Quadrant.Q3,
            message="Q3 DRIFT -- live in Azure with nothing in the repo. "
                    "Escalate to DevOps.",
        )
    return QuadrantOutcome(
        quadrant=Quadrant.Q4,
        proceeds=True,
        message="Q4 genuinely new -- proceeds to 4c render.",
    )
