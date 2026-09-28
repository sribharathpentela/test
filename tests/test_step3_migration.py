"""M2, M3, M4 and M8 -- the Step 3 migration items.

Assertions come from `docs/architecture/04-deltas-and-migration.md` section 3 and
from ADR 0004 (case-sensitive module paths), ADR 0006 (module_ref is read, not
chosen), ADR 0018 (an unresolvable ref is a pinning failure) and ADR 0020/0021
(sources are materialised from Git).

Offline: the GitHub client and the materialiser are both fakes, so nothing here
touches the network.
"""

import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(ROOT))

from src.da.clients.git_client import GitClient, ModulePathMatch, match_module_path
from src.da.config import GitSettings
from src.da.section_a_iac.contracts.materialise import MaterialiseError, MaterialisedRepo
from src.da.schemas.base_intake import GateAIntakeOutput
from src.da.section_a_iac.gate_b import GateBValidator
from src.da.section_a_iac.module_discovery import ModuleDiscoveryEngine

# The real pin, from pss_fabric_app_iac_sample/common.hcl:29-30.
COMMON_HCL_TEXT = '''
locals {
  context = read_terragrunt_config(find_in_parent_folders("context.hcl"))
  iac_modules_repo = "git::https://github.com/HON-IA/pss_fabric_azure_modules_iac.git"
  module_ref       = "v1.0.6"
}
'''


def intake(**over):
    base = dict(
        is_da_task=True,
        classification="cloud_service_iac",
        environment="dev",
        target_service_repo="pss_fabric_app_iac_sample",
        resource_type="AKS",
    )
    base.update(over)
    return GateAIntakeOutput(**base)


class FakeGit(GitClient):
    """A GitClient with canned refs and canned file contents."""

    SETTINGS = GitSettings(
        api_base_url="https://ghes.example/api/v3",   # deliberately not github.com
        org="HON-IA",
        default_branch="dev",                         # deliberately not "main"
        modules_repo="pss_fabric_azure_modules_iac_sample",
        modules_pat="x",
        app_repo="pss_fabric_app_iac_sample",
        app_pat="y",
    )

    def __init__(self, common=COMMON_HCL_TEXT):
        self.settings = self.SETTINGS
        self.owner = self.SETTINGS.org
        self.base_api_url = self.SETTINGS.api_base_url
        self.azure_modules_pat = self.SETTINGS.modules_pat
        self.service_repo_pat = self.SETTINGS.app_pat
        self._common = common
        self.known_refs = {"v1.0.6", "main", "dev"}

    @property
    def default_branch(self):
        return self.SETTINGS.default_branch

    def ref_exists(self, repo_name, ref, use_service_pat=False):
        return ref in self.known_refs

    def get_file_content(self, repo_name, file_path, ref=None, use_service_pat=False):
        return self._common if file_path == "common.hcl" else None


class FakeMaterialiser:
    """Builds a module tree on disk, recording what was asked for."""

    def __init__(self, tmp_path, folders, fail=False):
        self.tmp_path = tmp_path
        self.folders = folders
        self.fail = fail
        self.calls = []

    def __call__(self, repo, ref):
        self.calls.append((repo, ref))
        if self.fail:
            raise MaterialiseError("simulated fetch failure")
        root = os.path.join(str(self.tmp_path), repo, ref)
        for folder in self.folders:
            directory = os.path.join(root, *folder.split("/"))
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, "variables.tf"), "w") as handle:
                handle.write('variable "x" { type = string }\n')
        return MaterialisedRepo(repo=repo, ref=ref, root=root, from_cache=False,
                                file_count=len(self.folders), message="fake")


def engine(tmp_path, folders=("Containers/AKS",), common=COMMON_HCL_TEXT, fail=False):
    git = FakeGit(common=common)
    mat = FakeMaterialiser(tmp_path, list(folders), fail=fail)
    return ModuleDiscoveryEngine(git_client=git, materialiser=mat), git, mat


# --------------------------------------------------------------------------
# M8 -- resource_prefix is not a Gate B requirement
# --------------------------------------------------------------------------

def test_m8_gate_b_passes_without_resource_prefix():
    result = GateBValidator.validate_iac_request(intake(resource_prefix=None))
    assert result.is_valid, result.message
    assert result.missing_fields == []


def test_m8_gate_b_still_requires_the_other_three():
    for field in ["environment", "target_service_repo", "resource_type"]:
        result = GateBValidator.validate_iac_request(intake(**{field: None}))
        assert not result.is_valid, field
        assert any(field in m for m in result.missing_fields), result.missing_fields


# --------------------------------------------------------------------------
# M3 -- the matching rule is case-sensitive (ADR 0004)
# --------------------------------------------------------------------------

def test_m3_exact_case_matches():
    match = match_module_path(["Containers/AKS"], "AKS")
    assert isinstance(match, ModulePathMatch)
    assert match.path == "Containers/AKS"
    assert match.case_mismatches == []


def test_m3_punctuation_is_still_ignored():
    assert match_module_path(["Compute/Function_App"], "Function-App").path \
        == "Compute/Function_App"


@pytest.mark.parametrize("folder,requested", [
    ("Compute/Function-App", "Function-app"),
    ("Identity/Managed-Identity", "Managed-identity"),
    ("Security/Keyvault", "KeyVault"),
])
def test_m3_case_only_difference_is_a_near_miss_not_a_match(folder, requested):
    """The three real mismatches that break Linux CI must not resolve."""
    match = match_module_path([folder], requested)
    assert match.path is None
    assert match.case_mismatches == [folder]
    assert "case" in match.message.lower()


