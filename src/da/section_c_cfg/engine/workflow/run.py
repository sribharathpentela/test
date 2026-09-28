#!/usr/bin/env python3
"""
run.py
------
The one way to run Section C: validate a request, compute the edit, stage it,
and report the pull request it would have opened.

This was two entry points, `main.py` and `github_pr_flow.py`. The split was a
safety line -- `main.py` had no code path to a GitHub write and
`github_pr_flow.py` did, so the safe one was the default and writing took a
second, explicit command. **Writes were removed on 2026-09-26, and with them
the reason for the split.** Two commands that differ only in whether they
compute a PR plan is a question the caller should not have to answer, so
`main.py` is gone and this is the whole surface.

Runs strictly AFTER a successful Step 4a mutation, and is a pure addition on
top of the existing pipeline: validation.py and mutation.py are reused via
mutation.mutate(), never modified or re-implemented here.

    GitHub API -> retrieve target file -> extract target field
        -> deterministic modification            (unchanged: Steps 1-4a)
        -> validate each modified file             (re-parse + field check — no LLM, no kustomize)
        -> build the plan: branch, per-file commit messages, PR title, PR body
        -> report it                               (--execute), or print it and stop

**Writes were removed 2026-09-26** while the agent is in development. There is
no HTTP client in this module, so `--execute` produces a simulated result
carrying the full plan -- it cannot reach GitHub whatever it is passed. See
the removed-writes note further down.

GOVERNANCE — one logical change is one branch and one PR. When target_fields
mirrors the same logical field into more than one file (e.g. "Target port
change" also patching envoy-filter.yaml's listener.portNumber), every file is
part of the same request: identified together, validated together, and so
branched and raised together. They are never split into per-file branches or
PRs. Any failed step stops immediately.

    (Historical note: the paragraph removed from here described the opposite —
    an independent branch and PR per file, "never bundled into one PR". That
    has not matched `_process_logical_change` for some time; the code, and the
    reasoning written into it, is what governs.)

Usage — from the `ips_agent_da` repo root:
    python -m src.da.section_c_cfg.engine.workflow.run
        # prints the plan and stops
    python -m src.da.section_c_cfg.engine.workflow.run --execute
        # simulated PR result: "Raised PR with these details — In development Phase"

For a Jira ticket rather than an `input.json`, use `run_da.py` at the repo
root: it routes the ticket to Section A or Section C and calls this.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

# `requests` and `base64` are deliberately absent -- see the removed-writes
# note below. This module has no HTTP client, which is what makes the
# simulated PR safe rather than merely disabled.

from ..core.config import SAMPLE_REQUEST, _RULE, use_utf8_console
from ..core.exceptions import ValidationToolError
from ..core.github import _get_default_branch
from ..processing.mutation import _load_yaml_documents, _select_mutation_document, _parse_structured, _values_match, mutate, print_mutation_result

# The commit trailer and PR footer that used to live here named the tool that
# wrote the code. They were removed on 2026-09-26: a pull request raised against
# a Honeywell repository is a Honeywell artifact, and the reviewer needs to know
# what changed and what checked it, not which editor produced the agent. The
# trailer was also simply wrong -- it credited a model that had not run.

DEV_PHASE_PR_MESSAGE = "Raised PR with these details — In development Phase"


class GitHubFlowError(ValidationToolError):
    """Step 5 could not safely proceed; nothing further was branched/committed/PR'd."""


# ─────────────────────────────────────────────────────────────────────────
# GitHub writes — REMOVED 2026-09-26, development phase.
#
# This block held `_github_json_request`, `get_base_branch_sha`,
# `create_feature_branch`, `get_file_sha_on_branch`, `update_file_on_branch`
# and `create_pull_request` -- the only code in the whole agent that could
# write to a real repository. Section A has never had a write path: it renders
# a `terragrunt.hcl` and a PR body and stops. Section C now matches it, so the
# agent is uniformly read-only and `--execute` simulates the PR instead
# (`_simulate_pr` below).
#
# They were the sole users of `requests` and `base64` in this file, so both
# imports went with them: there is no HTTP client left here to write with, and
# restoring the behaviour means restoring the transport too rather than
# flipping a flag. Everything up to the PR -- validation, mutation, the
# post-mutation re-parse, the branch name, the PR title and body -- still runs
# exactly as before, because that is what the simulated result reports.
# ─────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────
# Deterministic helpers — no LLM, no kustomize/kubectl (both out of scope).
# ─────────────────────────────────────────────────────────────────────────

