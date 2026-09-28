"""Section C reads its GitOps repo the way Section A reads its repos.

`src/da/config.py` states the rule: moving from this GitHub.com mirror to
Honeywell's GHES in VDI must be a change to `.env` and two tokens, never a
change to code. Section A honours it. Section C did not -- it hardcoded
`https://api.github.com` and defaulted `GITOPS_REPO` to one developer's
personal mirror, so an unconfigured run silently read a real repository that
happened to exist rather than reporting that it was unconfigured.

Both are now read from DA's canonical `GIT_*` names, with Section C's original
names kept as a fallback so an older checkout's `.env` still works.

Resolution now lives in `src/da/config.py`'s `gitops_settings()`; this section
binds the result to module constants at import, so each case controls
`os.environ` and reloads it.
"""

import importlib
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(ROOT))

MODULE = "src.da.section_c_cfg.engine.core.config"

# Everything either naming scheme reads, cleared before each case so a value
# from the developer's own .env cannot make a test pass.
KEYS = ("GIT_API_BASE_URL", "GIT_ORG", "GIT_DEFAULT_BRANCH",
        "GIT_GITOPS_REPO", "GIT_GITOPS_PAT",
        "GITOPS_REPO", "GITOPS_REPO_BRANCH", "GITHUB_TOKEN")


def load(**env):
    """Reload the config module seeing only `env`.

    Every key is set to `""` rather than popped. Loading a `.env` cannot
    overwrite a variable that is already set, so a key that is merely absent
    comes straight back from the developer's own file and the case then passes
    or fails on what happens to be configured on that machine.
    Present-but-empty cannot be refilled, and `first_env` treats it as unset.
    """
    for key in KEYS:
        os.environ[key] = ""
    os.environ.update({k: v for k, v in env.items() if v is not None})
    return importlib.reload(importlib.import_module(MODULE))


@pytest.fixture(autouse=True)
def _restore():
    saved = {k: os.environ.get(k) for k in KEYS}
    yield
    for key, value in saved.items():
        os.environ.pop(key, None)
        if value is not None:
            os.environ[key] = value
    importlib.reload(importlib.import_module(MODULE))


# --- no hardcoded anything ----------------------------------------------

def test_there_is_no_working_default_for_the_repo():
    """The old default pointed at a real personal mirror, so a misconfigured
    run looked like a working one."""
    cfg = load()
    assert not cfg.GITOPS_REPO


def test_an_unconfigured_run_says_so_and_names_both_settings():
    """`MissingConfig`, the same type Section A raises for an absent setting --
    one config module now, so one way of saying a setting is missing."""
    from src.da.config import MissingConfig

    cfg = load()
    with pytest.raises(MissingConfig) as caught:
        cfg.require_gitops_config()
    message = str(caught.value)
    assert "GIT_GITOPS_REPO" in message
    assert "GIT_GITOPS_PAT" in message


def test_the_api_host_comes_from_configuration():
    """GHES serves /api/v3, not api.github.com. This was the one hardcoded
    value that would have forced a code edit at the VDI migration."""
    cfg = load(GIT_API_BASE_URL="https://github.honeywell.com/api/v3",
               GIT_GITOPS_REPO="HON-IA/gitops", GIT_GITOPS_PAT="x")
    assert cfg.GIT_API_BASE_URL == "https://github.honeywell.com/api/v3"
    cfg.require_gitops_config()          # configured, so this must not raise


def test_api_host_falls_back_to_github_com():
    cfg = load(GIT_GITOPS_REPO="o/r", GIT_GITOPS_PAT="x")
    assert cfg.GIT_API_BASE_URL == "https://api.github.com"


# --- one .env drives both sections ---------------------------------------

def test_das_names_are_preferred():
    cfg = load(GIT_GITOPS_REPO="HON-IA/gitops", GITOPS_REPO="old/mirror",
               GIT_GITOPS_PAT="new-token", GITHUB_TOKEN="old-token")
    assert cfg.GITOPS_REPO == "HON-IA/gitops"
    assert cfg.GITHUB_TOKEN == "new-token"


def test_section_cs_own_names_still_work():
    """An older checkout's .env must not stop working."""
    cfg = load(GITOPS_REPO="owner/repo", GITHUB_TOKEN="tok")
    assert cfg.GITOPS_REPO == "owner/repo"
    assert cfg.GITHUB_TOKEN == "tok"


def test_a_bare_repo_name_is_qualified_with_the_org():
    """DA keeps the owner in GIT_ORG and repo names bare; Section C wants
    owner/repo. The org is read, never guessed."""
    cfg = load(GIT_ORG="HON-IA", GIT_GITOPS_REPO="pss_cf_git_ops_deployment")
    assert cfg.GITOPS_REPO == "HON-IA/pss_cf_git_ops_deployment"


def test_a_qualified_name_is_left_alone():
    cfg = load(GIT_ORG="HON-IA", GIT_GITOPS_REPO="someone-else/repo")
    assert cfg.GITOPS_REPO == "someone-else/repo"


def test_a_bare_name_without_an_org_is_not_invented():
    """With no `GIT_ORG` there is nothing to qualify with, and an owner is not
    worth guessing: the wrong one reads a real repo belonging to someone else."""
    cfg = load(GIT_GITOPS_REPO="justarepo")
    assert cfg.GITOPS_REPO == "justarepo"      # unqualified, not guessed at


def test_the_branch_falls_back_to_das_default():
    cfg = load(GIT_DEFAULT_BRANCH="development",
               GIT_GITOPS_REPO="o/r", GIT_GITOPS_PAT="x")
    assert cfg.GITOPS_REPO_BRANCH == "development"


def test_an_explicit_gitops_branch_wins():
    cfg = load(GIT_DEFAULT_BRANCH="development", GITOPS_REPO_BRANCH="main",
               GIT_GITOPS_REPO="o/r", GIT_GITOPS_PAT="x")
    assert cfg.GITOPS_REPO_BRANCH == "main"


# --- the .env is found from the repo root --------------------------------

def test_the_repo_root_dotenv_is_reachable():
    """The section had its own dotenv loader that searched only two levels up.
    Flattening it into `src/da/section_c_cfg/engine/` put the repo-root `.env`
    four levels away, so it silently read no configuration at all. The loader
    is now the pipeline's, and loads by path rather than by searching."""
    cfg = importlib.import_module(MODULE)
    root = os.path.abspath(ROOT)
    assert os.path.commonpath([str(cfg.BASE_DIR), root]) == root
    assert cfg.BASE_DIR.name == "engine"


# --- DA's settings carry the gitops pair ---------------------------------

def test_git_settings_exposes_the_gitops_repo_without_requiring_it():
    """Section A never touches the GitOps repo, so an IaC-only run must not be
    made to carry a credential it has no use for."""
    from src.da.config import GitSettings
    settings = GitSettings(
        api_base_url="https://api.github.com", org="o", default_branch="main",
        modules_repo="m", modules_pat="mp", app_repo="a", app_pat="ap")
    assert settings.gitops_repo is None
    assert settings.pat_for("m") == "mp"
    assert settings.pat_for("anything-else") is None


def test_pat_for_returns_the_gitops_token_when_configured():
    from src.da.config import GitSettings
    settings = GitSettings(
        api_base_url="https://api.github.com", org="o", default_branch="main",
        modules_repo="m", modules_pat="mp", app_repo="a", app_pat="ap",
        gitops_repo="g", gitops_pat="gp")
    assert settings.pat_for("g") == "gp"
