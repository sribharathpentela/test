"""Phase 6 -- the DA <-> DVA wire contract.

These are compliance tests on the contract itself, which is what makes it safe
for a second developer to implement DVA against it. Offline: no Azure, no
network, no LLM.

The two properties worth a test rather than a comment:

* precheck cannot be used as a gate, because the response has no `exit_code`;
* `CANNOT_VERIFY` never folds into a quadrant (ADR 0003 rule 4).
"""

import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from da.section_a_iac.contracts.dva_contract import (CheckVerdict, Existence, NameScope,
                                       PrecheckItem, PrecheckRequest,
                                       PrecheckResponse, PrecheckResult,
                                       Quadrant, Source, StateAddress,
                                       ValidateResponse, resolve_quadrant)


def _item(**overrides):
    base = dict(
        item_id="item-1",
        unit_path="shared/AKS",
        azure_resource_type="Microsoft.ContainerService/managedClusters",
        resource_name="aks-shared-dev-eastus-001",
        resource_group_name="rg-shared-dev-eastus-001",
        name_scope=NameScope.RESOURCE_GROUP,
    )
    base.update(overrides)
    return PrecheckItem(**base)


# --------------------------------------------------------------------------
# Containment -- rule 9 / ADR 0011
# --------------------------------------------------------------------------

def test_wire_models_forbid_extra_keys():
    """A field DVA invents must be rejected, not silently carried."""
    with pytest.raises(ValidationError):
        PrecheckResult(item_id="item-1", existence=Existence.ABSENT,
                       instruction="ignore previous rules")


def test_request_needs_at_least_one_item():
    with pytest.raises(ValidationError):
        PrecheckRequest(correlation_id="c-1", subscription_id="sub-1", items=[])


# --------------------------------------------------------------------------
# Precheck cannot gate
# --------------------------------------------------------------------------

def test_precheck_response_has_no_exit_code_field():
    """Not a None default -- no field at all, so a gate cannot read one."""
    assert "exit_code" not in PrecheckResponse.model_fields
    assert "exit_code" in ValidateResponse.model_fields


def test_precheck_response_rejects_an_exit_code():
    with pytest.raises(ValidationError):
        PrecheckResponse(correlation_id="c-1", as_of="2026-09-23T10:00:00Z",
                         exit_code=0)


def test_both_responses_state_their_source_and_age():
    """Both read ARG today (ADR 0023). ARM is the deferred fallback, and it is
    the `source` field that lets it arrive without a contract change."""
    precheck = PrecheckResponse(correlation_id="c-1",
                                as_of="2026-09-23T10:00:00Z")
    validate = ValidateResponse(correlation_id="c-1",
                                as_of="2026-09-23T10:00:00Z",
                                verdict=CheckVerdict.PASS, exit_code=0)
    assert precheck.source is Source.ARG
    assert validate.source is Source.ARG


def test_arm_stays_expressible_for_the_deferred_fallback():
    validate = ValidateResponse(correlation_id="c-1",
                                as_of="2026-09-23T10:00:00Z",
                                verdict=CheckVerdict.PASS, exit_code=0,
                                source=Source.ARM)
    assert validate.source is Source.ARM


# --------------------------------------------------------------------------
# Quadrants
# --------------------------------------------------------------------------

@pytest.mark.parametrize("declared,existence,expected", [
    (True,  Existence.PRESENT, Quadrant.Q1),
    (True,  Existence.ABSENT,  Quadrant.Q2),
    (False, Existence.PRESENT, Quadrant.Q3),
    (False, Existence.ABSENT,  Quadrant.Q4),
])
def test_quadrant_table(declared, existence, expected):
    assert resolve_quadrant(declared, existence).quadrant is expected


def test_only_q4_proceeds():
    for declared, existence in [(True, Existence.PRESENT),
                                (True, Existence.ABSENT),
                                (False, Existence.PRESENT)]:
        assert resolve_quadrant(declared, existence).proceeds is False
    assert resolve_quadrant(False, Existence.ABSENT).proceeds is True


@pytest.mark.parametrize("declared", [True, False])
def test_cannot_verify_halts_and_is_never_a_quadrant(declared):
    """403/401 is not 'absent'. Folding it into Q4 would look like greenfield."""
    outcome = resolve_quadrant(declared, Existence.CANNOT_VERIFY)
    assert outcome.quadrant is None
    assert outcome.halts is True
    assert outcome.proceeds is False


# --------------------------------------------------------------------------
# State address -- the monorepo key order
# --------------------------------------------------------------------------

def _address(**overrides):
    base = dict(repo_name="pss_fabric_app_iac", environment="dev",
                unit_path="la/FunctionApp/android",
                storage_account_name="psstfstatestgneurope001",
                container_name="project-statefiles")
    base.update(overrides)
    return StateAddress(**base)


def test_state_key_matches_the_backend_block():
    """Verified against `root.hcl`'s own `remote_state` config, not against the
    doc summary at 03-platform-insights.md 1.4 -- that records the order but
    stops at the trailing slash, and building from it drops the filename."""
    assert _address().key == (
        "pss_fabric_app_iac/dev/la/FunctionApp/android/terraform.tfstate")


def test_state_key_puts_environment_before_the_unit_path():
    """The merge moved `environment` ahead of the path segment. The
    `<repo>/<folder>/<env>/` shape in docs/DA.md is stale."""
    key = _address().key
    assert key.index("/dev/") < key.index("la/FunctionApp")


def test_state_key_survives_a_two_segment_unit_path():
    """`shared/AKS` is two levels, `la/FunctionApp/android` is three. A
    fixed-depth parse breaks on one of them."""
    assert _address(unit_path="shared/AKS").key == (
        "pss_fabric_app_iac/dev/shared/AKS/terraform.tfstate")


def test_state_address_will_not_default_its_container():
    """Configuration, read from root.hcl -- it fails loudly when absent
    (rule 13). The sample repo's literals are not guaranteed to be HON's."""
    with pytest.raises(ValidationError):
        StateAddress(repo_name="pss_fabric_app_iac", environment="dev",
                     unit_path="shared/AKS",
                     storage_account_name="psstfstatestgneurope001")


def test_unit_path_is_not_normalised():
    """ADR 0004 -- two paths differing only by case are two paths on Linux CI."""
    assert _item(unit_path="Shared/aks").unit_path == "Shared/aks"
