"""Section C driven through `DAPipeline`, the way Section A already is.

The agent now has one entry point and one envelope. A caller reads `status`
and `classification`; it never needs to know which section ran, and neither
section can be reached except through the router.

The inversion these tests rest on: Section C used to block on `input()`. It now
replays the whole run each round against an append-only `AnswerStore` and stops
at the first unanswered question -- which is exactly what `Step4A` does, for
the reason [ADR 0010](../docs/decisions/0010-validation-on-facts-store-write-path.md)
gives.

Offline: the intake parser and the engine runner are both injected, so nothing
here reaches Jira, GitHub or Groq.
"""

import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(ROOT))

from src.da.pipeline import DAPipeline, PipelineStatus, Stage
from src.da.schemas.base_intake import GateAIntakeOutput
from src.da.section_c_cfg.flow import CfgStatus, ConfigChangeFlow
from src.da.section_c_cfg.questions import (AnswerStore, CfgQuestion,
                                            current_asker)


TICKET = {"jira": {"key": "DEVOPS-901",
                   "summary": "Raise CPU request for cf-app-registration",
                   "description": "dev environment, 300m"}}


class FakeParser(object):
    """Stands in for the Groq-backed Jira parser."""

    def __init__(self, **over):
        base = dict(
            is_da_task=True,
            classification="config_changes",
            environment="dev",
            target_service_repo="cf-app-registration",
            resource_type="CPU request/limit change",
            extracted_inputs={},
            ticket_metadata={"ticket_id": "DEVOPS-901"},
        )
        base.update(over)
        self.output = GateAIntakeOutput(**base)

    def parse_raw_jira_ticket(self, raw):
        return self.output


def runner_asking(*fields):
    """An engine run that needs `fields` answered, then reports a simulated PR."""
    def run(request):
        got = {}
        for field in fields:
            got[field] = current_asker().ask(CfgQuestion.make(
                field, "Value for %s?" % field, allowed_values=[], open_ended=True))
        return {
            "validation": {"status": "VALID", "matched_change": request.get("configuration_change")},
            "mutation": {"status": "COMPLETED", "files": []},
            "github": {"status": "SIMULATED", "results": [{
                "status": "SIMULATED",
                "message": "Raised PR with these details — In development Phase",
                "plan": {"branch": "feature/config-cf-app-registration-cpu",
                         "base_branch": "main",
                         "files": ["apps/cf-app-registration/base/deployment-v1.yaml"],
                         "commit_messages": ["config: CPU request/limit change"],
                         "pr_title": "CPU request/limit change: cf-app-registration (dev)",
                         "pr_body": "## CPU request/limit change"},
            }]},
            "answers": got,
        }
    return run


def pipe(runner, **intake_over):
    return DAPipeline(intake_parser=FakeParser(**intake_over), cfg_runner=runner)


# --- the router now reaches Section C ------------------------------------

def test_config_changes_is_no_longer_rejected():
    """It used to halt at CLASSIFY with "not built yet"."""
    result = pipe(runner_asking()).start(TICKET)
    assert result.status is PipelineStatus.COMPLETE
    assert result.classification == "config_changes"


def test_the_config_change_stage_is_recorded():
    result = pipe(runner_asking()).start(TICKET)
    stages = [s.stage for s in result.stages]
    assert Stage.CLASSIFY in stages
    assert Stage.CONFIG_CHANGE in stages


def test_gate_b_is_skipped_for_config_changes():
    """Gate B validates an *IaC* request -- module, prefix, region. Running it
    on a config change would reject every one of them."""
    result = pipe(runner_asking()).start(TICKET)
    assert Stage.GATE_B not in [s.stage for s in result.stages]


def test_an_unimplemented_classification_still_halts():
    result = pipe(runner_asking(),
                  classification="microservice_onboarding").start(TICKET)
    assert result.status is PipelineStatus.HALTED
    assert result.halted_at is Stage.CLASSIFY
    assert "microservice_onboarding" in result.message


def test_a_non_da_ticket_still_halts_before_any_section():
    result = pipe(runner_asking(), is_da_task=False,
                  rejection_reason="Not deployment work.").start(TICKET)
    assert result.status is PipelineStatus.HALTED
    assert result.halted_at is Stage.GATE_A


# --- the HITL round trip -------------------------------------------------

def test_it_returns_a_question_instead_of_blocking():
    """The whole point of the inversion. Before this, the call would have sat
    on `input()` waiting for a terminal that a supervisor does not have."""
    result = pipe(runner_asking("target_value")).start(TICKET)
    assert result.status is PipelineStatus.NEEDS_HITL
    assert result.cfg_question.field == "target_value"
    assert result.cfg_round == 1