def _git_ref_slug(text: str) -> str:
    """Sanitizes text for use inside a git branch name: lowercase,
    alphanumerics and hyphens only, no leading/trailing/doubled hyphens."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug) or "change"


def _run_suffix() -> str:
    """A short timestamp suffix so repeated runs for the same service don't
    collide on the same branch name."""
    return time.strftime("%Y%m%dT%H%M%S")


def validate_modified_file(
    target_file: str, mutated_text: str, target_path: list[Any], expected_new_value: Any, kind: Optional[str] = None
) -> None:
    """Deterministic sanity check before committing: the mutated text must
    still parse cleanly, and the target field must now hold the expected
    new value. This is NOT a kustomize/kubectl build (explicitly out of
    scope) — just confirmation the surgical edit is syntactically sound
    and landed on the right field. `kind` re-selects the correct document
    out of a file that bundles more than one (the ingress config's
    VirtualService + Gateway) — without it, re-parsing a multi-document
    file with the plain single-document parser would itself raise, which
    is exactly the failure this check exists to catch correctly, not
    trigger as a false positive."""
    try:
        if kind:
            documents = _load_yaml_documents(mutated_text)
            parsed = _select_mutation_document(target_file, documents, kind)
        else:
            parsed = _parse_structured(target_file, mutated_text)
    except Exception as exc:
        raise GitHubFlowError(f"Modified file '{target_file}' failed to re-parse after mutation: {exc}") from exc
    node = parsed
    for segment in target_path:
        try:
            node = node[segment]
        except (KeyError, IndexError, TypeError) as exc:
            raise GitHubFlowError(f"Modified file '{target_file}' is missing field {target_path} after mutation: {exc}") from exc
    if not _values_match(node, expected_new_value):
        raise GitHubFlowError(
            f"Modified file '{target_file}' field {target_path} = {node!r}, expected {expected_new_value!r} — refusing to commit."
        )




def build_pr_description(
    mutation_result: dict[str, Any], input_request: dict[str, Any], changes_by_file: dict[str, list[dict[str, Any]]]
) -> str:
    """One PR body for the entire logical configuration change — every
    changed file (there may be just one, or several when the request has
    required dependent files, e.g. the Service-port <-> VirtualService
    dependency) and every field within it is listed under the same PR
    body, in the order the files were changed. GOVERNANCE: one logical
    change is always exactly one PR — dependent files are never split
    into separate PRs (see run_config_change).

    Written for the engineer reviewing the PR: what changed, where, and what
    was checked before it was proposed."""
    matched = mutation_result["validation"].get("matched_change")
    lines = [
        "## Configuration Change",
        f"- **Type:** {matched}",
        f"- **Service:** {input_request.get('service')}",
        f"- **Environment:** {input_request.get('environment')}",
        "",
        "## Changes",
    ]
    for target_file, changes in changes_by_file.items():
        lines.append(f"- **File:** `{target_file}`")
        for change in changes:
            lines.append(f"  - **Field:** `{'.'.join(str(p) for p in change['field_path'])}`")
            lines.append(f"    - **Value:** `{change['previous_value']!r}` -> `{change['new_value']!r}`")
    pending = mutation_result["validation"].get("pending_agent_verification") or []
    if pending:
        lines += ["", "## Pending manual verification (could not be checked deterministically)"]
        lines += [f"- {p}" for p in pending]
    lines += [
        "",
        "## Checks",
        "",
        f"Matched to **{matched}** in `config_changes.json`. Every pre-check "
        "that entry declares was run before the file was touched.",
        "",
        "The edit is one field, at the path that entry fixes. The file was "
        "re-parsed afterwards and the field confirmed to hold the new value.",
    ]
    if len(changes_by_file) > 1:
        lines += [
            "",
            f"The {len(changes_by_file)} files above are one change, not "
            "several: `config_changes.json` declares them interdependent, they "
            "were validated against each other, and merging one without the "
            "others would leave the change half applied.",
        ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────

def _process_logical_change(
    changes_by_file: dict[str, list[dict[str, Any]]], mutation_result: dict[str, Any], input_request: dict[str, Any],
    service: str, environment: str, matched_change: Optional[str], run_suffix: str, execute: bool,
) -> dict[str, Any]:
    """GOVERNANCE — one logical change, atomically: every file changed by
    ONE validate()+mutate() run (there may be just one, or several when
    the request has required dependent files — e.g. Service port change
    also touching the VirtualService ingress config, or Target/Container
    port change also touching each other and envoy-filter.yaml) is
    committed to ONE branch and raised as ONE PR. These files are never
    independent edits from this pipeline's point of view — they were
    identified together as one logical request, validated together
    (including the dependent-field consistency checks in
    validate_configuration_change.py), and must be reviewed/merged
    together: merging one without the other would leave the change only
    half-applied. ONE branch, ONE commit per file on that SAME branch (in
    file order), ONE PR covering every file — never split into per-file
    branches/PRs, regardless of how many files are involved."""
    slug = _git_ref_slug(matched_change or "config-change")
    branch_name = f"feature/config-{_git_ref_slug(service)}-{slug}-{run_suffix}"

    print(f"\n  branch          {branch_name} (one PR for all {len(changes_by_file)} file(s) in this logical change)")
    for target_file, changes in changes_by_file.items():
        print(f"\n  file            {target_file}")
        for change in changes:
            print(f"  field path      {change['field_path']}")
            print(f"  value change    {change['previous_value']!r} -> {change['new_value']!r}")

    for target_file, changes in changes_by_file.items():
        mutated_text = Path(changes[0]["output_path"]).read_text()
        try:
            for change in changes:
                validate_modified_file(target_file, mutated_text, list(change["field_path"]), change["new_value"], kind=change.get("kind"))
        except GitHubFlowError as exc:
            print(f"\n  validation      FAILED ({target_file}): {exc}")
            return {"file": "multiple", "files": list(changes_by_file), "branch": branch_name, "status": "FAILED", "reason": str(exc)}
    print("\n  validation      OK for every file (re-parsed cleanly; every field matches its expected new value)")

    # The plan is computed identically whether or not it is acted on, so a dry
    # run and a simulated PR report the same facts and neither is a rehearsal
    # of something the other would do differently.
    plan = _build_pr_plan(changes_by_file, mutation_result, input_request,
                          service, environment, matched_change, branch_name)

    if not execute:
        print("  DRY RUN — the plan above was computed; nothing was simulated or created.")
        return {
            "file": "multiple", "files": list(changes_by_file), "branch": branch_name,
            "status": "DRY_RUN", "plan": plan,
            "reason": "Pass --execute to produce the simulated PR result.",
        }

    return _simulate_pr(changes_by_file, branch_name, plan)


def _build_pr_plan(
    changes_by_file: dict[str, list[dict[str, Any]]], mutation_result: dict[str, Any],
    input_request: dict[str, Any], service: str, environment: str,
    matched_change: Optional[str], branch_name: str,
) -> dict[str, Any]:
    """Everything the removed write path would have sent to GitHub.

    Reporting the real plan is the point. "PR raised" on its own proves
    nothing; showing the exact branch, files, commit messages, title and body
    shows the agent knew precisely what it would have done -- and that it
    stopped anyway.

    The base branch is a read, so it is attempted and allowed to fail: a
    simulation must not require a token it never spends.
    """
    try:
        base_branch = _get_default_branch()
    except Exception:
        base_branch = "(unresolved — no GitHub access in this run)"

    return {
        "branch": branch_name,
        "base_branch": base_branch,
        "files": list(changes_by_file),
        "commit_messages": [
            f"config: {matched_change} for {service} ({target_file})"
            for target_file in changes_by_file
        ],
        "pr_title": f"{matched_change}: {service} ({environment})",
        "pr_body": build_pr_description(mutation_result, input_request, changes_by_file),
    }


def _simulate_pr(changes_by_file: dict[str, list[dict[str, Any]]],
                 branch_name: str, plan: dict[str, Any]) -> dict[str, Any]:
    """Stands in for Step 5 while the agent is read-only.

    Not a flag-guarded skip over live code -- the live code is gone. There is
    no HTTP client in this module to reach GitHub with, so this cannot become a
    real PR by accident, by a stray `--execute`, or by a caller passing the
    wrong argument.
    """
    print(f"\n  {DEV_PHASE_PR_MESSAGE}")
    print(f"  branch          {plan['branch']}  (base: {plan['base_branch']})")
    for target_file in plan["files"]:
        print(f"  would commit    {target_file}")
    print(f"  PR title        {plan['pr_title']}")

    return {
        "file": "multiple", "files": list(changes_by_file), "branch": branch_name,
        "status": "SIMULATED", "message": DEV_PHASE_PR_MESSAGE, "plan": plan,
        "pr_url": None, "commit_shas": [],
    }


def run_config_change(input_request: dict[str, Any], verbose: bool = False, execute: bool = False) -> dict[str, Any]:
    """Runs the existing, unmodified Steps 1-4a via mutate_configuration_change.mutate(),
    then — only if that mutation COMPLETED — performs Step 5. GOVERNANCE:
    one logical configuration request is always exactly one feature
    branch, one PR — every file the mutation touched (there may be just
    one, or several when the request has required dependent files) is
    committed together and raised together; this pipeline never creates
    separate branches/commits/PRs for files belonging to the same
    request, since by construction anything that had to be identified
    and validated together also has to be reviewed and merged together.
    Reads each mutated file's content from the local mutation_output/
    path mutate() already wrote, but pushes it to the file's real
    repo-relative path, never to a path derived from the local
    mutation_output/ folder structure."""
    mutation_result = mutate(input_request, verbose=verbose)
    print_mutation_result(mutation_result)

    if mutation_result["mutation"]["status"] != "COMPLETED":
        return {
            **mutation_result,
            "github": {
                "status": "SKIPPED",
                "reason": f"Mutation status was {mutation_result['mutation']['status']}, not COMPLETED.",
            },
        }

    # Group by file: "both" can put more than one field change (e.g.
    # maxSurge and maxUnavailable) into the same file, and a required
    # dependent field can land in its own separate file — either way,
    # every file below is part of this ONE logical change.
    changes_by_file: dict[str, list[dict[str, Any]]] = {}
    for change in mutation_result["mutation"]["files"]:
        changes_by_file.setdefault(change["file"], []).append(change)

    service = str(input_request.get("service", "service"))
    environment = input_request.get("environment", "")
    matched_change = mutation_result["validation"].get("matched_change")
    run_suffix = _run_suffix()

    print(f"\n{_RULE}")
    print(f"STEP 5 — GitHub Branch / Commit / PR (1 branch, 1 PR for all {len(changes_by_file)} file(s) in this logical change)")
    print(_RULE)

    result = _process_logical_change(changes_by_file, mutation_result, input_request, service, environment, matched_change, run_suffix, execute)

    return {**mutation_result, "github": {"status": result["status"], "results": [result]}}


def print_result(result: dict[str, Any]) -> None:
    github = result["github"]
    print(f"\n{_RULE}")
    print("Step 5 Result")
    print(_RULE)
    print(f"GITHUB:  {github['status']}")

    if github["status"] == "SKIPPED":
        print(f"  Reason: {github['reason']}")
        return

    for r in github["results"]:
        if r["file"] == "multiple":
            print(f"\n  files     {', '.join(r['files'])}")
        else:
            print(f"\n  file      {r['file']}")
        print(f"  branch    {r['branch']}")
        if r["status"] == "SIMULATED":
            plan = r["plan"]
            print(f"  status    SIMULATED — {r['message']}")
            print(f"  base      {plan['base_branch']}")
            for target_file, message in zip(plan["files"], plan["commit_messages"]):
                print(f"  commit    {target_file} — {message.splitlines()[0]}")
            print(f"  PR title  {plan['pr_title']}")
            print("\n  PR body")
            for line in plan["pr_body"].splitlines():
                print(f"    {line}")
        elif r["status"] == "DRY_RUN":
            print(f"  status    DRY_RUN — {r['reason']}")
        else:  # FAILED
            print(f"  status    FAILED — {r['reason']}")


def main() -> int:
    use_utf8_console()
    # argparse rather than reading sys.argv directly. The old hand-rolled parse
    # treated every unrecognised flag as the input filename, so `--help` was
    # read as a path and the command reported that it could not find a file
    # called `--help`.
    parser = argparse.ArgumentParser(
        prog="python -m src.da.section_c_cfg.engine.workflow.run",
        description="Validate a configuration-change request, stage the edit, "
                    "and report the pull request it would have opened. This "
                    "agent has no write path to any repository.")
    parser.add_argument("input_file", nargs="?", default=str(SAMPLE_REQUEST),
                        help="the request to process (default: the sample in "
                             "src/da/samples/)")
    parser.add_argument("--execute", action="store_true",
                        help="report the simulated PR result. Named for the flag "
                             "that used to open a real PR; it no longer can.")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="print the step-by-step trace")
    args = parser.parse_args()

    execute, verbose = args.execute, args.verbose
    input_path = Path(args.input_file)
    if not input_path.exists():
        print(f"[ERROR] Input file not found: {input_path}", file=sys.stderr)
        return 3

    try:
        input_request = json.loads(input_path.read_text())
        result = run_config_change(input_request, verbose=verbose, execute=execute)
    except ValidationToolError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3
    except json.JSONDecodeError as exc:
        print(f"[ERROR] {input_path} is not valid JSON: {exc}", file=sys.stderr)
        return 3

    print_result(result)

    status_map = {"SIMULATED": 0, "DRY_RUN": 0, "SKIPPED": 2, "PARTIAL": 1, "FAILED": 1}
    return status_map.get(result["github"]["status"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
