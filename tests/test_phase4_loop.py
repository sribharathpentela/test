"""Phase 4 acceptance tests -- the Step 4a resolution loop.

The loop is a state machine: it returns a question and waits to be called
again, rather than reading stdin. Every round recomputes from the facts store.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from da.section_a_iac.contracts.module_contract import build_contract
from da.section_a_iac.contracts.platform_context import build_platform_context
from da.section_a_iac.contracts.template_map import build_template_map
from da.section_a_iac.resolve.facts import FactsStore
from da.section_a_iac.resolve.loop import Step4A, Step4AStatus
from da.section_a_iac.resolve.models import FactSource

HON = os.path.join(os.path.expanduser("~"),
                   "OneDrive - KnackForge Soft Solutions Pvt Ltd",
                   "Desktop", "HON")
MODULES_REPO = os.path.join(HON, "pss_fabric_azure_modules_iac_sample")
APP_REPO = os.path.join(HON, "pss_fabric_app_iac_sample")

pytestmark = pytest.mark.skipif(
    not os.path.isdir(MODULES_REPO),
    reason="sample repos not present; see .agent/journal.md for expected paths")


def _load(category, name, slug):
    platform = build_platform_context(APP_REPO)
    contract = build_contract(os.path.join(MODULES_REPO, category, name),
                              "%s/%s" % (category, name))
    template = build_template_map(
        os.path.join(MODULES_REPO, "docs", "examples", slug,
                     "terragrunt.template.hcl"), slug)
    return contract, template, platform


@pytest.fixture(scope="module")
def redis():
    return _load("Databases", "Managed-Redis", "managed-redis")


@pytest.fixture(scope="module")
def storage():
    return _load("Storage", "storage_account", "storage-account")


# --------------------------------------------------------------------------
# The state machine
# --------------------------------------------------------------------------

def test_empty_ticket_asks_rather_than_guesses(redis):
    contract, template, platform = redis
    result = Step4A(contract, template, platform).advance()

    assert result.status is Step4AStatus.NEEDS_HITL
    assert result.hitl_request is not None
    assert result.hitl_request.questions
    assert not result.may_render


def test_answering_closes_the_loop(redis):
    contract, template, platform = redis
    loop = Step4A(contract, template, platform)

    first = loop.advance()
    answers = {}
    for question in first.hitl_request.questions:
        answers[question.path] = ("Balanced_B3" if "sku" in question.path
                                  else "subscription-redis")

    second = loop.submit(answers)
    assert second.status is Step4AStatus.RENDER_READY
    assert second.may_render
    assert second.proof.ask_set == []
    assert not second.proof.unclassified


def test_questions_carry_the_fact_path_not_just_the_name(redis):
    """A payload-map answer has to know which collection it belongs to."""
    contract, template, platform = redis
    request = Step4A(contract, template, platform).advance().hitl_request
    by_path = {q.path: q for q in request.questions}
    assert "managed_redis_instances.primary.sku_name" in by_path
    question = by_path["managed_redis_instances.primary.sku_name"]
    assert question.variable == "managed_redis_instances"
    assert "managed_redis_instances" in question.prompt


def test_questions_explain_why_they_are_asked(redis):
    contract, template, platform = redis
    request = Step4A(contract, template, platform).advance().hitl_request
    for question in request.questions:
        assert question.why, question.path


def test_confirmations_are_offered_but_do_not_block(redis):
    """CONFIRM values ride along with the questions without gating closure."""
    contract, template, platform = redis
    loop = Step4A(contract, template, platform)
    request = loop.advance().hitl_request

    assert request.confirmations, "template divergences should be offered"
    confirmed = {c["variable"] for c in request.confirmations}
    assert "persistence_rdb_backup_frequency" in confirmed


# --------------------------------------------------------------------------
# The retry budget
# --------------------------------------------------------------------------

def test_exhausting_the_budget_halts_instead_of_inventing(storage):
    contract, template, platform = storage
    loop = Step4A(contract, template, platform, max_rounds=1)

    loop.advance()
    result = loop.submit({})           # a round that answers nothing

    assert result.status is Step4AStatus.HALT
    assert "escalate" in result.message.lower()
    assert not result.may_render


def test_partial_answers_carry_over_between_rounds(storage):
    """The store is append-only, so a later round keeps what an earlier one got."""
    contract, template, platform = storage
    loop = Step4A(contract, template, platform, max_rounds=3)

    first = loop.advance()
    paths = [q.path for q in first.hitl_request.questions]
    assert len(paths) >= 2

    loop.submit({paths[0]: "answered-first"})
    assert loop.facts.get(paths[0]).value == "answered-first"

    second = loop.advance()
    still_asked = {q.path for q in (second.hitl_request.questions
                                    if second.hitl_request else [])}
    assert paths[0] not in still_asked


# --------------------------------------------------------------------------
# Validation still applies to human answers
# --------------------------------------------------------------------------

def test_human_answers_go_through_the_ladder():
    """A human is authoritative about intent, not about what the module allows."""
    contract, template, platform = _load("Containers", "AKS", "aks")
    loop = Step4A(contract, template, platform)
    loop.advance()

    result = loop.submit({"automatic_upgrade_channel": "banana"})
    assert loop.facts.get("automatic_upgrade_channel") is None
    assert "rejected" in result.message.lower()


def test_history_records_the_tier_movement(redis):
    contract, template, platform = redis
    loop = Step4A(contract, template, platform)
    first = loop.advance()
    loop.submit({q.path: "x" for q in first.hitl_request.questions})

    assert len(loop.history) >= 2
    assert loop.history[0]["tiers"]["ASK"] > loop.history[-1]["tiers"]["ASK"]
    assert loop.history[-1]["facts"] > loop.history[0]["facts"]