def test_answering_resumes_the_run():
    pipeline = pipe(runner_asking("target_value"))
    result = pipeline.start(TICKET)
    result = pipeline.submit({result.cfg_question.key: "300m"})
    assert result.status is PipelineStatus.COMPLETE
    assert result.cfg_outcome["answers"] == {"target_value": "300m"}


def test_several_questions_are_asked_one_round_at_a_time():
    """Section C discovers what it needs as it goes, so unlike Section A's slot
    schema the set cannot be computed up front."""
    pipeline = pipe(runner_asking("request_or_limit", "target_value"))
    result = pipeline.start(TICKET)
    asked = []
    while result.status is PipelineStatus.NEEDS_HITL:
        asked.append(result.cfg_question.field)
        result = pipeline.submit({result.cfg_question.key: "x"})
    assert asked == ["request_or_limit", "target_value"]
    assert result.status is PipelineStatus.COMPLETE


def test_an_earlier_answer_survives_the_replay():
    """Each round re-runs from the start. If the store did not hold, round two
    would ask round one's question again."""
    pipeline = pipe(runner_asking("request_or_limit", "target_value"))
    first = pipeline.start(TICKET)
    second = pipeline.submit({first.cfg_question.key: "Request"})
    final = pipeline.submit({second.cfg_question.key: "300m"})
    assert final.cfg_outcome["answers"] == {"request_or_limit": "Request",
                                            "target_value": "300m"}


# --- the envelope --------------------------------------------------------

def test_the_simulated_pr_plan_is_reachable_from_the_envelope():
    pipeline = pipe(runner_asking())
    result = pipeline.start(TICKET)
    assert result.pr_plan["branch"] == "feature/config-cf-app-registration-cpu"
    assert result.pr_plan["pr_title"].startswith("CPU request/limit change")


def test_section_a_fields_stay_empty_for_a_config_change():
    """One envelope, but a caller must still be able to tell what ran."""
    result = pipe(runner_asking()).start(TICKET)
    assert result.unit_hcl is None
    assert result.module_path is None
    assert result.hitl_request is None


def test_a_config_change_has_no_pr_plan_until_it_completes():
    result = pipe(runner_asking("target_value")).start(TICKET)
    assert result.pr_plan is None


# --- the request built from the ticket -----------------------------------

def test_the_ticket_fills_what_it_can():
    pipeline = pipe(runner_asking())
    pipeline.start(TICKET)
    assert pipeline.cfg.request["service"] == "cf-app-registration"
    assert pipeline.cfg.request["environment"] == "dev"
    assert pipeline.cfg.request["configuration_change"] == "CPU request/limit change"


def test_extracted_inputs_are_not_overwritten_by_the_coarser_fields():
    """`extracted_inputs` came from reading the ticket text; `resource_type` is
    Gate A's coarse label. The specific one must win."""
    pipeline = pipe(runner_asking(),
                    extracted_inputs={"configuration_change": "Replica count change"})
    pipeline.start(TICKET)
    assert pipeline.cfg.request["configuration_change"] == "Replica count change"


def test_a_field_the_ticket_lacks_becomes_a_question_not_a_default():
    pipeline = pipe(runner_asking(), environment=None)
    pipeline.start(TICKET)
    assert "environment" not in pipeline.cfg.request


# --- the store's guarantees ----------------------------------------------

def test_the_store_refuses_to_overwrite_an_answer():
    """ADR 0010 -- append-only is what makes replaying rounds safe. An
    overwrite would let a late answer change an earlier round's decision."""
    store = AnswerStore()
    store.add("target_value#1", "300m")
    with pytest.raises(ValueError, match="append-only"):
        store.add("target_value#1", "500m")


def test_resubmitting_the_same_answer_halts_rather_than_silently_replaying():
    pipeline = pipe(runner_asking("target_value"))
    result = pipeline.start(TICKET)
    key = result.cfg_question.key
    pipeline.submit({key: "300m"})
    again = pipeline.submit({key: "500m"})
    assert again.status is PipelineStatus.HALTED
    assert "append-only" in again.message


def test_a_run_that_never_converges_halts():
    """A fresh question every round is a loop, not progress. The key changes
    each time, so the store can never satisfy it and the round cap is the only
    thing that ends the run."""
    flow = ConfigChangeFlow({}, max_rounds=3, runner=lambda request: (
        current_asker().ask(CfgQuestion.make("q%d" % flow.round, "?")) or {}))
    result = flow.advance()
    rounds = 0
    while result.status is CfgStatus.NEEDS_HITL and rounds < 10:
        result = flow.submit({result.question.key: "x"})
        rounds += 1
    assert result.status is CfgStatus.HALT
    assert "without converging" in result.message


