"""The 28 change types, and the verdicts `validate()` reaches before it reads.

`config_changes.json` is Section C's contract: nothing about a specific
service, field or path is in the Python, so a malformed entry is a silent
failure at the moment someone asks for that change. Twenty-seven of the
twenty-eight had never been exercised.

Two layers here, because they cost very differently:

* the catalogue is checked whole -- every entry, every declared target -- which
  is cheap and catches the failure that would otherwise surface in front of
  someone;
* `validate()` is driven for the verdicts it reaches *before* the first
  repository read, which is most of them. Ambiguity, missing requirements, a
  conflicting answer, an unqualified quantity: all decided from the request and
  the catalogue alone.

Offline: the model and the human are both injected, and nothing here reaches
GitHub, Groq or a manifest.
"""

import json
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(ROOT))

from src.da.llm import client as llm
from src.da.section_c_cfg.engine.core.config import CONFIG_CHANGES_FILE
from src.da.section_c_cfg.engine.processing.validation import validate
from src.da.section_c_cfg.questions import ScriptedAsker, using_asker

CATALOGUE = json.loads(CONFIG_CHANGES_FILE.read_text(encoding="utf-8"))
ENTRIES = CATALOGUE["config_changes"]

# `in` says where a target file lives. Anything else is a typo that would send
# a read at a path the repo does not have.
KNOWN_SCOPES = {"base", "environment", "clusters/<cluster>/<environment>-ingress"}


def ids():
    return [e["request"] for e in ENTRIES]


