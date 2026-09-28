"""Phase 2 acceptance tests -- the precedence engine.

The anchor is the worked example in `01-algorithm.md` section 10, run end to
end, plus the closure proof over every module: each declared variable must land
in exactly one branch.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from da.section_a_iac.contracts.module_contract import build_contract, discover_modules
from da.section_a_iac.contracts.platform_context import build_platform_context
from da.section_a_iac.contracts.reconcile import pair_modules_with_templates, slug_for_module
from da.section_a_iac.contracts.template_map import build_template_map, discover_templates
from da.section_a_iac.resolve.engine import resolve_module
from da.section_a_iac.resolve.facts import FactRejected, FactsStore
from da.section_a_iac.resolve.models import Action, Branch, FactSource, Verdict

HON = os.path.join(os.path.expanduser("~"),
                   "OneDrive - KnackForge Soft Solutions Pvt Ltd",
                   "Desktop", "HON")
MODULES_REPO = os.path.join(HON, "pss_fabric_azure_modules_iac_sample")
APP_REPO = os.path.join(HON, "pss_fabric_app_iac_sample")

pytestmark = pytest.mark.skipif(
    not os.path.isdir(MODULES_REPO),
    reason="sample repos not present; see .agent/journal.md for expected paths")

# Helper modules, never provisioned directly.
HELPERS = {"Shared/naming", "Shared/tagging", "Security/Keyvault-secrets"}


@pytest.fixture(scope="module")
def platform():
    return build_platform_context(APP_REPO)


@pytest.fixture(scope="module")
def redis():
    contract = build_contract(os.path.join(MODULES_REPO, "Databases",
                                           "Managed-Redis"),
                              "Databases/Managed-Redis")
    template = build_template_map(
        os.path.join(MODULES_REPO, "docs", "examples", "managed-redis",
                     "terragrunt.template.hcl"), "managed-redis")
    return contract, template


def _ticket_facts(contract):
    """The three facts the section 10 ticket states."""
    facts = FactsStore(contract)
    facts.append("resource_prefix", "subscription-redis", FactSource.JIRA,
                 evidence="Resource prefix: subscription-redis")
    facts.append("managed_redis_instances.primary.sku_name", "Balanced_B3",
                 FactSource.JIRA, evidence="SKU: Balanced_B3")
    facts.append("high_availability_enabled", True, FactSource.JIRA,
                 evidence="High Availability: true")
    return facts


# --------------------------------------------------------------------------
# 1. The closure proof
# --------------------------------------------------------------------------

def test_every_variable_lands_in_exactly_one_branch(platform):
    """An unclassified variable is an algorithm bug, not a value to guess."""
    modules = [m for m in discover_modules(MODULES_REPO) if m not in HELPERS]
    templates = discover_templates(MODULES_REPO)
    paired = pair_modules_with_templates(modules, templates)

    total = 0
    unclassified = {}
    for module_path in modules:
        contract = build_contract(
            os.path.join(MODULES_REPO, *module_path.split("/")), module_path)
        template_path = paired[module_path]
        template = (build_template_map(template_path, slug_for_module(module_path))
                    if template_path else None)
        proof = resolve_module(contract, template, platform)

        total += len(contract.variables)
        assert len(proof.resolutions) + len(proof.unclassified) == \
            len(contract.variables), module_path
        if proof.unclassified:
            unclassified[module_path] = proof.unclassified

    assert not unclassified, unclassified
    assert total > 700, "expected the full corpus, got %d variables" % total


def test_aks_ask_set_is_six(platform):
    """Matches the Phase 1 figure, now produced by the real engine."""
    contract = build_contract(os.path.join(MODULES_REPO, "Containers", "AKS"),
                              "Containers/AKS")
    template = build_template_map(
        os.path.join(MODULES_REPO, "docs", "examples", "aks",
                     "terragrunt.template.hcl"), "aks")
    proof = resolve_module(contract, template, platform)
    assert len(proof.ask_set) == 6, proof.ask_set


# --------------------------------------------------------------------------
# 2. The worked example, end to end (01-algorithm.md section 10)
# --------------------------------------------------------------------------

def test_round_zero_asks_then_ticket_facts_close_it(redis, platform):
    contract, template = redis

    before = resolve_module(contract, template, platform)
    assert not before.may_render
    assert "resource_prefix" in before.ask_set
    assert "managed_redis_instances" in before.ask_set

    after = resolve_module(contract, template, platform, _ticket_facts(contract))
    assert after.ask_set == [], after.ask_set
    assert after.may_render
    assert not after.unclassified


def test_the_three_disclosures(redis, platform):
    """Section 10: exactly three template literals differ from module defaults.

    Filtered to template-sourced disclosures. Values read from the ticket into a
    naming variable are also disclosed, for a different reason, and belong to
    `test_naming_values_from_the_ticket_are_flagged_for_confirmation`.
    """
    contract, template = redis
    proof = resolve_module(contract, template, platform, _ticket_facts(contract))
    disclosed = sorted(n for n, r in proof.resolutions.items()
                       if r.disclose and r.source is FactSource.TEMPLATE)
    assert disclosed == ["access_keys_authentication_enabled",
                         "eviction_policy",
                         "persistence_rdb_backup_frequency"], disclosed


def test_naming_values_from_the_ticket_are_flagged_for_confirmation(redis,
                                                                    platform):
    """A ticket value that decides the resource name goes to CONFIRM.

    Every sample ticket says "Target service repo is X", and `service` is a
    naming component, so a grounded-but-wrong value would end up in the resource
    name. The algorithm cannot tell it is wrong -- it can make sure a human sees
    it.
    """
    contract, template = redis
    assert "service" in contract.naming_inputs

    proof = resolve_module(contract, template, platform, _ticket_facts(contract))
    resolution = proof.resolutions["resource_prefix"]
    assert resolution.disclose
    assert "confirm" in resolution.disclosure.lower()


def test_hitl_answers_are_not_re_confirmed(redis, platform):
    """A value a human already typed does not need confirming again."""
    contract, template = redis
    facts = FactsStore(contract)
    facts.append("resource_prefix", "subscription-redis", FactSource.HITL,
                 evidence="hitl", round=1)
    proof = resolve_module(contract, template, platform, facts)
    assert not proof.resolutions["resource_prefix"].disclose


def test_rdb_backup_frequency_is_not_silently_dropped(redis, platform):
    """The dangerous one: module default null means no backups at all."""
    contract, template = redis
    proof = resolve_module(contract, template, platform, _ticket_facts(contract))
    resolution = proof.resolutions["persistence_rdb_backup_frequency"]
    assert resolution.action == Action.USE_DISCLOSE
    assert resolution.value == "12h"
    assert resolution.disclose


def test_t2_clears_once_the_precondition_is_satisfied(redis, platform):
    """`service` is required only until `resource_prefix` arrives."""
    contract, template = redis
    before = resolve_module(contract, template, platform)
    assert "service" in before.ask_set

    after = resolve_module(contract, template, platform, _ticket_facts(contract))
    assert "service" not in after.ask_set
    assert after.resolutions["service"].branch == Branch.OTHERWISE


# --------------------------------------------------------------------------
# 3. Branch order -- the user always wins (ADR 0002)
# --------------------------------------------------------------------------

def test_branch_one_beats_auto_injected(redis, platform):
    """shared/StorageAccount:40 hardcodes env; Branch-2-first would drop it."""
    contract, template = redis
    facts = FactsStore(contract)
    facts.append("env", "nonprod", FactSource.JIRA, evidence="env: nonprod")

    proof = resolve_module(contract, template, platform, facts)
    resolution = proof.resolutions["env"]
    assert resolution.branch == Branch.USER_FACT
    assert resolution.action == Action.LITERAL
    assert resolution.value == "nonprod"
    assert resolution.disclose, "overriding an auto-injected key must disclose"


def test_stating_location_propagates_to_region(redis, platform):
    """common.hcl:37 -- one value under two names. westus must emit both."""
    contract, template = redis
    facts = FactsStore(contract)
    facts.append("location", "westus", FactSource.JIRA, evidence="deploy to westus")

    proof = resolve_module(contract, template, platform, facts)
    assert proof.resolutions["location"].value == "westus"
    assert proof.resolutions["region"].branch == Branch.USER_FACT
    assert proof.resolutions["region"].value == "westus"


def test_user_stated_pointer_key_is_forced_literal(redis, platform):
    """Provenance that is not `platform` can never render as a POINTER."""
    contract, template = redis
    facts = FactsStore(contract)
    facts.append("resource_group_name", "rg-explicit", FactSource.JIRA,
                 evidence="RG: rg-explicit")

    resolution = resolve_module(contract, template, platform,
                                facts).resolutions["resource_group_name"]
    assert resolution.action == Action.LITERAL
    assert resolution.pointer_target == ""
    assert resolution.disclose


# --------------------------------------------------------------------------
# 4. The facts store write path (ADR 0010)
# --------------------------------------------------------------------------

def test_bad_enum_never_enters_the_store(platform):
    """Validation is a gate inside append(), not a call-site concern."""
    contract = build_contract(os.path.join(MODULES_REPO, "Containers", "AKS"),
                              "Containers/AKS")
    facts = FactsStore(contract)

    outcome = facts.append("automatic_upgrade_channel", "banana", FactSource.JIRA)
    assert outcome.verdict == Verdict.REJECT
    assert facts.get("automatic_upgrade_channel") is None
    assert len(facts) == 0

    good = facts.append("automatic_upgrade_channel", "stable", FactSource.JIRA)
    assert good.accepted
    assert facts.get("automatic_upgrade_channel").value == "stable"


def test_wrong_type_is_rejected(redis):
    contract, _ = redis
    facts = FactsStore(contract)
    outcome = facts.append("high_availability_enabled", "yes-please",
                           FactSource.JIRA)
    assert outcome.verdict == Verdict.REJECT
    assert len(facts) == 0


def test_strict_mode_raises(redis):
    contract, _ = redis
    facts = FactsStore(contract)
    with pytest.raises(FactRejected):
        facts.append("high_availability_enabled", "nope", FactSource.JIRA,
                     strict=True)


def test_platform_values_are_validated_too(redis):
    """Being platform-sourced is not a trust exemption (ADR 0010)."""
    contract, _ = redis
    facts = FactsStore(contract)
    outcome = facts.append("high_availability_enabled", "not-a-bool",
                           FactSource.PLATFORM)
    assert outcome.verdict == Verdict.REJECT


def test_azure_rungs_are_reported_skipped_not_passed(redis):
    """A rung that did not run must never look like a rung that passed."""
    from da.section_a_iac.resolve.models import Rung
    contract, _ = redis
    facts = FactsStore(contract)
    outcome = facts.append("resource_prefix", "wfi-core", FactSource.JIRA)
    assert outcome.accepted
    assert Rung.EXISTENCE in outcome.skipped
    assert Rung.UNIQUENESS in outcome.skipped


def test_append_only_latest_round_wins(redis):
    contract, _ = redis
    facts = FactsStore(contract)
    facts.append("resource_prefix", "first", FactSource.JIRA, round=0)
    facts.append("resource_prefix", "second", FactSource.HITL, round=1)
    assert facts.get("resource_prefix").value == "second"
    assert len(facts) == 2, "nothing is overwritten"


# --------------------------------------------------------------------------
# 5. Secrets (01-algorithm.md section 7)
# --------------------------------------------------------------------------

def test_boolean_flag_is_not_treated_as_a_secret(redis, platform):
    """`access_keys_authentication_enabled` contains "access_key" but is a bool."""
    contract, template = redis
    proof = resolve_module(contract, template, platform, _ticket_facts(contract))
    assert proof.resolutions["access_keys_authentication_enabled"].action == \
        Action.USE_DISCLOSE


def test_string_typed_secret_is_routed_to_secret(platform):
    from da.section_a_iac.resolve.branches import _is_secret
    contract = build_contract(os.path.join(MODULES_REPO, "Databases", "Redis"),
                              "Databases/Redis")
    candidates = [s for n, s in contract.variables.items()
                  if "connection_string" in n or "access_key" in n]
    stringy = [s for s in candidates
               if s.type_tree is not None and s.type_tree.kind.value == "string"]
    for spec in stringy:
        assert _is_secret(spec), spec.name


def test_payload_map_is_assembled_from_dotted_facts(redis, platform):
    """Section 10: "3 UNWRAP + 1" -- blueprint shape, ticket contents.

    The fact arrives as `managed_redis_instances.primary.sku_name`. Emitting one
    leaf's value as the whole variable would produce
    `managed_redis_instances = "Balanced_B3"` instead of the declared map.
    """
    contract, template = redis
    proof = resolve_module(contract, template, platform, _ticket_facts(contract))
    resolution = proof.resolutions["managed_redis_instances"]
    assert resolution.branch == Branch.USER_FACT
    assert resolution.action == Action.UNWRAP
    assert resolution.value == {"primary": {"sku_name": "Balanced_B3"}}
    assert "cache1" in resolution.blueprint, "blueprint shape retained"