def test_an_engine_failure_halts_rather_than_raising():
    """DA reports; it does not throw at its supervisor."""
    def boom(request):
        raise RuntimeError("GitHub unreachable")
    result = pipe(boom).start(TICKET)
    assert result.status is PipelineStatus.HALTED
    assert "GitHub unreachable" in result.message


# --- the console default is untouched ------------------------------------

def test_the_default_asker_is_still_the_console():
    """Nothing outside the pipeline changed behaviour: the CLI still prompts."""
    from src.da.section_c_cfg.questions import ConsoleAsker
    assert isinstance(current_asker(), ConsoleAsker)


# --- the engine does not write to the terminal ---------------------------

def noisy_runner(request):
    """An engine run that prints, like the real one does 139 times."""
    print("STEP 1 - reading the request")
    value = current_asker().ask(CfgQuestion.make("target_value", "value?"))
    print("STEP 2 - resolved %s" % value)
    return {"validation": {"status": "VALID"}, "mutation": {"status": "COMPLETED"},
            "github": {"status": "SIMULATED", "results": []}}


def test_the_engine_prints_nothing_through_the_pipeline(capsys):
    """Section A has never printed from inside the pipeline. Section C did, 139
    times, which interleaved with the caller's own stage report -- the caller
    prints stages only after the call returns, so the engine's output arrived
    first."""
    pipeline = pipe(noisy_runner)
    result = pipeline.start(TICKET)
    result = pipeline.submit({result.cfg_question.key: "300m"})
    assert capsys.readouterr().out == ""
    assert result.status is PipelineStatus.COMPLETE


def test_what_it_would_have_printed_is_kept_on_the_result():
    """Captured, not discarded: a caller that wants the trace can still show
    it, and a UI gets it as data rather than as terminal output."""
    pipeline = pipe(noisy_runner)
    result = pipeline.start(TICKET)
    result = pipeline.submit({result.cfg_question.key: "300m"})
    assert "STEP 1 - reading the request" in result.cfg_trace
    assert "STEP 2 - resolved 300m" in result.cfg_trace


def test_replaying_a_round_does_not_repeat_the_trace():
    """Every round re-runs the request from the start, so round two reprinted
    everything round one printed. The trace is per-round, not cumulative."""
    pipeline = pipe(noisy_runner)
    first = pipeline.start(TICKET)
    final = pipeline.submit({first.cfg_question.key: "300m"})
    assert first.cfg_trace.count("STEP 1") == 1
    assert final.cfg_trace.count("STEP 1") == 1


def test_a_halting_run_still_reports_what_it_printed():
    """The trace matters most when something failed."""
    def boom(request):
        print("STEP 1 - reading the request")
        raise RuntimeError("GitHub unreachable")
    result = pipe(boom).start(TICKET)
    assert result.status is PipelineStatus.HALTED
    assert "STEP 1" in result.cfg_trace


# --- Gate B belongs to Section A -----------------------------------------

def test_the_runner_does_not_ask_gate_b_fields_for_a_config_change():
    """X15. `PromptingParser` filled Gate B's baseline fields at intake, before
    classification had run — so a request to raise a CPU limit was asked for a
    "Resource module type (e.g. AKS, Managed-Redis)".

    That was not only noise. `_cfg_request_from_intake` maps `resource_type` to
    `configuration_change`, so whatever was typed became the name of the change
    being requested: typing `gvjhb` produced INVALID for a reason that looked
    unrelated to the answer.
    """
    import run_da

    asked = []
    run_da.ask = lambda prompt, **kw: asked.append(prompt) or "AKS"

    inner = FakeParser()                      # classification="config_changes"
    intake = run_da.PromptingParser(inner, interactive=True) \
        .parse_raw_jira_ticket(TICKET)

    assert asked == [], "asked Section A's baseline questions: %s" % asked
    assert intake.resource_type == "CPU request/limit change"


def test_the_runner_still_asks_gate_b_fields_for_an_iac_ticket():
    """The other half: Section A genuinely needs them, and the pipeline halts
    without them by design."""
    import run_da

    asked = []
    run_da.ask = lambda prompt, **kw: asked.append(prompt) or "AKS"

    inner = FakeParser(classification="cloud_service_iac",
                       resource_type=None, target_service_repo=None)
    run_da.PromptingParser(inner, interactive=True).parse_raw_jira_ticket(TICKET)

    assert any("Resource module type" in p for p in asked), asked