class ReachedGitHub(Exception):
    """Raised instead of a repository read, so a test cannot quietly use one."""


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Neither a model nor a repository may be reached by accident.

    Both guards earn their keep. Without the second, a case here read the real
    GitOps repo -- the developer's `.env` has a token, so `validate()` ran to
    VALID against a live manifest and the test passed for the wrong reason.
    """
    llm.set_default_completer(
        lambda s, u: pytest.fail("a model was called; the test should inject one"))

    import src.da.section_c_cfg.engine.processing.validation as v
    for name in ("_resolve_pinned_ref", "check_target_existence",
                 "fetch_target_manifests", "_fetch_file_content"):
        monkeypatch.setattr(v, name, _raise_reached(name))
    yield
    llm.set_default_completer(None)


def _raise_reached(name):
    def reached(*args, **kwargs):
        raise ReachedGitHub(name)
    return reached


def model(candidates, requirements=None):
    """A stand-in for the matcher.

    The model is the *primary* matcher here, not a fallback: every run calls it
    once to produce candidates, even when the request names an entry verbatim.
    `_deterministic_entry_candidates` only adds to what it returns. So every
    case below has to supply one, and a case that forgets fails loudly rather
    than reaching the network.
    """
    def complete(system_prompt, user_prompt):
        if "human_must_provide:" in user_prompt:
            return json.dumps({"human_requirements": requirements or []})
        return json.dumps({"candidates": list(candidates),
                           "match_reason": "scripted for the test"})
    return complete


def needs(*names):
    """`human_must_provide` items the request did not satisfy."""
    return [{"requirement": n, "satisfied": False, "source_field": None}
            for n in names]


def run(request, answers=None, completer=None):
    """`validate()` with the human and the model both scripted.

    Returns `(result, asker)`, or `(ReachedGitHub, asker)` when the run got far
    enough to want the repository -- which is itself worth asserting: it means
    every check before the read passed.
    """
    if completer is not None:
        llm.set_default_completer(completer)
    asker = ScriptedAsker(answers or {})
    with using_asker(asker):
        try:
            return validate(request, verbose=False), asker
        except ReachedGitHub:
            return ReachedGitHub, asker


# --- the catalogue, all 28 ----------------------------------------------

def test_the_catalogue_has_the_declared_number_of_entries():
    assert len(ENTRIES) == 28


@pytest.mark.parametrize("entry", ENTRIES, ids=ids())
def test_every_entry_declares_the_same_five_keys(entry):
    assert set(entry) == {"id", "request", "human_must_provide",
                          "pre_checks", "target_fields"}


def test_ids_are_unique_and_contiguous():
    assert sorted(e["id"] for e in ENTRIES) == list(range(1, len(ENTRIES) + 1))


def test_request_names_are_unique():
    """Two entries with the same name make disambiguation unanswerable: the
    human picks one and the matcher cannot tell which."""
    names = [e["request"] for e in ENTRIES]
    assert len(set(names)) == len(names)


@pytest.mark.parametrize("entry", ENTRIES, ids=ids())
def test_every_entry_asks_for_service_and_environment(entry):
    """Without both, nothing can locate a file to read."""
    required = " ".join(entry["human_must_provide"]).lower()
    assert "service" in required
    assert "environment" in required


@pytest.mark.parametrize("entry", ENTRIES, ids=ids())
def test_every_entry_declares_at_least_one_pre_check(entry):
    """An entry with no pre-checks would mutate on nothing but a name match."""
    assert entry["pre_checks"]
    assert all(isinstance(c, str) and c.strip() for c in entry["pre_checks"])


@pytest.mark.parametrize("entry", ENTRIES, ids=ids())
def test_every_target_declares_a_file_a_scope_and_fields(entry):
    assert entry["target_fields"], "no target: nothing to mutate"
    for target in entry["target_fields"]:
        assert target["file"].endswith((".yaml", ".yml", ".json")), target["file"]
        assert target["in"] in KNOWN_SCOPES, target["in"]
        assert target["fields"], target["file"]


@pytest.mark.parametrize("entry", ENTRIES, ids=ids())
def test_every_field_path_is_a_usable_path(entry):
    """A path is walked segment by segment into parsed YAML. An empty one would
    return the whole document, and the mutation would replace the file."""
    for target in entry["target_fields"]:
        for name, path in target["fields"].items():
            assert isinstance(path, list) and path, "%s -> %r" % (name, path)
            assert all(isinstance(p, (str, int)) for p in path)
            assert all(str(p).strip() for p in path)


def test_every_environment_has_a_cluster():
    """`in: clusters/<cluster>/...` is resolved through this map; an
    environment missing from it cannot have its ingress files located."""
    clusters = CATALOGUE["environment_clusters"]
    assert clusters
    assert all(name and cluster for name, cluster in clusters.items())


def test_the_catalogue_names_no_service():
    """The Python has nothing service-specific in it, and neither should this:
    a service name here would make the entry work for one service only."""
    text = json.dumps(ENTRIES)
    assert "cf-app-registration" not in text
    assert "cf-mandi-ocr-formbuilder" not in text


# --- the verdicts reached before any repository read ---------------------

def test_an_unknown_change_is_invalid_not_a_guess():
    """No candidate from either matcher means no change to make. The verdict is
    INVALID, never the nearest entry."""
    result, _ = run({"configuration_change": "make it faster",
                     "service": "cf-app-registration", "environment": "dev"},
                    completer=model([]))
    assert result["status"] == "INVALID"


def test_an_ambiguous_change_asks_which_one():
    """"max" matches five entries. It must ask, never pick the first."""
    result, asker = run({"configuration_change": "max",
                         "service": "cf-app-registration", "environment": "dev"},
                        completer=model([]))
    assert result["status"] == "INCOMPLETE"
    assert any(q.field == "configuration_change" for q in asker.asked)
    assert len(asker.asked[0].allowed_values) > 1


def test_the_ambiguity_question_offers_only_real_entries():
    _, asker = run({"configuration_change": "max",
                    "service": "cf-app-registration", "environment": "dev"},
                   completer=model([]))
    offered = set(asker.asked[0].allowed_values)
    assert offered <= set(ids()), offered - set(ids())


def test_an_exact_name_is_not_treated_as_ambiguous():
    """A request that names an entry outright must not be re-asked."""
    _, asker = run({"configuration_change": "Replica count change",
                    "service": "cf-app-registration", "environment": "dev"},
                   completer=model(["Replica count change"]))
    assert all(q.field != "configuration_change" for q in asker.asked)


def test_the_matcher_is_consulted_even_for_a_verbatim_name():
    """Worth pinning because it is a cost, not a feature: the model is the
    primary matcher and runs before the deterministic pass, which only adds to
    its candidates. A request naming an entry exactly still spends a call."""
    calls = []

    def counting(system_prompt, user_prompt):
        calls.append(user_prompt)
        return model(["Replica count change"])(system_prompt, user_prompt)

    run({"configuration_change": "Replica count change",
         "service": "cf-app-registration", "environment": "dev"},
        completer=counting)
    assert calls, "the matcher was not called at all"


def test_a_missing_requirement_is_incomplete_not_a_default():
    """`Replica count change` needs a value and which field. Neither is given,
    and neither may be invented."""
    result, _ = run({"configuration_change": "Replica count change",
                     "service": "cf-app-registration", "environment": "dev"},
                    completer=model(["Replica count change"],
                                    needs("Desired replica value",
                                          "Which field: minReplicas, maxReplicas, or both")))
    assert result["status"] == "INCOMPLETE"
    assert result["missing"]


def test_which_field_is_settled_before_anything_is_read():
    """`Replica count change` declares `minReplicas` and `maxReplicas`. The
    request names neither, so it asks -- and it asks before touching the repo,
    because reading depends on knowing which field is the target."""
    entry = next(e for e in ENTRIES if e["request"] == "Replica count change")
    declared = entry["target_fields"][0]["fields"]
    result, asker = run({"configuration_change": "Replica count change",
                         "service": "cf-app-registration", "environment": "dev"},
                        completer=model(["Replica count change"],
                                        needs("Desired replica value")))
    assert result is not ReachedGitHub
    assert result["status"] == "INCOMPLETE"
    assert [q.field for q in asker.asked] == ["which_field"]
    # The options come from the entry, not from prose in the code.
    assert set(asker.asked[0].allowed_values) == set(declared) | {"both"}


def test_a_complete_request_gets_as_far_as_the_repository():
    """Everything the entry declares is supplied, so nothing is left to ask and
    the next thing needed is the file itself. Reaching the read *is* the
    assertion: every pre-read check passed."""
    result, asker = run({"configuration_change": "CPU request/limit change",
                         "service": "cf-app-registration", "environment": "dev",
                         "target_value": "300m", "request_or_limit": "Request"},
                        completer=model(["CPU request/limit change"]))
    assert result is ReachedGitHub
    assert [q.field for q in asker.asked] == []


def test_nothing_is_read_until_the_request_is_settled():
    """The mirror of the case above: an unsettled request must not cost a
    repository read."""
    result, _ = run({"configuration_change": "max",
                     "service": "cf-app-registration", "environment": "dev"},
                    completer=model([]))
    assert result is not ReachedGitHub
    assert result["status"] == "INCOMPLETE"


def test_a_malformed_quantity_is_only_caught_after_the_file_is_read():
    """Recorded as it behaves, not as it should.

    `300x` is not a Kubernetes quantity and nothing in the repository could
    make it one, but the validity check runs after the target file is fetched.
    So a value that is unusable on its face still costs a read.

    Not a correctness problem -- it is still rejected -- and worth knowing
    before anyone moves these checks around: the ordering is incidental, not
    load-bearing, and pulling the quantity check forward would be safe.
    """
    result, _ = run({"configuration_change": "CPU request/limit change",
                     "service": "cf-app-registration", "environment": "dev",
                     "target_value": "300x", "request_or_limit": "Request"},
                    completer=model(["CPU request/limit change"]))
    assert result is ReachedGitHub


def test_every_verdict_is_one_of_the_four():
    """`validate()` has one vocabulary, and a caller switches on it."""
    for request, candidates in (
            ({"configuration_change": "make it faster",
              "service": "s", "environment": "dev"}, []),
            ({"configuration_change": "max", "service": "s",
              "environment": "dev"}, []),
            ({"configuration_change": "Replica count change",
              "service": "s", "environment": "dev"}, ["Replica count change"])):
        result, _ = run(request, completer=model(candidates,
                                                 needs("Desired replica value")))
        assert result is not ReachedGitHub
        assert result["status"] in {"VALID", "INVALID", "INCOMPLETE", "CONFLICT"}


def test_a_verdict_always_names_the_change_it_judged():
    result, _ = run({"configuration_change": "Replica count change",
                     "service": "s", "environment": "dev"},
                    completer=model(["Replica count change"],
                                    needs("Desired replica value")))
    assert result is not ReachedGitHub
    assert result["configuration_change"] == "Replica count change"
