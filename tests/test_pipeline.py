"""The joined pipeline: ticket in, unit out, and every halt on the way.

Offline. The intake parser, the slot filler and the Git fetch are all injected,
so nothing here calls an LLM or the network. What is *not* faked is the part
that matters: real contracts built from real module files, the real precedence
engine, the real closure proof and the real renderer.

Module files come from `tests/fixtures/` when present, and otherwise from a
Git materialisation cache populated by an earlier run. Either way the content
originates from Git (ADR 0021) -- never from a hand-maintained working copy.
"""

import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from src.da.config import GitSettings
from src.da.section_a_iac.contracts.materialise import MaterialisedRepo
from src.da.pipeline import DAPipeline, PipelineStatus, Stage
from src.da.schemas.base_intake import GateAIntakeOutput, TicketMetadata

MODULES_REPO = "pss_fabric_azure_modules_iac_sample"
APP_REPO = "pss_fabric_app_iac_sample"
REF = "v1.0.6"

CACHE = os.path.join(ROOT, ".cache", "repos")
MODULES_ROOT = os.path.join(CACHE, MODULES_REPO, REF)
APP_ROOT = os.path.join(CACHE, APP_REPO, "main")

pytestmark = pytest.mark.skipif(
    not (os.path.isdir(MODULES_ROOT) and os.path.isdir(APP_ROOT)),
    reason="no materialised repos; run scripts/run_pipeline.py once to populate "
           ".cache/ from Git")

COMMON_HCL = '''
locals {
  iac_modules_repo = "git::https://github.com/HON-IA/pss_fabric_azure_modules_iac.git"
  module_ref       = "v1.0.6"
}
'''


class FakeGit(object):
    """Answers the two questions Step 3a asks, without a network."""

    def __init__(self, known_refs=(REF, "main")):
        self.settings = GitSettings(
            api_base_url="https://ghes.example/api/v3", org="HON-IA",
            default_branch="main", modules_repo=MODULES_REPO, modules_pat="x",
            app_repo=APP_REPO, app_pat="y")
        self.known_refs = set(known_refs)

    def ref_exists(self, repo_name, ref, use_service_pat=False):
        return ref in self.known_refs

    def get_file_content(self, repo_name, file_path, ref=None, use_service_pat=False):
        return COMMON_HCL if file_path == "common.hcl" else None


def fake_materialiser(repo, ref):
    root = MODULES_ROOT if repo == MODULES_REPO else APP_ROOT
    return MaterialisedRepo(repo=repo, ref=ref, root=root, from_cache=True,
                            file_count=0, message="fixture")


class FakeParser(object):
    def __init__(self, **over):
        self.over = over

    def parse_raw_jira_ticket(self, raw):
        ticket = raw.get("jira", raw)
        base = dict(
            is_da_task=True, classification="cloud_service_iac",
            ticket_metadata=TicketMetadata(ticket_id=ticket.get("key", "TEST-1")),
            environment="qa", target_service_repo=APP_REPO,
            resource_type="AKS", resource_prefix="voicecatalyst-aks")
        base.update(self.over)
        return GateAIntakeOutput(**base)


def quiet_completer(_system, _user):
    """Quotes nothing, so every blocking slot goes to HITL."""
    return json.dumps({"values": {}, "evidence": {}})


TICKET = {"key": "DEVOPS-888", "summary": "Provision AKS cluster",
          "description": "Environment: qa. Target service repo is %s." % APP_REPO}


def pipeline(**over):
    return DAPipeline(git_client=FakeGit(), materialiser=fake_materialiser,
                      completer=quiet_completer, intake_parser=FakeParser(**over))


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------

def test_reaches_hitl_with_the_stages_recorded_in_order():
    result = pipeline().start(TICKET)
    assert result.status is PipelineStatus.NEEDS_HITL
    assert [s.stage for s in result.stages] == [
        Stage.INTAKE, Stage.GATE_A, Stage.CLASSIFY, Stage.GATE_B,
        Stage.DISCOVERY, Stage.PLATFORM, Stage.CONTRACT, Stage.RESOLVE]
    assert all(s.ok for s in result.stages)
    assert result.module_path == "Containers/AKS"
    assert result.module_ref == REF


