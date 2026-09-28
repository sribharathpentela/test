#!/usr/bin/env python3
"""
github.py
---------
All read-access to the GitOps repository: existence checks, file content
fetches, and the file-tree listing used for manifest discovery. Pure
GitHub REST API access — no parsing, no business logic, no LLM. Depends
only on config.py and exceptions.py.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

import requests

from ....config import MissingConfig
from .config import (GIT_API_BASE_URL, GITHUB_TOKEN, GITOPS_REPO,
                     GITOPS_REPO_BRANCH, require_gitops_config)
from .exceptions import GitOpsAccessError


def _github_get(path: str, **params: Any) -> requests.Response:
    # One check, naming the variables actually read. The old one named
    # GITHUB_TOKEN only, which is now the fallback rather than the setting.
    try:
        require_gitops_config()
    except MissingConfig as exc:
        raise GitOpsAccessError(str(exc)) from exc
    url = f"{GIT_API_BASE_URL.rstrip('/')}/repos/{GITOPS_REPO}{path}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    if GITOPS_REPO_BRANCH:
        params.setdefault("ref", GITOPS_REPO_BRANCH)
    try:
        return requests.get(url, headers=headers, params=params, timeout=15)
    except requests.exceptions.RequestException as exc:
        raise GitOpsAccessError(f"Could not reach GitHub at {path}: {exc}") from exc


def _github_path_exists(path: str, ref: Optional[str] = None) -> bool:
    response = _github_get(f"/contents/{path}", **({"ref": ref} if ref else {}))
    if response.status_code == 200:
        return True
    if response.status_code == 404:
        return False
    if response.status_code in (401, 403):
        raise GitOpsAccessError(
            f"GitHub authentication/permission error while checking '{path}': "
            f"HTTP {response.status_code}. Check GIT_GITOPS_PAT and its access to {GITOPS_REPO}."
        )
    raise GitOpsAccessError(f"Unexpected GitHub API response while checking '{path}': HTTP {response.status_code}")


def _get_default_branch() -> str:
    if GITOPS_REPO_BRANCH:
        return GITOPS_REPO_BRANCH
    response = _github_get("")
    if response.status_code != 200:
        raise GitOpsAccessError(f"Could not fetch repo metadata: HTTP {response.status_code}")
    return response.json().get("default_branch", "main")


def _resolve_pinned_ref() -> str:
    """Resolves the EXACT commit SHA every read within one validate() run
    is pinned to, once — so a cross-file consistency check is guaranteed
    to compare values from the SAME repository state, never two reads
    that could land on different commits if the branch moved in between."""
    branch = _get_default_branch()
    response = _github_get(f"/git/refs/heads/{branch}")
    if response.status_code != 200:
        raise GitOpsAccessError(f"Could not resolve current commit for branch '{branch}': HTTP {response.status_code}")
    return response.json()["object"]["sha"]


def _fetch_file_content(file_path: str, ref: Optional[str] = None) -> Optional[str]:
    """Fetches via the GitHub Contents API — never raw.githubusercontent.com,
    which sits behind a CDN that doesn't guarantee reflecting a push
    immediately (a real observed cause of false-positive mismatch aborts).
    Returns None on 404; raises on any other failure. `ref`, when given,
    pins the read to that exact commit."""
    response = _github_get(f"/contents/{file_path}", **({"ref": ref} if ref else {}))
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise GitOpsAccessError(f"Could not fetch '{file_path}': HTTP {response.status_code}")
    return base64.b64decode(response.json().get("content", "")).decode("utf-8")


def check_target_existence(service: str, environment: str, ref: Optional[str] = None) -> dict[str, Any]:
    """Checks whether base/<service>/ and environments/<environment>/<service>/ exist."""
    base_path = f"base/{service}"
    environment_path = f"environments/{environment}/{service}"
    return {
        "base_path": base_path,
        "base_exists": _github_path_exists(base_path, ref=ref),
        "environment_path": environment_path,
        "environment_exists": _github_path_exists(environment_path, ref=ref),
    }


_MANIFEST_EXTENSIONS = {".yaml", ".yml", ".json"}
_MAX_MANIFEST_CHARS = 60_000
_MAX_FETCH_WORKERS = 8


def fetch_target_manifests(labeled_prefixes: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    """Fetches every real file under any of labeled_prefixes (a list of
    (label, path_prefix) pairs — label names which config_changes.json
    field asked for that path, for the verbose trace). Used only when an
    entry has no target_fields declared (context/existence display).
    Fetched concurrently — the slowest step in the pipeline otherwise."""
    branch = _get_default_branch()
    response = _github_get(f"/git/trees/{branch}", recursive="1")
    if response.status_code != 200:
        raise GitOpsAccessError(f"Could not list repo file tree: HTTP {response.status_code}")

    normalized = [(label, p.rstrip("/")) for label, p in labeled_prefixes]

    def _label_for(path: str) -> Optional[str]:
        for label, prefix in normalized:
            if path == prefix or path.startswith(prefix + "/"):
                return label
        return None

    candidates: list[tuple[str, str]] = []
    for item in response.json().get("tree", []):
        if item.get("type") != "blob":
            continue
        path = item["path"]
        if Path(path).suffix.lower() not in _MANIFEST_EXTENSIONS:
            continue
        label = _label_for(path)
        if label:
            candidates.append((path, label))

    results: dict[str, dict[str, Any]] = {}
    total_chars = 0
    with ThreadPoolExecutor(max_workers=_MAX_FETCH_WORKERS) as pool:
        future_to_candidate = {pool.submit(_fetch_file_content, path): (path, label) for path, label in candidates}
        for future in as_completed(future_to_candidate):
            path, label = future_to_candidate[future]
            text = future.result()
            if text is None or total_chars + len(text) > _MAX_MANIFEST_CHARS:
                continue
            results[path] = {"content": text, "label": label}
            total_chars += len(text)

    return results
