"""Read the service repo's platform files into a PlatformContext.

Everything here is *read*, never assumed. The auto-injected set, the pointer
catalogue, the alias pairs and `module_ref` are all facts about the repo:

* the 8 auto-injected keys -- `tags` was added after v2, so a hardcoded list
  would already be wrong (01-algorithm.md section 3);
* `module_ref` -- DA verifies the pinned tag, it does not pick one (ADR 0006).

This module builds the *static* picture. Per-environment resolution and the
override classes are Step 4a-0 Effective Context (ADR 0003), not Phase 1.
"""

import os
import re
from typing import Dict, List, Optional

from . import hcl_loader as H
from .models import PlatformContext

COMMON_HCL = "common.hcl"
PLATFORM_FILES = ("common.hcl", "env.hcl", "context.hcl", "root.hcl")

# A local is a valid pointer target when it resolves the current environment's
# placement, i.e. it reads through `local.selected`. That is a structural test,
# not a hardcoded list of seven names.
_RE_SELECTED = re.compile(r"\blocal\.selected\b")
_RE_LOCAL_REF = re.compile(r"^\$?\{?\s*local\.([A-Za-z_][A-Za-z0-9_]*)\s*\}?$")


def build_platform_context(repo_root: str) -> PlatformContext:
    """Read common.hcl (and siblings) from the service repo root."""
    repo_root = os.path.abspath(repo_root)
    context = PlatformContext(repo_root=repo_root)

    common_path = os.path.join(repo_root, COMMON_HCL)
    if not os.path.isfile(common_path):
        context.message = "no %s at %s" % (COMMON_HCL, repo_root)
        return context

    try:
        data = H.load_hcl(common_path)
    except H.HclParseError as exc:
        context.message = "could not parse %s: %s" % (COMMON_HCL, exc)
        return context

    context.files_parsed.append(COMMON_HCL)
    for name in PLATFORM_FILES:
        if name != COMMON_HCL and os.path.isfile(os.path.join(repo_root, name)):
            context.files_parsed.append(name)

    locals_map = _merge_blocks(data, "locals")
    inputs_map = _merge_blocks(data, "inputs")

    # Branch 2 -- whatever common.hcl actually injects.
    context.auto_injected = {k: _as_text(v) for k, v in inputs_map.items()
                             if not k.startswith("__")}

    # Branch 4 -- the pointer catalogue.
    context.pointer_locals = sorted(
        name for name, value in locals_map.items()
        if not name.startswith("__") and _RE_SELECTED.search(_as_text(value)))

    # Alias pairs: an injected key that is literally another pointer local under
    # a second name. `region = local.location` is the only one in the repo.
    pointer_set = set(context.pointer_locals)
    pairs = []
    for key, value in context.auto_injected.items():
        match = _RE_LOCAL_REF.match(value.strip())
        if match and match.group(1) in pointer_set and match.group(1) != key:
            pairs.append(sorted([key, match.group(1)]))
    context.alias_pairs = sorted(pairs)

    context.module_ref = _as_text(locals_map.get("module_ref", "")).strip('"')
    context.iac_modules_repo = _as_text(
        locals_map.get("iac_modules_repo", "")).strip('"')

    context.message = "%s: %d auto-injected, %d pointer locals, module_ref=%s" % (
        os.path.basename(repo_root), len(context.auto_injected),
        len(context.pointer_locals), context.module_ref or "?")
    return context


def _merge_blocks(data: Dict, block_type: str) -> Dict:
    """`locals`/`inputs` may appear as a dict or a list of dicts."""
    node = data.get(block_type)
    merged: Dict = {}
    if isinstance(node, dict):
        merged.update(node)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                merged.update(item)
    merged.pop("__is_block__", None)
    return merged


def _as_text(value) -> str:
    if isinstance(value, str):
        return value
    return "" if value is None else str(value)


def find_unit_terragrunt(repo_root: str, unit_path: str) -> Optional[str]:
    """Path to `<repo>/<app>/<Resource>[/<variant>]/terragrunt.hcl`.

    `unit_path` is case-sensitive and is not normalised -- ADR 0004.
    """
    candidate = os.path.join(repo_root, *unit_path.split("/"), "terragrunt.hcl")
    return candidate if os.path.isfile(candidate) else None


def discover_units(repo_root: str) -> List[str]:
    """Every `<app>/<Resource>[/<variant>]` holding a terragrunt.hcl."""
    found = []
    skip = {".git", ".github", "docs"}
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        if "terragrunt.hcl" in filenames:
            relative = os.path.relpath(dirpath, repo_root).replace("\\", "/")
            if relative != ".":
                found.append(relative)
    return sorted(found)
