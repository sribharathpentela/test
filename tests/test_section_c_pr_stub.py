"""Section C — the removed write path and the simulated PR.

Section A has never had a write path: it renders a `terragrunt.hcl` and a PR
body and stops. Section C's Step 5 used to branch, commit and open a real PR,
which made it the only code in the agent that could change a real repository.
Those six functions were removed on 2026-09-26 and `--execute` now reports the
plan instead.

The point of these tests is that the removal is **structural, not a flag**.
A guard can be passed the wrong argument; a module with no HTTP client in it
cannot open a PR however it is called. That property is what is pinned here,
because it is the one a later refactor could quietly undo.

Offline by construction -- resolving the base branch is the only GitHub read
left anywhere near this path, and it is allowed to fail.
"""

import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(ROOT))

from src.da.section_c_cfg.engine.workflow import run as flow


CHANGES_BY_FILE = {
    "apps/cf-app-registration/base/deployment-v1.yaml": [
        {
            "file": "apps/cf-app-registration/base/deployment-v1.yaml",
            "field_path": ["spec", "template", "spec", "containers", 0,
                           "resources", "requests", "cpu"],
            "previous_value": "200m",
            "new_value": "300m",
            "kind": "Deployment",
            "output_path": "(not read by the plan builder)",
        }
    ],
}

MUTATION_RESULT = {
    "validation": {"status": "VALID", "matched_change": "CPU request/limit change"},
    "mutation": {"status": "COMPLETED", "files": list(CHANGES_BY_FILE.values())[0]},
}

INPUT_REQUEST = {
    "configuration_change": "CPU request/limit change",
    "service": "cf-app-registration",
    "environment": "dev",
    "target_value": "300m",
    "request_or_limit": "Request",
}


@pytest.fixture
def plan(monkeypatch):
    # The base-branch lookup is the one GitHub read left; pin it so the test
    # says nothing about the network either way.
    monkeypatch.setattr(flow, "_get_default_branch", lambda: "main")
    monkeypatch.setattr(flow, "build_pr_description",
                        lambda *a, **k: "## CPU request/limit change\n\n200m -> 300m")
    return flow._build_pr_plan(
        CHANGES_BY_FILE, MUTATION_RESULT, INPUT_REQUEST,
        service="cf-app-registration", environment="dev",
        matched_change="CPU request/limit change",
        branch_name="feature/config-cf-app-registration-cpu-20260926T120000",
    )


# --- the removal is structural -------------------------------------------

def test_the_module_has_no_http_client():
    """`requests` and `base64` were used only by the write path and went with
    it. Without a transport there is nothing to open a PR with."""
    assert not hasattr(flow, "requests")
    assert not hasattr(flow, "base64")


@pytest.mark.parametrize("name", [
    "_github_json_request",
    "get_base_branch_sha",
    "create_feature_branch",
    "get_file_sha_on_branch",
    "update_file_on_branch",
    "create_pull_request",
])
def test_every_write_function_is_gone(name):
    assert not hasattr(flow, name)


def test_no_module_in_section_c_can_write_to_github():
    """A write would have to go out through `requests` somewhere. Only the
    read-only client may import it."""
    import importlib
    writers = []
    for mod in ("core.config", "core.exceptions", "core.llm_hitl",
                "processing.validation", "processing.mutation",
                "workflow.run"):
        m = importlib.import_module("src.da.section_c_cfg.engine." + mod)
        if hasattr(m, "requests"):
            writers.append(mod)
    assert writers == [], (
        "only core.github may hold an HTTP client; also found: %s" % writers)


# --- the plan is complete ------------------------------------------------

def test_the_plan_carries_everything_the_write_path_would_have_sent(plan):
    """"PR raised" on its own proves nothing. The demo claim is that the agent
    knew exactly what it would have done and stopped anyway."""
    assert set(plan) == {"branch", "base_branch", "files",
                         "commit_messages", "pr_title", "pr_body"}
    assert plan["branch"].startswith("feature/config-cf-app-registration-")
    assert plan["base_branch"] == "main"
    assert plan["files"] == list(CHANGES_BY_FILE)
    assert plan["pr_title"] == "CPU request/limit change: cf-app-registration (dev)"
    assert "200m -> 300m" in plan["pr_body"]


def test_one_commit_message_per_file(plan):
    assert len(plan["commit_messages"]) == len(plan["files"])
    assert plan["commit_messages"][0].startswith(
        "config: CPU request/limit change for cf-app-registration")


def test_a_missing_base_branch_does_not_break_the_simulation(monkeypatch):
    """A simulation must not require a token it never spends."""
    def boom():
        raise RuntimeError("no GitHub access")
    monkeypatch.setattr(flow, "_get_default_branch", boom)
    monkeypatch.setattr(flow, "build_pr_description", lambda *a, **k: "body")
    built = flow._build_pr_plan(
        CHANGES_BY_FILE, MUTATION_RESULT, INPUT_REQUEST,
        service="s", environment="dev", matched_change="c", branch_name="b")
    assert "unresolved" in built["base_branch"]


# --- the simulated result ------------------------------------------------

def test_simulate_returns_the_dev_phase_message(plan, capsys):
    result = flow._simulate_pr(CHANGES_BY_FILE, plan["branch"], plan)
    assert result["status"] == "SIMULATED"
    assert result["message"] == "Raised PR with these details — In development Phase"
    assert result["plan"] == plan


def test_simulate_never_reports_a_pr_url_or_commit(plan):
    """Nothing downstream should be able to mistake a simulation for a real PR
    by finding a URL or a SHA on it."""
    result = flow._simulate_pr(CHANGES_BY_FILE, plan["branch"], plan)
    assert result["pr_url"] is None
    assert result["commit_shas"] == []


def test_simulate_prints_the_plan(plan, capsys):
    flow._simulate_pr(CHANGES_BY_FILE, plan["branch"], plan)
    out = capsys.readouterr().out
    assert "In development Phase" in out
    assert plan["branch"] in out
    assert "apps/cf-app-registration/base/deployment-v1.yaml" in out


def test_completed_is_no_longer_a_reachable_status():
    """`COMPLETED` meant a real PR existed. Leaving it in the exit-code map
    would let a future change resurrect the claim without the behaviour."""
    import inspect
    source = inspect.getsource(flow.main)
    assert "COMPLETED" not in source
    assert "SIMULATED" in source


# --- the artifact is Honeywell's, not the tool's -------------------------

def test_no_tool_attribution_anywhere_in_the_pr(plan):
    """A PR raised against a Honeywell repository is a Honeywell artifact. The
    reviewer needs what changed and what checked it, not which editor produced
    the agent. The commit trailer was also crediting a model that never ran."""
    text = plan["pr_body"] + "\n".join(plan["commit_messages"])
    for leak in ("Claude", "claude.com", "anthropic", "Co-Authored-By", "🤖"):
        assert leak not in text, "%r is still in the PR" % leak


def test_the_commit_message_says_only_what_changed(plan):
    assert plan["commit_messages"][0].splitlines()[-1].startswith("config: ")
    assert len(plan["commit_messages"][0].splitlines()) == 1