def test_m3_one_rule_serves_both_resolution_routes():
    """The API route and the materialised route must not drift (ADR 0020)."""
    from src.da.clients import git_client
    assert git_client.match_module_path is match_module_path


def test_m3_get_latest_release_tag_is_gone():
    """ADR 0006: remove it rather than leave it available to be reused."""
    assert not hasattr(GitClient, "get_latest_release_tag")


# --------------------------------------------------------------------------
# M2 -- module_ref is read from common.hcl, never chosen (ADR 0006)
# --------------------------------------------------------------------------

def test_m2_reads_the_pinned_ref_and_the_repo_url(tmp_path):
    eng, _git, _mat = engine(tmp_path)
    pin = eng.read_module_pin("pss_fabric_app_iac_sample")
    assert pin.module_ref == "v1.0.6"
    assert pin.iac_modules_repo.endswith("pss_fabric_azure_modules_iac.git")


def test_m2_module_is_materialised_at_the_pinned_ref(tmp_path):
    eng, _git, mat = engine(tmp_path)
    result = eng.discover_module_in_central_registry(intake())
    assert result.found, result.message
    assert result.module_ref == "v1.0.6"
    assert mat.calls == [("pss_fabric_azure_modules_iac_sample", "v1.0.6")], \
        "the repo must be fetched at the pin, not at a branch"
    assert os.path.isfile(os.path.join(result.module_dir, "variables.tf"))


def test_x13_source_url_comes_from_common_hcl_not_a_hardcoded_host(tmp_path):
    """The rendered unit points at common.hcl's iac_modules_repo; so must this."""
    eng, _git, _mat = engine(tmp_path)
    url = eng.discover_module_in_central_registry(intake()).git_source_url
    assert url.startswith("git::https://github.com/HON-IA/pss_fabric_azure_modules_iac.git")
    assert url.endswith("//Containers/AKS?ref=v1.0.6")


def test_m2_halts_when_common_hcl_declares_no_ref(tmp_path):
    eng, _git, mat = engine(tmp_path, common="locals {\n  foo = 1\n}\n")
    result = eng.discover_module_in_central_registry(intake())
    assert not result.found
    assert "module_ref" in result.message
    assert mat.calls == []


def test_m2_halts_when_common_hcl_is_unreachable(tmp_path):
    eng, _git, mat = engine(tmp_path, common=None)
    result = eng.discover_module_in_central_registry(intake())
    assert not result.found and "HALT" in result.message
    assert mat.calls == []


def test_m2_halts_without_a_service_repo_to_read_the_ref_from(tmp_path):
    eng, _git, mat = engine(tmp_path)
    result = eng.discover_module_in_central_registry(intake(target_service_repo=None))
    assert not result.found
    assert "target_service_repo" in result.message
    assert mat.calls == [], "nothing may be fetched before the ref is known"


def test_m2_no_fallback_ref_on_a_parse_failure(tmp_path):
    eng, _git, mat = engine(tmp_path, common="locals { this is not hcl ")
    assert not eng.discover_module_in_central_registry(intake()).found
    assert mat.calls == []


# --------------------------------------------------------------------------
# ADR 0018 -- an unresolvable ref is a pinning failure, not a catalogue gap
# --------------------------------------------------------------------------

def test_a_pin_that_does_not_resolve_is_not_a_catalogue_gap(tmp_path):
    """The mirror was tagged v1.0.0 while common.hcl pinned v1.0.6 -- found live."""
    eng, git, mat = engine(tmp_path)
    git.known_refs = {"v1.0.0", "main"}          # the pinned v1.0.6 is absent
    result = eng.discover_module_in_central_registry(intake())

    assert not result.found
    assert result.module_ref == "v1.0.6"
    assert "REF NOT FOUND" in result.message
    assert result.gap_entry is None, "a bad pin is not a missing module"
    assert mat.calls == [], "the catalogue must not be fetched at all"


def test_a_failed_fetch_halts_rather_than_guessing(tmp_path):
    eng, _git, _mat = engine(tmp_path, fail=True)
    result = eng.discover_module_in_central_registry(intake())
    assert not result.found
    assert "FETCH FAILED" in result.message
    assert result.gap_entry is None


# --------------------------------------------------------------------------
# M2 x M3 -- a case-only near-miss is reported distinctly, not as "not found"
# --------------------------------------------------------------------------

def test_case_mismatch_is_not_logged_as_an_unsupported_resource(tmp_path):
    eng, _git, _mat = engine(tmp_path, folders=["Security/Keyvault"])
    result = eng.discover_module_in_central_registry(intake(resource_type="KeyVault"))
    assert not result.found
    assert result.case_mismatches == ["Security/Keyvault"]
    assert result.gap_entry is None, "a casing bug is not a missing module"
    assert "CASE MISMATCH" in result.message


def test_a_genuinely_absent_module_does_reach_the_gap_register(tmp_path):
    eng, _git, _mat = engine(tmp_path, folders=["Databases/Managed-Redis"])
    result = eng.discover_module_in_central_registry(intake(resource_type="Cosmos-DB"))
    assert not result.found
    assert result.case_mismatches == []
    assert result.gap_entry is not None, "this one is a real catalogue gap"
    assert "MODULE NOT FOUND" in result.message
