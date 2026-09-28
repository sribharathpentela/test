"""Phase 1 acceptance tests.

The assertions come from `docs/architecture/04-deltas-and-migration.md` section 4
-- pre-written from the validation runs, not invented here. Offline: no LLM, no
GitHub, no Azure.

Sample repo paths come from `.agent/journal.md`. Use `Desktop/HON/`, never
`Desktop/DA/` -- the two have diverged and the DA copies produce false reports.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from da.section_a_iac.contracts.models import EntryState, ReconcileStatus, RequiredTier
from da.section_a_iac.contracts.module_contract import build_contract, discover_modules
from da.section_a_iac.contracts.platform_context import build_platform_context
from da.section_a_iac.contracts.reconcile import (pair_modules_with_templates, reconcile,
                                    slug_for_module)
from da.section_a_iac.contracts.template_map import build_template_map, discover_templates

HON = os.path.join(os.path.expanduser("~"),
                   "OneDrive - KnackForge Soft Solutions Pvt Ltd",
                   "Desktop", "HON")
MODULES_REPO = os.path.join(HON, "pss_fabric_azure_modules_iac_sample")
APP_REPO = os.path.join(HON, "pss_fabric_app_iac_sample")

pytestmark = pytest.mark.skipif(
    not os.path.isdir(MODULES_REPO),
    reason="sample repos not present; see .agent/journal.md for expected paths")

# Two files arrived truncated mid-literal and were repaired on 2026-09-18:
#   Storage/storage_account/variables.tf:257,283 -- description cut at 128 chars
#   Databases/Redis/variables.tf                 -- ended inside a heredoc
# Both now parse, and both previously-unverifiable counts reproduce.
# Empty, so any future parse failure is an outright failure.
KNOWN_UNPARSEABLE = set()


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def module_paths():
    return discover_modules(MODULES_REPO)


@pytest.fixture(scope="module")
def templates():
    return discover_templates(MODULES_REPO)


@pytest.fixture(scope="module")
def contracts(module_paths):
    built = {}
    for path in module_paths:
        try:
            built[path] = build_contract(os.path.join(MODULES_REPO, *path.split("/")),
                                         path)
        except Exception as exc:                       # noqa: BLE001
            built[path] = exc
    return built


def _contract(contracts, path):
    value = contracts[path]
    if isinstance(value, Exception):
        pytest.skip("%s does not parse: %s" % (path, str(value)[:80]))
    return value


# --------------------------------------------------------------------------
# 1. Contract builds for all modules
# --------------------------------------------------------------------------

def test_contract_builds_for_every_parseable_module(contracts):
    broke = {p: v for p, v in contracts.items() if isinstance(v, Exception)}
    assert set(broke) <= KNOWN_UNPARSEABLE, (
        "unexpected build failures: %s" % sorted(set(broke) - KNOWN_UNPARSEABLE))


def test_every_module_parses(contracts):
    """No exceptions, for all 30 module directories."""
    broke = {p: str(v)[:100] for p, v in contracts.items()
             if isinstance(v, Exception)}
    assert not broke, broke


# --------------------------------------------------------------------------
# 2. The heredoc bug -- has_default must survive a description heredoc
# --------------------------------------------------------------------------

def test_managed_redis_instances_has_default(contracts):
    contract = _contract(contracts, "Databases/Managed-Redis")
    spec = contract.variables["managed_redis_instances"]
    assert spec.has_default is True
    assert spec.default == {}


def test_postgresql_flexible_servers_has_default(contracts):
    contract = _contract(contracts, "Databases/PostgreSQL-Flexible-Server")
    candidates = [n for n in contract.variables if "flexible_server" in n]
    assert candidates, "no *flexible_server* variable found"
    for name in candidates:
        assert contract.variables[name].has_default is True


# --------------------------------------------------------------------------
# 3. Required counts
# --------------------------------------------------------------------------

@pytest.mark.parametrize("module_path,expected", [
    ("Containers/AKS", 22),
    ("Storage/storage_account", 13),
    ("Databases/Managed-Redis", 9),
])
def test_required_counts(contracts, module_path, expected):
    """The documented counts are the T1 set -- declared with no default.

    04-deltas section 4 labels these "required counts", but both reproducible
    numbers (AKS 22, Managed-Redis 9) are exactly the count of variables with no
    default. T2/T3/T4 are additional and module-specific, so asserting the
    all-tiers total against these numbers would be asserting the wrong thing.
    `contract.required` stays all-tiers because Branch 5 needs T1-T4.
    """
    contract = _contract(contracts, module_path)
    t1 = [n for n, v in contract.variables.items()
          if RequiredTier.T1 in v.tiers]
    assert len(t1) == expected, (
        "%s: expected %d T1, got %d -> %s"
        % (module_path, expected, len(t1), sorted(t1)))


def test_tiers_beyond_t1_are_detected(contracts):
    """The tier framework is not decorative -- T2 and T3 must actually fire."""
    contract = _contract(contracts, "Databases/Managed-Redis")
    assert RequiredTier.T3 in contract.variables["managed_redis_instances"].tiers
    assert RequiredTier.T2 in contract.variables["resource_prefix"].tiers, (
        "resource_prefix defaults to '' and a precondition rejects that")


def test_mutual_exclusion_preconditions_do_not_create_requirements(contracts):
    """Two of Managed-Redis's three preconditions constrain combinations.

    They pass on the declared defaults, so they demand nothing and must not
    inflate the required set.
    """
    contract = _contract(contracts, "Databases/Managed-Redis")
    for name in ("persistence_aof_backup_frequency",
                 "persistence_rdb_backup_frequency",
                 "geo_replication_group_name"):
        if name in contract.variables:
            assert RequiredTier.T2 not in contract.variables[name].tiers, name


# --------------------------------------------------------------------------
# 4. The primary payload map
# --------------------------------------------------------------------------

def test_managed_redis_primary_payload_map(contracts):
    contract = _contract(contracts, "Databases/Managed-Redis")
    assert contract.primary_payload_map == "managed_redis_instances"


def test_storage_account_has_no_primary_payload_map(contracts):
    contract = _contract(contracts, "Storage/storage_account")
    assert contract.primary_payload_map is None


# --------------------------------------------------------------------------
# 5. Blueprint detection
# --------------------------------------------------------------------------

def test_managed_redis_blueprint_is_unwrapped(templates):
    template = build_template_map(templates["managed-redis"], "managed-redis")
    entry = template.entries["managed_redis_instances"]
    assert entry.state == EntryState.BOTH, "commented AND live -> blueprint"
    assert entry.blueprint_lines, "blueprint body not captured"
    assert "cache1" in entry.blueprint_text


def test_commented_only_keys_are_not_blueprints(templates):
    template = build_template_map(templates["managed-redis"], "managed-redis")
    for key in ("persistence_aof_backup_frequency", "redis_modules", "tags"):
        assert template.entries[key].state == EntryState.COMMENTED


def test_nested_blueprint_lines_are_not_template_keys(templates):
    """`sku_name` sits inside the commented blueprint; it is not a top-level key."""
    template = build_template_map(templates["managed-redis"], "managed-redis")
    assert "sku_name" not in template.entries
    aks = build_template_map(templates["aks"], "aks")
    assert "nodepool_vnet" not in aks.entries


def test_every_template_parses(templates):
    assert len(templates) == 27
    for slug, path in templates.items():
        result = build_template_map(path, slug)
        assert not result.message.startswith("hcl2 pass failed"), result.message


# --------------------------------------------------------------------------
# 6. Reconciliation
# --------------------------------------------------------------------------

def _reconcile_all(contracts, module_paths, templates):
    paired = pair_modules_with_templates(module_paths, templates)
    results = {}
    for path, template_path in paired.items():
        value = contracts[path]
        if isinstance(value, Exception):
            results[path] = ReconcileStatus.HALT      # unparseable is a halt
            continue
        template = (build_template_map(template_path, slug_for_module(path))
                    if template_path else None)
        results[path] = reconcile(value, template).status
    return results


# Helper modules, never provisioned directly, and Keyvault-secrets which has no
# template at all (Q4). None of the three is one of the 27.
HELPERS = {"Shared/naming", "Shared/tagging", "Security/Keyvault-secrets"}


def test_halt_set_is_the_two_documented_modules(contracts, module_paths,
                                                templates):
    """03-platform-insights 3.5: 25 of 27 reconcile clean; two do not.

    """
    results = _reconcile_all(contracts, module_paths, templates)
    halted = {p for p, s in results.items()
              if s == ReconcileStatus.HALT and p not in HELPERS}
    expected = {"Databases/Redis", "Backup/Recovery-Services-Vault"}
    assert halted == expected, "halt set drifted: %s" % sorted(halted)


def test_recovery_services_vault_halts_on_two_undeclared_template_keys(
        contracts, templates):
    """The documented "2 orphan variables" -- counted in the other direction.

    The template sets `pe_net_name` / `pe_net_rg`; the module declares
    `pe_vnet_name` / `pe_vnet_rg` and uses them at data.tf:32. The template is
    missing a `v`, so these are template keys with no declaration rather than
    declared variables nobody references.
    """
    from da.section_a_iac.contracts.reconcile import CODE_UNDECLARED_TEMPLATE_KEY

    contract = _contract(contracts, "Backup/Recovery-Services-Vault")
    template = build_template_map(templates["recovery-service-vault"],
                                  "recovery-service-vault")
    result = reconcile(contract, template)
    undeclared = sorted(f.key for f in result.findings
                        if f.code == CODE_UNDECLARED_TEMPLATE_KEY)
    assert undeclared == ["pe_net_name", "pe_net_rg"], undeclared
    assert result.status == ReconcileStatus.HALT


def test_no_casing_mismatches_remain(contracts, module_paths, templates):
    """Every template `source` resolves case-exactly. This is a drift guard.

    History, because the inversion matters. This test once asserted that three
    mismatches *existed* -- `Compute/Function-App`, `Identity/Managed-Identity`
    and `Security/KeyVault` -- and defect D2 recorded them as Honeywell bugs that
    would fail on Linux CI.

    They were not Honeywell bugs. They were corruption introduced when the repos
    were extracted from VDI, confirmed against HON on 2026-09-21 and corrected at
    source. HON is internally consistent.

    What survives is the mechanism: case-sensitive matching is what *found* three
    silently corrupted paths in a mirror everyone believed was faithful. Matching
    case-insensitively would have resolved all three and generated units whose
    `source` was wrong, failing later in CI with no visible connection to DA.

    So the assertion is inverted rather than deleted: zero mismatches, and any
    future non-zero means the mirror has drifted from HON again.
    """
    from da.section_a_iac.contracts.reconcile import CODE_PATH_CASE_MISMATCH

    paired = pair_modules_with_templates(module_paths, templates)
    flagged = {}
    for path, template_path in paired.items():
        value = contracts[path]
        if isinstance(value, Exception) or not template_path:
            continue
        template = build_template_map(template_path, slug_for_module(path))
        for finding in reconcile(value, template).findings:
            if finding.code == CODE_PATH_CASE_MISMATCH:
                flagged[path] = template.declared_module_path

    assert flagged == {}, (
        "the mirror has drifted from HON again -- a template `source` no longer "
        "matches its directory: %s" % flagged)


def test_redis_halts_for_the_documented_reason(contracts):
    """03-platform-insights 3.5: 11 variables used but never declared."""
    contract = _contract(contracts, "Databases/Redis")
    assert len(contract.referenced_undeclared) == 11,         contract.referenced_undeclared


def test_case_only_mismatch_is_its_own_finding(contracts, templates):
    """ADR 0004: a case-only difference must not fold into the generic warning."""
    from da.section_a_iac.contracts.models import TemplateMap
    from da.section_a_iac.contracts.reconcile import CODE_PATH_CASE_MISMATCH

    # Synthetic on purpose: the real repos are now clean, and a detector must be
    # testable without depending on a defect existing somewhere.
    contract = _contract(contracts, "Compute/Function-app")
    faked = TemplateMap(module_slug="function-app", template_path="x",
                        declared_module_path="Compute/Function-App")
    result = reconcile(contract, faked)
    codes = [f.code for f in result.findings]
    assert CODE_PATH_CASE_MISMATCH in codes


# --------------------------------------------------------------------------
# 7. Platform context
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def platform():
    return build_platform_context(APP_REPO)


def test_auto_injected_is_eight_keys_including_tags(platform):
    assert len(platform.auto_injected) == 8, sorted(platform.auto_injected)
    assert "tags" in platform.auto_injected
    assert set(platform.auto_injected) == {
        "env", "region", "instance", "app", "owner",
        "deployment_method", "provisioned_time", "tags"}


def test_pointer_catalogue_is_seven_locals(platform):
    assert len(platform.pointer_locals) == 7, platform.pointer_locals
    assert set(platform.pointer_locals) == {
        "subscription_id", "resource_group_name", "location", "vnet_name",
        "vnet_resource_group_name", "pve_subnet_name", "svc_subnet_name"}


def test_exactly_one_alias_pair(platform):
    assert platform.alias_pairs == [["location", "region"]]


def test_module_ref_is_read_not_chosen(platform):
    """ADR 0006 -- pinned in common.hcl; DA verifies, it never picks a tag."""
    assert platform.module_ref == "v1.0.6"


# --------------------------------------------------------------------------
# 8. Invisible variables -- declared but absent from the template
# --------------------------------------------------------------------------

@pytest.mark.parametrize("module_path,slug,expected", [
    ("Storage/storage_account", "storage-account", 13),
    ("Containers/AKS", "aks", 1),
    ("Containers/ACR", "acr", 0),
])
def test_invisible_variable_counts(contracts, templates, platform, module_path,
                                   slug, expected):
    """Variables the unit can set that the template never mentions.

    Net of the auto-injected keys: common.hcl supplies those to every unit, so
    a variable the template omits *because common.hcl covers it* is handled, not
    invisible. ACR's raw difference is 5 and all 5 are auto-injected -- the
    documented count of 0 is what pins the definition.

    This is why the loop runs over variables.tf and not the template (ADR 0008).
    """
    contract = _contract(contracts, module_path)
    template = build_template_map(templates[slug], slug)
    invisible = sorted(set(contract.variables)
                       - set(template.entries)
                       - set(platform.auto_injected))
    assert len(invisible) == expected, (
        "%s: expected %d invisible, got %d -> %s"
        % (module_path, expected, len(invisible), invisible))


# --------------------------------------------------------------------------
# 9. The AKS ASK set
# --------------------------------------------------------------------------

def test_aks_ask_set_is_six(contracts, templates, platform):
    """Required, not auto-injected, not answered by the template -> ASK."""
    contract = _contract(contracts, "Containers/AKS")
    template = build_template_map(templates["aks"], "aks")
    auto = set(platform.auto_injected)
    pointers = set(platform.pointer_locals)

    ask = []
    for name, spec in contract.required.items():
        if name in auto or name in pointers:
            continue
        entry = template.entries.get(name)
        if entry is None:
            ask.append(name)
        elif entry.kind.value == "update_value":
            ask.append(name)
    assert len(ask) == 6, "expected 6, got %d -> %s" % (len(ask), sorted(ask))
