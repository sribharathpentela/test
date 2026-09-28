"""Every environment-specific value DA needs, in one place.

Nothing below is hardcoded with a working default. The point is that moving from
this GitHub.com mirror to Honeywell's GitHub Enterprise Server in VDI is a change
to `.env` and two tokens -- not a change to code. Three values in particular used
to be baked in and each would have forced an edit at migration:

* the API base URL   -- GHES serves `https://<host>/api/v3`, not api.github.com
* the owner          -- was a hardcoded username, ignoring `GIT_ORG` entirely
* the default branch -- HON's monorepo default is not necessarily `main`

Names are ALL-CAPS because environment variables are case-sensitive on Linux and
case-insensitive on Windows; mixed case works locally and fails in CI.
"""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel

# `src/da/config.py` -> `src/da` -> `src` -> the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Loaded by path, not by searching upward from the working directory: the agent
# is imported from a notebook, a test runner and a CLI, and only one of those
# is reliably started from the repo root.
load_dotenv(REPO_ROOT / ".env")
load_dotenv()

# Generated artifacts. Runs write here; `src/` is what the repository is, not
# where a run leaves its results.
OUTPUT_DIR = Path(os.getenv("DA_OUTPUT_DIR") or (REPO_ROOT / "output"))


class MissingConfig(Exception):
    """A required setting is absent. Raised, not returned: DA cannot proceed."""


class GitSettings(BaseModel):
    """Where the repos live and how to reach them."""

    api_base_url: str
    org: str
    default_branch: str
    modules_repo: str
    modules_pat: str
    app_repo: str
    app_pat: str

    # Section C's GitOps repo. Optional because Section A never touches it, so
    # an IaC-only run must not be made to carry a credential it has no use for.
    # Section C asks for them explicitly and fails loudly if they are absent.
    gitops_repo: Optional[str] = None
    gitops_pat: Optional[str] = None

    def repo_url(self, repo: str, *path: str) -> str:
        parts = "/".join(str(p).strip("/") for p in path if str(p).strip("/"))
        base = "%s/repos/%s/%s" % (self.api_base_url.rstrip("/"), self.org, repo)
        return "%s/%s" % (base, parts) if parts else base

    def pat_for(self, repo: str) -> Optional[str]:
        """The token scoped to `repo`, or None if DA has no business there."""
        if repo == self.modules_repo:
            return self.modules_pat
        if repo == self.app_repo:
            return self.app_pat
        if self.gitops_repo and repo == self.gitops_repo:
            return self.gitops_pat
        return None


def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise MissingConfig(
            "%s is not set. See .env.example; DA does not guess an endpoint, an "
            "owner or a credential." % name)
    return value


def first_env(*names: str) -> str:
    """The first of `names` with a non-empty value, or "".

    Two naming schemes reach the same settings: DA's `GIT_*` names, and the
    ones Section C shipped with before it was merged in. DA's win; the others
    keep an older checkout's `.env` working.
    """
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


class GitOpsSettings(BaseModel):
    """Where Section C's Kubernetes manifests live, and how to reach them.

    Resolved here rather than in the section, because a section is an aid to
    reading and not a thing that runs on its own -- anything it needs in order
    to run is the pipeline's.
    """

    api_base_url: str
    repo: str
    pat: str
    branch: Optional[str] = None

    def require(self) -> None:
        """Fail loudly, naming what to set. Called before the first repo read."""
        missing = []
        if not self.repo:
            missing.append("GIT_GITOPS_REPO (owner/repo, or a bare name with GIT_ORG)")
        if not self.pat:
            missing.append("GIT_GITOPS_PAT (read-only is enough; this agent never writes)")
        if missing:
            raise MissingConfig(
                "Section C is not configured to reach its GitOps repo. Set, in "
                "the repo-root .env: " + "; ".join(missing))


def gitops_settings() -> GitOpsSettings:
    """Read the GitOps repo's settings. No working default for any of them.

    A hardcoded fallback here used to point at one developer's personal mirror,
    so an unconfigured run read a real repository that happened to exist
    instead of reporting that it was unconfigured.
    """
    repo = first_env("GIT_GITOPS_REPO", "GITOPS_REPO")
    org = first_env("GIT_ORG")
    # DA keeps the owner separately in `GIT_ORG`, so a bare repo name is
    # qualified with it rather than guessed at.
    if repo and "/" not in repo and org:
        repo = "%s/%s" % (org, repo)

    return GitOpsSettings(
        # GHES serves `https://<host>/api/v3`, not api.github.com.
        api_base_url=first_env("GIT_API_BASE_URL") or "https://api.github.com",
        repo=repo,
        pat=first_env("GIT_GITOPS_PAT", "GITHUB_TOKEN"),
        branch=first_env("GITOPS_REPO_BRANCH", "GIT_DEFAULT_BRANCH") or None,
    )


def _optional(name: str) -> Optional[str]:
    """A setting only one section needs. Absent is a valid state here; the
    section that needs it raises `MissingConfig` when it actually does."""
    return (os.getenv(name) or "").strip() or None


def git_settings() -> GitSettings:
    """Build settings from the environment, failing loudly on anything absent."""
    return GitSettings(
        api_base_url=_require("GIT_API_BASE_URL"),
        org=_require("GIT_ORG"),
        default_branch=_require("GIT_DEFAULT_BRANCH"),
        modules_repo=_require("GIT_MODULES_REPO"),
        modules_pat=_require("GIT_MODULES_PAT"),
        app_repo=_require("GIT_APP_REPO"),
        app_pat=_require("GIT_APP_PAT"),
        gitops_repo=_optional("GIT_GITOPS_REPO"),
        gitops_pat=_optional("GIT_GITOPS_PAT"),
    )