def test_aks_asks_six_questions_not_thirty_six():
    """03-platform-insights 3.1 -- the template marks what a human must supply."""
    result = pipeline().start(TICKET)
    assert len(result.hitl_request.questions) == 6, \
        sorted(q.path for q in result.hitl_request.questions)


def test_answering_closes_the_loop_and_renders():
    pipe = pipeline()
    result = pipe.start(TICKET)
    result = pipe.submit({q.path: "answered" for q in result.hitl_request.questions})

    # Closure is not the end any more: placement asks which group the unit
    # belongs to, because the repo records the folder *name* and not the
    # ownership (ADR 0026). `AKS` already sits under `shared/`, so every group
    # is offered and none is assumed.
    assert result.status is PipelineStatus.NEEDS_HITL
    assert result.placement_question.field == "group"
    # `la` hosts no AKS, so naming it settles the path outright. Answering
    # `shared` would ask again for a variant, because `shared/AKS` is itself a
    # unit and a second one cannot share the folder.
    result = pipe.submit({"group": "la"})

    assert result.status is PipelineStatus.COMPLETE
    assert result.proposed_unit_path == "la/AKS"
    assert result.placement_is_provisional is False
    assert result.unit_hcl and result.pr_body
    assert result.rendered_count > 0
    assert "Containers/AKS" in result.unit_hcl
    # The ref is pointed at, never baked in (ADR 0006).
    assert "${include.common.locals.module_ref}" in result.unit_hcl
    assert REF not in result.unit_hcl


def test_placement_is_flagged_provisional_until_m5_m7():
    """DA must not present a folder it cannot yet derive as decided."""
    pipe = pipeline()
    result = pipe.start(TICKET)
    result = pipe.submit({q.path: "answered" for q in result.hitl_request.questions})
    assert result.placement_is_provisional is True


# --------------------------------------------------------------------------
# Halts -- each one stops at its own stage and says why
# --------------------------------------------------------------------------

def test_gate_a_halts_a_non_da_ticket():
    result = pipeline(is_da_task=False,
                      rejection_reason="password reset").start(TICKET)
    assert result.status is PipelineStatus.HALTED
    assert result.halted_at is Stage.GATE_A
    assert "password reset" in result.message


def test_gate_b_halts_when_a_baseline_field_is_missing():
    result = pipeline(environment=None).start(TICKET)
    assert result.status is PipelineStatus.HALTED
    assert result.halted_at is Stage.GATE_B
    assert "environment" in result.message


def test_an_unimplemented_classification_halts_rather_than_improvising():
    result = pipeline(classification="microservice_onboarding").start(TICKET)
    assert result.status is PipelineStatus.HALTED
    assert result.halted_at is Stage.CLASSIFY


def test_a_case_mismatch_halts_at_discovery():
    result = pipeline(resource_type="KeyVault").start(TICKET)
    assert result.status is PipelineStatus.HALTED
    assert result.halted_at is Stage.DISCOVERY
    assert "CASE MISMATCH" in result.message


def test_an_unresolvable_pin_halts_before_the_catalogue(monkeypatch):
    """ADR 0018 -- and it must not be reported as a missing module."""
    pipe = DAPipeline(git_client=FakeGit(known_refs=("v1.0.0",)),
                      materialiser=fake_materialiser, completer=quiet_completer,
                      intake_parser=FakeParser())
    result = pipe.start(TICKET)
    assert result.status is PipelineStatus.HALTED
    assert result.halted_at is Stage.DISCOVERY
    assert "REF NOT FOUND" in result.message
    assert "MODULE NOT FOUND" not in result.message


def test_submit_before_start_is_refused_not_guessed():
    result = DAPipeline(git_client=FakeGit(), materialiser=fake_materialiser,
                        completer=quiet_completer,
                        intake_parser=FakeParser()).submit({"x": "y"})
    assert result.status is PipelineStatus.HALTED
    assert result.halted_at is Stage.RESOLVE


# --------------------------------------------------------------------------
# Rule 12 -- render only after the closure proof
# --------------------------------------------------------------------------

def test_nothing_is_rendered_while_questions_remain():
    result = pipeline().start(TICKET)
    assert result.status is PipelineStatus.NEEDS_HITL
    assert result.unit_hcl is None and result.pr_body is None
    assert result.rendered_count == 0
