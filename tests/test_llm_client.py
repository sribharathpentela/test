"""One model client for the whole agent.

There were three: `intake/parser.py` built a `Groq` in its constructor,
`slots/filler.py` built one inside `groq_completer`, and Section C's
`llm_hitl.py` built one per call. They sent the same request and disagreed on
everything around it -- one retried a rate limit, one pinned a seed, one
wrapped vendor failures in its own exception type.

The property worth pinning is that there is now exactly **one** place that
constructs a client, because that is what makes swapping the model to a
Databricks foundation endpoint a one-file change rather than a hunt.

Offline: `groq` is never imported here, and no test needs a key.
"""

import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(ROOT))

from src.da.llm import client as llm


@pytest.fixture(autouse=True)
def _reset():
    yield
    llm.set_default_completer(None)


# --- there is one client -------------------------------------------------

def test_only_one_module_constructs_a_model_client():
    """A second `Groq(...)` anywhere means the swap has two places to reach."""
    import glob
    import io
    sites = []
    for path in glob.glob("src/**/*.py", recursive=True):
        if "__pycache__" in path:
            continue
        if "Groq(" in io.open(path, encoding="utf-8").read():
            sites.append(path.replace("\\", "/"))
    assert sites == ["src/da/llm/client.py"], sites


def test_every_caller_shares_the_same_contract():
    """`Completer` is what the pipeline already injects, so one seam covers
    intake, slot filling and Section C's clarifications alike."""
    from src.da.section_a_iac.slots.filler import groq_completer as from_filler
    assert from_filler is llm.groq_completer


# --- the completer contract ----------------------------------------------

def test_complete_json_parses_what_the_completer_returns():
    got = llm.complete_json("sys", "user", completer=lambda s, u: '{"a": 1}')
    assert got == {"a": 1}


def test_complete_json_passes_both_prompts_through():
    seen = {}

    def spy(system, user):
        seen["system"], seen["user"] = system, user
        return "{}"

    llm.complete_json("SYSTEM", "USER", completer=spy)
    assert seen == {"system": "SYSTEM", "user": "USER"}


def test_bad_json_reports_what_came_back():
    """Two of the three old call sites raised a bare parse error, which left
    nothing to debug with."""
    with pytest.raises(llm.LLMError) as caught:
        llm.complete_json("s", "u", completer=lambda s, u: "Sorry, I cannot")
    assert "Sorry, I cannot" in str(caught.value)


def test_an_empty_response_is_an_error_not_an_empty_dict():
    with pytest.raises(llm.LLMError):
        llm.complete_json("s", "u", completer=lambda s, u: "")


# --- the default, and overriding it --------------------------------------

def test_the_default_completer_can_be_replaced():
    llm.set_default_completer(lambda s, u: '{"from": "override"}')
    assert llm.complete_json("s", "u") == {"from": "override"}


def test_importing_the_module_does_not_require_a_key():
    """Built lazily on purpose: if importing needed a credential, the offline
    suite could not import the code it tests."""
    assert llm._default is None or callable(llm._default)


def test_a_missing_key_says_so_rather_than_failing_inside_the_vendor(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(llm.LLMError, match="GROQ_API_KEY"):
        llm.groq_completer()


def test_the_model_is_configuration_not_a_literal(monkeypatch):
    monkeypatch.setenv("GROQ_MODEL", "some/other-model")
    assert llm.model_name() == "some/other-model"
    assert llm.model_name("explicit/win") == "explicit/win"


def test_there_is_a_default_model_when_nothing_is_set(monkeypatch):
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    assert llm.model_name() == llm.DEFAULT_MODEL


# --- the callers are injectable -----------------------------------------

def test_the_intake_parser_takes_an_injected_completer():
    """It used to build a client in `__init__`, so constructing the parser
    required a key even when the caller only wanted to parse."""
    from src.da.intake.parser import JiraIntakeParser

    parser = JiraIntakeParser(completer=lambda s, u: (
        '{"is_da_task": true, "classification": "cloud_service_iac",'
        ' "environment": "dev", "resource_type": "AKS"}'))
    intake = parser.parse_raw_jira_ticket(
        {"jira": {"key": "DEVOPS-1", "summary": "s", "description": "d"}})
    assert intake.is_da_task is True
    assert intake.ticket_metadata.ticket_id == "DEVOPS-1"


def test_constructing_the_parser_needs_no_credential(monkeypatch):
    from src.da.intake.parser import JiraIntakeParser

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    JiraIntakeParser()          # must not raise


def test_section_c_reports_a_model_failure_in_its_own_terms():
    """`validation.py` catches `GroqApiError` throughout; a model failure has
    always meant the same as any other tool error -- report, never guess."""
    from src.da.section_c_cfg.engine.core.exceptions import GroqApiError
    from src.da.section_c_cfg.engine.core.llm_hitl import _call_groq_json

    llm.set_default_completer(lambda s, u: "not json")
    with pytest.raises(GroqApiError):
        _call_groq_json("s", "u")
