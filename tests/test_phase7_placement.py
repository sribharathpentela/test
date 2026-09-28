"""M5/M7 -- unit placement in the app monorepo.

The fixture is the real `pss_fabric_app_iac_sample` tree: 6 groups carrying a
`project.hcl`, 17 units carrying a `terragrunt.hcl`, and the module path each
unit actually declares. Every mapping below was read out of the repo, not
invented -- that is the whole claim this module rests on (ADR 0021).

Offline: the tree and the blob fetcher are both fakes, so nothing here touches
the network.
"""

import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(ROOT))

from src.da.section_a_iac.placement import (
    parse_project_name,
    parse_unit_source,
    propose_placement,
    survey,
)

# unit folder -> the module path its terragrunt.hcl declares.
UNITS = {
    "core/EventHub": "Messaging-and-notifications/Event-Hub",
    "core/FunctionApp": "Compute/Function-app",
    "genai/AIFoundry": "Foundry/AI-Foundry",
    "la/FunctionApp/android": "Compute/Function-app",
    "la/FunctionApp/cleanup": "Compute/Function-app",
    "la/FunctionApp/printer": "Compute/Function-app",
    "la/FunctionApp/voicecatalyst": "Compute/Function-app",
    "la/FunctionApp/voiceclient": "Compute/Function-app",
    "la/KeyVault": "Security/Keyvault",
    "la/StorageAccount": "Storage/storage_account",
    "shared/ACR": "Containers/ACR",
    "shared/AKS": "Containers/AKS",
    "shared/APIM": "API-Management/API-Management-service",
    "shared/Databricks": "Databricks/Azure-Databricks",
    "shared/KeyVault": "Security/Keyvault",
    "shared/StorageAccount": "Storage/storage_account",
    "vcng/EventHub": "Messaging-and-notifications/Event-Hub",
}

# `wfi` carries a project.hcl and hosts no units at all.
GROUPS = {
    "core": "Core",
    "genai": "GenAI",
    "la": "LA",
    "shared": "Shared",
    "vcng": "VCNG",
    "wfi": "WFI",
}

SOURCE_LINE = (
    'terraform {\n'
    '  source = "${include.common.locals.iac_modules_repo}//%s'
    '?ref=${include.common.locals.module_ref}"\n'
    '}\n'
)

PROJECT_HCL = 'locals {\n  project_name = "%s"\n  instance = "001"\n}\n'


def _tree():
    items = [{"type": "tree", "path": g} for g in GROUPS]
    items += [{"type": "blob", "path": "%s/project.hcl" % g} for g in GROUPS]
    items += [{"type": "blob", "path": "%s/terragrunt.hcl" % u} for u in UNITS]
    # Repo machinery that must not be read as a group or a unit.
    items += [
        {"type": "blob", "path": "common.hcl"},
        {"type": "blob", "path": "root.hcl"},
        {"type": "blob", "path": ".github/CODEOWNERS"},
        {"type": "blob", "path": "Readme.md"},
    ]
    return items


def _content(path):
    if path.endswith("/project.hcl"):
        return PROJECT_HCL % GROUPS[path.split("/")[0]]
    if path.endswith("/terragrunt.hcl"):
        folder = path[: -len("/terragrunt.hcl")]
        return SOURCE_LINE % UNITS[folder]
    return None


@pytest.fixture(scope="module")
def repo():
    return survey(_tree(), _content)


# --- parsing -------------------------------------------------------------

def test_source_takes_the_last_double_slash():
    """A source written as a full URL must not yield everything after `https:`."""
    text = ('source = "git::https://github.com/HON-IA/pss_fabric_azure_modules_iac'
            '.git//Containers/AKS?ref=v1.0.6"')
    assert parse_unit_source(text) == "Containers/AKS"


def test_source_from_the_interpolated_form():
    assert parse_unit_source(SOURCE_LINE % "Security/Keyvault") == "Security/Keyvault"


def test_unparseable_source_is_none_not_a_guess():
    assert parse_unit_source('terraform {\n  # no source here\n}') is None
    assert parse_unit_source("") is None


def test_project_name_read_from_project_hcl():
    assert parse_project_name(PROJECT_HCL % "WFI") == "WFI"
    assert parse_project_name("locals {}") is None


# --- the survey ----------------------------------------------------------

def test_survey_finds_every_group_and_unit(repo):
    assert repo.group_names == ["core", "genai", "la", "shared", "vcng", "wfi"]
    assert len(repo.units) == 17


def test_an_empty_group_is_still_a_group(repo):
    """`wfi` has a project.hcl and no units. Inferring groups from unit paths
    would hide it, and it is a legitimate placement target."""
    assert "wfi" in repo.group_names
    assert [u for u in repo.units if u.group == "wfi"] == []


def test_group_label_carries_the_project_name(repo):
    assert repo.label("wfi") == "wfi (WFI)"


def test_repo_machinery_is_not_a_group(repo):
    assert ".github" not in repo.group_names
    assert "common.hcl" not in repo.group_names


def test_variant_is_split_out(repo):
    android = [u for u in repo.units if u.path == "la/FunctionApp/android"][0]
    assert (android.group, android.unit, android.variant) == ("la", "FunctionApp", "android")
    aks = [u for u in repo.units if u.path == "shared/AKS"][0]
    assert (aks.group, aks.unit, aks.variant) == ("shared", "AKS", None)


def test_a_unit_outside_a_known_group_is_dropped():
    tree = _tree() + [{"type": "blob", "path": "orphan/Thing/terragrunt.hcl"}]

    def content(path):
        if path == "orphan/Thing/terragrunt.hcl":
            return SOURCE_LINE % "Containers/AKS"
        return _content(path)

    assert [u for u in survey(tree, content).units if u.group == "orphan"] == []


