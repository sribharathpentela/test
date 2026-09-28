"""Fetch a repository at a ref and put it on disk, so the loaders can read it.

Git is the single source of truth (ADR 0021); the contract builders want paths
(ADR 0020). This module is the join: fetch at an explicit ref, extract to a
cache, hand back a directory.

The cache is **never authoritative**. It is written only by a fetch, is keyed by
`{repo}/{ref}` so two refs never collide, and may be deleted at any time -- a
miss is a fetch, never a fallback to something stale.

Why the whole repo rather than one module's files:

* `build_contract` lists *every* `.tf` in the module directory, not a fixed five
  (`module_contract.py:57`) -- `private-endpoint.tf` and `linkedservers.tf` carry
  references, and missing them corrupts `referenced_undeclared`.
* `pair_modules_with_templates` reads *every* template's declared `source` to
  pair it (ADR 0013), so a single slug's template is not enough.

One tarball call satisfies both and costs fewer requests than fetching ~35 files
individually. A tag is immutable, so a hit needs no revalidation.
"""

import io
import os
import shutil
import tarfile
import time
from typing import List, Optional

import httpx
from pydantic import BaseModel

from ...config import GitSettings, git_settings

DEFAULT_CACHE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))), ".cache", "repos")


class MaterialiseError(Exception):
    """The ref could not be fetched. DA cannot proceed on a guess."""


class MaterialisedRepo(BaseModel):
    """A repository, at one ref, on local disk."""

    repo: str
    ref: str
    root: str
    from_cache: bool
    file_count: int = 0
    message: str = ""

    def module_dir(self, module_path: str) -> str:
        """Local directory for a repo-relative module path. Case is preserved."""
        return os.path.join(self.root, *module_path.split("/"))

    def has_module(self, module_path: str) -> bool:
        return os.path.isfile(os.path.join(self.module_dir(module_path), "variables.tf"))


def materialise(repo: str,
                ref: str,
                settings: Optional[GitSettings] = None,
                cache_root: Optional[str] = None,
                refresh: bool = False) -> MaterialisedRepo:
    """Put `repo` at `ref` on disk and return where it landed.

    Raises MaterialiseError if the ref cannot be fetched -- an empty or partial
    extraction must never be handed to the contract builders as if it were the
    module catalogue.
    """
    settings = settings or git_settings()
    cache_root = cache_root or DEFAULT_CACHE
    root = os.path.join(cache_root, repo, _safe(ref))

    if os.path.isdir(root) and os.listdir(root) and not refresh:
        count = sum(len(files) for _r, _d, files in os.walk(root))
        return MaterialisedRepo(
            repo=repo, ref=ref, root=root, from_cache=True, file_count=count,
            message="%s@%s: %d files (cached)" % (repo, ref, count))

    token = settings.pat_for(repo)
    if token is None:
        raise MaterialiseError(
            "no token is configured for '%s'. DA reaches only the repos it is "
            "given credentials for." % repo)

    url = "%s/repos/%s/%s/tarball/%s" % (
        settings.api_base_url.rstrip("/"), settings.org, repo, ref)
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "IPS-Deployment-Agent",
               "Authorization": "token %s" % token}
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
    except Exception as exc:                       # noqa: BLE001 - re-raised typed
        raise MaterialiseError("fetching %s@%s: %s" % (repo, ref, exc))

    if response.status_code != 200:
        raise MaterialiseError(
            "%s@%s returned HTTP %s. A ref that will not fetch is not a ref to "
            "guess around." % (repo, ref, response.status_code))

    staging = root + ".partial"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
            _extract_stripped(tar, staging)
    except MaterialiseError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as exc:                       # noqa: BLE001 - re-raised typed
        shutil.rmtree(staging, ignore_errors=True)
        raise MaterialiseError("unpacking %s@%s: %s" % (repo, ref, exc))

    # Swap in only once the extraction is whole, so an interrupted fetch can
    # never leave a half-populated cache that reads as a complete repo.
    _replace_dir(staging, root)

    count = sum(len(files) for _r, _d, files in os.walk(root))
    return MaterialisedRepo(
        repo=repo, ref=ref, root=root, from_cache=False, file_count=count,
        message="%s@%s: %d files fetched" % (repo, ref, count))


def _replace_dir(new: str, target: str) -> None:
    """Move `new` onto `target`, replacing whatever was there.

    `os.rename` will not overwrite an existing directory -- on Windows it raises
    WinError 183, and on POSIX it raises ENOTEMPTY. A refresh therefore has to
    move the old tree aside first. This path runs whenever a tag moves, which is
    precisely when the cache must not be left holding stale content.

    OneDrive and virus scanners hold transient handles on freshly written files,
    so the delete is retried rather than trusted first time.
    """
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)

    displaced = target + ".old"
    _rmtree_with_retry(displaced)
    if os.path.isdir(target):
        _rename_with_retry(target, displaced)
    try:
        _rename_with_retry(new, target)
    except OSError:
        # Put the previous tree back rather than leave nothing at all.
        if os.path.isdir(displaced) and not os.path.isdir(target):
            _rename_with_retry(displaced, target)
        raise
    _rmtree_with_retry(displaced)


def _rename_with_retry(src: str, dst: str, attempts: int = 5) -> None:
    """Windows denies a rename while any handle is open on the tree.

    OneDrive, indexers and scanners all take short-lived handles on files that
    were written moments ago, so a first attempt failing means "wait", not
    "impossible".
    """
    for attempt in range(attempts):
        try:
            os.rename(src, dst)
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.3 * (attempt + 1))


def _rmtree_with_retry(path: str, attempts: int = 5) -> None:
    for attempt in range(attempts):
        if not os.path.exists(path):
            return
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if attempt == attempts - 1:
                shutil.rmtree(path, ignore_errors=True)
                return
            time.sleep(0.2 * (attempt + 1))


def _extract_stripped(tar: tarfile.TarFile, dest: str) -> None:
    """Extract, dropping GitHub's `{owner}-{repo}-{sha}/` wrapper directory."""
    dest = os.path.abspath(dest)
    members: List[tarfile.TarInfo] = []
    for member in tar.getmembers():
        parts = member.name.split("/", 1)
        if len(parts) != 2 or not parts[1]:
            continue                                # the wrapper directory itself
        member.name = parts[1]
        target = os.path.abspath(os.path.join(dest, member.name))
        if not target.startswith(dest + os.sep):
            raise MaterialiseError("archive member escapes the cache: %r" % member.name)
        members.append(member)
    if not members:
        raise MaterialiseError("archive was empty")
    try:
        tar.extractall(dest, members=members, filter="data")
    except TypeError:                               # Python < 3.12
        tar.extractall(dest, members=members)


def _safe(ref: str) -> str:
    """`refs/heads/x` and friends must not become directories."""
    return ref.replace("/", "_").replace("\\", "_").replace("..", "_")