def test_an_unreadable_unit_is_skipped_not_guessed():
    tree = _tree() + [{"type": "blob", "path": "shared/Mystery/terragrunt.hcl"}]
    content = lambda p: None if "Mystery" in p else _content(p)
    assert [u for u in survey(tree, content).units if u.unit == "Mystery"] == []


# --- Q10's hard cases ----------------------------------------------------

@pytest.mark.parametrize("module_path,expected", [
    ("API-Management/API-Management-service", "APIM"),      # Q10: "not derivable"
    ("Databricks/Azure-Databricks", "Databricks"),          # Q10: "not derivable"
    ("Security/Keyvault", "KeyVault"),                      # Q10: "not derivable"
    ("Compute/Function-app", "FunctionApp"),
    ("Storage/storage_account", "StorageAccount"),
    ("Messaging-and-notifications/Event-Hub", "EventHub"),
    ("Containers/ACR", "ACR"),
    ("Foundry/AI-Foundry", "AIFoundry"),
])
def test_folder_name_is_observed_not_computed(repo, module_path, expected):
    """Q10 is right that these cannot be derived, and wrong that they cannot be
    obtained -- every one of them is declared in the repo."""
    proposal = propose_placement(module_path, repo)
    assert proposal.unit == expected
    assert proposal.unit_is_guess is False


def test_observation_beats_the_mechanical_guess(repo):
    """The guess for APIM would be 'API-Management-service'. The repo says APIM."""
    guessed = "API-Management/API-Management-service".split("/")[-1]
    assert propose_placement(
        "API-Management/API-Management-service", repo).unit != guessed


# --- proposals -----------------------------------------------------------

def test_group_is_asked_because_the_repo_does_not_record_it(repo):
    """`FunctionApp` sits in both `core/` and `la/` and nothing says why."""
    proposal = propose_placement("Compute/Function-app", repo)
    assert proposal.needs_ask
    q = proposal.questions[0]
    assert q.field == "group"
    assert q.allowed_values == ["core", "genai", "la", "shared", "vcng", "wfi"]
    assert q.labels["la"] == "la (LA)"


def test_the_question_carries_its_evidence(repo):
    proposal = propose_placement("Containers/AKS", repo)
    assert "shared/AKS declares source 'Containers/AKS'" in proposal.evidence
    assert "shared/AKS" in proposal.questions[0].why


def test_a_free_group_takes_a_bare_unit(repo):
    """`la` hosts no AKS, so the answer there is `la/AKS` with nothing left to ask."""
    proposal = propose_placement("Containers/AKS", repo, preferred_group="la")
    assert proposal.proposed_path == "la/AKS"
    assert proposal.needs_ask is False


def test_an_occupied_group_asks_for_a_variant(repo):
    proposal = propose_placement("Containers/AKS", repo, preferred_group="shared")
    assert proposal.proposed_path is None
    assert proposal.questions[0].field == "variant"
    assert proposal.questions[0].open_ended is True


def test_variant_mode_offers_its_siblings(repo):
    """`la/FunctionApp/` is a folder of units, so a new one needs a variant name
    and the existing five are the evidence for what such a name looks like."""
    proposal = propose_placement("Compute/Function-app", repo, preferred_group="la")
    q = proposal.questions[0]
    assert q.field == "variant"
    assert q.allowed_values == ["android", "cleanup", "printer",
                                "voicecatalyst", "voiceclient"]
    assert "variants" in proposal.reason


def test_a_bare_unit_in_the_way_is_named_as_a_move(repo):
    """`core/FunctionApp` is itself a unit. Nesting a variant under it would put
    a unit inside a unit, so the existing one has to move first."""
    proposal = propose_placement("Compute/Function-app", repo, preferred_group="core")
    assert proposal.questions[0].field == "variant"
    assert "moving the existing one" in proposal.reason


def test_both_answers_close_the_placement(repo):
    proposal = propose_placement("Compute/Function-app", repo,
                                 preferred_group="la", variant="voicerouter")
    assert proposal.proposed_path == "la/FunctionApp/voicerouter"
    assert proposal.needs_ask is False


def test_candidates_mark_which_groups_need_a_variant(repo):
    proposal = propose_placement("Compute/Function-app", repo)
    assert "core/FunctionApp/<variant>" in proposal.candidates
    assert "la/FunctionApp/<variant>" in proposal.candidates
    assert "wfi/FunctionApp" in proposal.candidates       # free -- no variant


# --- a module the repo has never used ------------------------------------

def test_an_unseen_module_asks_for_the_name_too(repo):
    proposal = propose_placement("Databases/Redis", repo)
    assert proposal.unit_is_guess is True
    assert [q.field for q in proposal.questions] == ["unit", "group"]
    assert proposal.questions[0].open_ended is True


def test_an_unseen_module_never_proposes_a_path(repo):
    """A guessed folder name must not reach the renderer unconfirmed."""
    proposal = propose_placement("Databases/Redis", repo)
    assert proposal.proposed_path is None


# --- ADR 0004 ------------------------------------------------------------

def test_module_paths_match_case_sensitively(repo):
    """`Security/KeyVault` is not `Security/Keyvault`. Folding them would let a
    near-miss supply evidence for the wrong module."""
    proposal = propose_placement("Security/KeyVault", repo)
    assert proposal.unit_is_guess is True


def test_the_old_guess_is_not_produced(repo):
    """The `{type}-{prefix}` guess rendered `AKS-wfi-core`, a path this repo has
    never contained."""
    proposal = propose_placement("Containers/AKS", repo, preferred_group="la")
    assert "-" not in proposal.proposed_path
    assert proposal.proposed_path.count("/") == 1
