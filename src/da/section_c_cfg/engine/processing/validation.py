#!/usr/bin/env python3
"""
validation.py
--------------
Steps 1-3 + pre-checks: matches input.json against config_changes.json
(via llm_hitl.py), deterministically extracts and cross-checks the
declared target_fields against the real GitOps repo (via github.py), and
evaluates every pre_check and cross-file dependency rule. Produces
VALID / INVALID / INCOMPLETE / CONFLICT. Nothing here ever writes to the
repo — that's mutation.py's job, once this returns VALID.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Optional

from ruamel.yaml import YAML
from ruamel.yaml.composer import ComposerError

from ..core.config import (
    CONFIG_CHANGES_FILE, GITOPS_REPO, _slugify, _coerce, _RULE, _step, _kv, _item, _table,
    _is_valid_k8s_quantity, _is_percentage_string, _parse_k8s_quantity, _numeric_in_range, _RANGE_IN_TEXT_RE,
    _quantity_numeric_and_unit, _QUANTITY_UNITS,
)
from ..core.exceptions import ValidationToolError, ConflictError, GitOpsAccessError
from ..core.github import _fetch_file_content, check_target_existence, _resolve_pinned_ref, fetch_target_manifests
from ..core.llm_hitl import (
    match_change_candidates, _deterministic_entry_candidates, check_human_requirements,
    prompt_for_change_disambiguation, _deterministic_satisfy_desired_value, _deterministic_satisfy_option_requirements,
    _find_value_by_keyword, _match_field_key, _is_both_answer, prompt_for_field_clarification, _llm_pick,
    _FIELD_SELECTION_SYSTEM_PROMPT, _NON_SELECTOR_KEYS, _hint_match_for_candidates,
    prompt_for_array_disambiguation, prompt_for_cpu_quantity_qualifier, prompt_for_quantity_unit,
    prompt_for_missing_requirements,
)

_yaml_read = YAML(typ="safe")  # read-only parsing


def _row(check: str, result: str, derivation: str, reason: str) -> dict[str, Any]:
    """One pre-check/dependency-check result — the uniform 4-key shape
    every check-evaluation function below produces, shared here so each
    check site is one call instead of a repeated dict literal."""
    return {"check": check, "result": result, "derivation": derivation, "reason": reason}


# Deterministic target_fields extraction — no LLM. When config_changes.json
# declares target_fields, we know exactly which file(s)/key path(s) hold
# the value(s) this request needs, so a plain parse+walk replaces LLM
# discovery entirely: fewer files fetched, no content sent to Groq, a
# precise typed value instead of an LLM paraphrase.

def _get_field(data: Any, path: list[Any], default: Any = None) -> Any:
    """Walks a parsed YAML/JSON structure; returns `default` if any segment is missing."""
    node = data
    for segment in path:
        try:
            node = node[segment]
        except (KeyError, IndexError, TypeError):
            return default
    return node


def _parse_yaml_or_json(path: str, text: str) -> Any:
    if path.endswith((".yaml", ".yml")):
        try:
            return _yaml_read.load(text)
        except ComposerError as exc:
            # File genuinely bundles multiple '---'-separated YAML documents,
            # but the requesting target_fields spec declares no "kind" — a
            # config_changes.json authoring gap, not a code defect. Never
            # silently guess which document was meant.
            raise ValidationToolError(
                f"'{path}' contains multiple YAML documents (separated by '---'), but the target_fields "
                f"spec requesting it declares no \"kind\" — add \"kind\": \"<the intended document's Kind>\" "
                f"to that spec in config_changes.json. Underlying parser error: {exc}"
            ) from exc
    if path.endswith(".json"):
        return json.loads(text)
    return None


def _parse_yaml_documents(path: str, text: str) -> list[Any]:
    """Like _parse_yaml_or_json, but for a file that may bundle more than
    one YAML document (real in this repo's ingress config: a VirtualService
    + a Gateway in one file) — returns every document, in order."""
    if path.endswith((".yaml", ".yml")):
        return [doc for doc in _yaml_read.load_all(text) if doc is not None]
    if path.endswith(".json"):
        return [json.loads(text)]
    return []


def _select_document(documents: list[Any], kind: str) -> Optional[Any]:
    """Picks the one document whose own 'kind' matches — never "the first
    one" by position. Returns None if no document has that kind."""
    for doc in documents:
        if isinstance(doc, dict) and doc.get("kind") == kind:
            return doc
    return None


# Array-position disambiguation — NO SILENT ASSUMPTIONS. A target_fields path may contain this sentinel in place of a literal array index (e.g. a Service's spec.ports[N]) wherever config_changes.json cannot know in advance which element the request means.

_ARRAY_INDEX_SENTINEL = "<selected_index>"


def _resolve_array_index_candidates(array: Any, sub_path: list[Any]) -> list[dict[str, Any]]:
    """{"index", "name", "value"} for each array element where sub_path
    resolves to a value (i.e. is "applicable" to this field) — an element
    where it doesn't resolve is never presented as a choice."""
    if not isinstance(array, list):
        return []
    candidates = []
    for i, element in enumerate(array):
        value = _get_field(element, sub_path)
        if value is None:
            continue
        name = element.get("name") if isinstance(element, dict) else None
        candidates.append({"index": i, "name": name, "value": value})
    return candidates


# Service-port <-> VirtualService dependency (Service port change only) — NO SILENT ASSUMPTIONS. Identifying which route(s) belong to a given Service is done ONLY by identity (the Service's own name against the route's destination.host), never by port number (circular — that's the value that might be changing) and never by array position.

_ROUTE_INDEX_SENTINEL = "<matching_route_index>"
_DESTINATION_INDEX_SENTINEL = "<matching_destination_index>"


def _resolve_route_matches(vs_doc: Any, service_name: str, key_path: list[Any]) -> list[dict[str, Any]]:
    """Every spec.http[].route[] entry whose destination.host's OWN
    leading DNS label equals service_name (e.g. Service "cf-mv-ocr"
    matches host "cf-mv-ocr.machinevision.svc.cluster.local" regardless
    of namespace). A Service can legitimately be routed by more than one
    rule, or none at all — returns every match, never just one."""
    route_idx_pos = key_path.index(_ROUTE_INDEX_SENTINEL)
    dest_idx_pos = key_path.index(_DESTINATION_INDEX_SENTINEL)
    http_array = _get_field(vs_doc, key_path[:route_idx_pos])
    if not isinstance(http_array, list):
        return []
    matches = []
    for i, http_entry in enumerate(http_array):
        routes = http_entry.get("route", []) if isinstance(http_entry, dict) else []
        if not isinstance(routes, list):
            continue
        for j, route in enumerate(routes):
            host = _get_field(route, ["destination", "host"])
            if not isinstance(host, str) or host.split(".")[0] != service_name:
                continue
            concrete_path = list(key_path)
            concrete_path[route_idx_pos] = i
            concrete_path[dest_idx_pos] = j
            matches.append({"path": concrete_path, "host": host, "port": _get_field(vs_doc, concrete_path)})
    return matches


def _evaluate_service_port_virtualservice_dependency(
    known_values: dict[str, Any], manifest_files: dict[str, dict[str, Any]], target_fields: list[dict[str, Any]],
    ref: Optional[str] = None,
) -> tuple[list[dict[str, Any]], Optional[list[dict[str, Any]]]]:
    """Triggered only when this entry's target_fields declares
    _ROUTE_INDEX_SENTINEL. Verifies every matching VirtualService route's
    CURRENT destination port already agrees with the Service's CURRENT
    port before any change — a pre-existing mismatch is reported and
    refused, never silently corrected. Returns (pre_check_rows,
    route_matches); route_matches is None when this entry declares no
    such dependency at all."""
    for spec in target_fields:
        for key, key_path in spec.get("fields", {}).items():
            if _ROUTE_INDEX_SENTINEL not in key_path:
                continue
            file_path = next((p for p in manifest_files if p == spec["file"] or p.endswith(f"/{spec['file']}")), None)
            if file_path is None:
                return [], None  # file wasn't fetched (e.g. no cluster mapped) — nothing to verify
            kind = spec.get("kind")
            docs = _parse_yaml_documents(file_path, manifest_files[file_path]["content"])
            vs_doc = _select_document(docs, kind) if kind else (docs[0] if docs else None)
            if vs_doc is None:
                return [], None

            # The Service's own spec (any OTHER spec in this entry, never
            # the route-matched one) — resolved generically, never a
            # hardcoded filename, purely to name what was read in the trace.
            service_spec = next(
                (s for s in target_fields if not any(_ROUTE_INDEX_SENTINEL in p for p in s.get("fields", {}).values())),
                None,
            )
            service_file_path = None
            if service_spec is not None:
                service_file_path = next(
                    (p for p in manifest_files if p == service_spec["file"] or p.endswith(f"/{service_spec['file']}")), None
                )
            service_resource = None
            if service_file_path is not None:
                try:
                    service_resource = _parse_yaml_or_json(service_file_path, manifest_files[service_file_path]["content"]).get("metadata", {}).get("name")
                except Exception:
                    service_resource = None
            vs_resource = vs_doc.get("metadata", {}).get("name") if isinstance(vs_doc, dict) else None

            # If the Service has multiple ports, resolution already picked
            # the sole applicable one or blocked on HITL earlier (an
            # unresolved case returns INCOMPLETE before pre-checks run).
            selection_row = _row(
                "If the Service has multiple ports, the intended Service port must be explicitly identified",
                "passed",
                f"service_port={known_values.get('service_port')!r} was resolved (uniquely applicable, or confirmed via HITL) before pre-checks ran",
                "an unresolved/ambiguous port selection would have already stopped the request as INCOMPLETE",
            )

            service_name = str(known_values.get("service", ""))
            matches = _resolve_route_matches(vs_doc, service_name, key_path)
            resolve_row = _row(
                "For the selected Service port, resolve all matching VirtualService routes in the environment ingress configuration",
                "passed",
                f"Service: resource={service_resource!r} file={service_file_path!r} ref={ref!r} | "
                f"VirtualService: resource={vs_resource!r} file={file_path!r} ref={ref!r} — "
                f"matched {len(matches)} route(s) whose destination host resolves to service={service_name!r}: "
                + (", ".join(f"host={m['host']!r} route_index={m['path'][key_path.index(_ROUTE_INDEX_SENTINEL)]}" for m in matches) or "(none)"),
                f"found {len(matches)} matching route(s)" if matches
                else "this Service is not routed through this ingress configuration — nothing to reconcile",
            )
            if not matches:
                return [
                    selection_row,
                    resolve_row,
                    _row(
                        "Current Service port must match the corresponding VirtualService destination port before applying the change",
                        "passed", "no matching VirtualService routes found",
                        "nothing to verify — this Service has no VirtualService dependency",
                    ),
                    _row(
                        "If the Service port and VirtualService destination port are already inconsistent, stop for review and do not automatically correct the existing mismatch",
                        "passed", "no matching VirtualService routes found",
                        "no dependency, nothing to be inconsistent with",
                    ),
                ], []

            # Normalized (via _coerce) comparison, never raw equality — a
            # YAML int vs. a JSON string representing the same port is not
            # a real mismatch.
            current_service_port = known_values.get("service_port")
            current_service_port_n = _coerce(current_service_port) if isinstance(current_service_port, str) else current_service_port
            mismatches = [
                m for m in matches
                if (_coerce(m["port"]) if isinstance(m["port"], str) else m["port"]) != current_service_port_n
            ]

            comparison_lines = "; ".join(
                f"host={m['host']!r} route={m['path']!r} destination_port={m['port']!r} vs service_port={current_service_port!r}"
                for m in matches
            )
            consistency_row = _row(
                "Current Service port must match the corresponding VirtualService destination port before applying the change",
                "failed" if mismatches else "passed",
                f"Service: resource={service_resource!r} file={service_file_path!r} ref={ref!r} current_port={current_service_port!r} | "
                f"VirtualService: resource={vs_resource!r} file={file_path!r} ref={ref!r} | "
                f"Comparison: {comparison_lines}",
                (
                    f"Service.spec.ports[].port ({current_service_port!r}) does not match the existing "
                    f"VirtualService destination port(s) for host(s) "
                    f"{', '.join(m['host'] for m in mismatches)} — this is a pre-existing repo "
                    f"inconsistency and must be reviewed manually; it is never silently auto-corrected."
                ) if mismatches else "Service port matches every corresponding VirtualService destination port",
            )
            stop_row = _row(
                "If the Service port and VirtualService destination port are already inconsistent, stop for review and do not automatically correct the existing mismatch",
                "passed", "governance rule itself — enforced by the 'failed' result above, never bypassed",
                "halting for manual review, as required" if mismatches else "no inconsistency found — nothing to stop for",
            )
            return [selection_row, resolve_row, consistency_row, stop_row], (None if mismatches else matches)
    return [], None


def _evaluate_mirrored_field_consistency(
    manifest_files: dict[str, dict[str, Any]], target_fields: list[dict[str, Any]], pre_checks: Optional[list[str]] = None,
    ref: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Whenever the matched entry declares the SAME field key across more
    than one file (e.g. Target port change's target_port, Container port
    change's containerPort), every one of those files' CURRENT values
    must already agree before any change is applied — a pre-existing
    disagreement halts the request, never silently reconciled. Fully
    generic; a field mirrored in only one file produces no row."""
    key_to_specs: dict[str, list[dict[str, Any]]] = {}
    for spec in target_fields:
        for key, path in spec.get("fields", {}).items():
            if _ROUTE_INDEX_SENTINEL in path or _ARRAY_INDEX_SENTINEL in path:
                continue  # route-matched fields have their own dependency function; an unresolved
                # array sentinel here means it was never applicable at all
            key_to_specs.setdefault(key, []).append(spec)

    rows = []
    for key, specs in key_to_specs.items():
        if len(specs) < 2:
            continue
        values: list[tuple[str, Any]] = []
        for spec in specs:
            file_path = next((p for p in manifest_files if p == spec["file"] or p.endswith(f"/{spec['file']}")), None)
            if file_path is None:
                continue
            parsed = _parse_yaml_or_json(file_path, manifest_files[file_path]["content"])
            value = _get_field(parsed, spec["fields"][key])
            if value is not None:
                values.append((spec["file"], value))
        if len(values) < 2:
            continue  # fewer than 2 files actually resolved this field for this service — nothing to compare
        distinct = {_coerce(v) if isinstance(v, str) else v for _, v in values}
        derivation = f"ref={ref!r} | " + ", ".join(f"{f}={v!r}" for f, v in values)
        check_text = f"Dependent field '{key}' must already be consistent across all required files before applying the change"
        if len(distinct) > 1:
            rows.append(_row(
                check_text, "failed", derivation,
                f"'{key}' has different current values across its required files ({derivation}) — this is "
                f"a pre-existing repo inconsistency and must be reviewed manually; it is never silently "
                f"corrected as a side effect of applying the requested change.",
            ))
        else:
            rows.append(_row(check_text, "passed", derivation, "consistent"))

    # This entry may also declare its own human-readable pre_check text
    # describing the same rule; that text can't be algorithmically tied to
    # one field key, so any declared pre_check mentioning "consistent" is
    # resolved here too (aggregate of the rows above), instead of falling
    # through to the generic "cannot_verify" fallback.
    if pre_checks and rows:
        any_failed = any(r["result"] == "failed" for r in rows)
        produced_checks = {r["check"] for r in rows}
        for text in pre_checks:
            if text in produced_checks or "consistent" not in text.lower():
                continue
            rows.append(_row(
                text, "failed" if any_failed else "passed",
                "see the per-field 'Dependent field ... must already be consistent' result(s) above",
                "a required dependent field was already inconsistent — halting, per this rule" if any_failed
                else "every required dependent field is already consistent",
            ))
    return rows


# Base vs. environment-overlay authority — NO SILENT ASSUMPTIONS. A "base" file is not always what's actually deployed: an environment's own kustomization.yaml can patch specific fields on top of it. Every target_fields entry declared "in": "base" is checked against the environment's real, declared patches.

def _parse_kustomization_patches(kustomization_text: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Returns (file_patch_paths, inline_json6902_patches) declared by an
    environment's kustomization.yaml. file_patch_paths come from
    'patchesStrategicMerge' and any plain-string 'patches' entry — a
    separate strategic-merge file applied on top of base.
    inline_json6902_patches come from any 'patches' entry that embeds RFC
    6902 operations directly (no separate file)."""
    try:
        data = _yaml_read.load(kustomization_text) or {}
    except Exception:
        return [], []
    file_paths: list[str] = []
    inline: list[dict[str, Any]] = []
    for entry in data.get("patchesStrategicMerge") or []:
        if isinstance(entry, str):
            file_paths.append(entry)
    for entry in data.get("patches") or []:
        if isinstance(entry, str):
            file_paths.append(entry)
        elif isinstance(entry, dict):
            if isinstance(entry.get("path"), str):
                file_paths.append(entry["path"])
            elif isinstance(entry.get("patch"), str) and isinstance(entry.get("target"), dict):
                inline.append(entry)
    return file_paths, inline


_STRATEGIC_MERGE_KEY = "name"  # Kubernetes' own default mergeKey for containers/volumes/ports/etc.


def _resolve_strategic_merge_path(patch_node: Any, base_node: Any, path: list[Any]) -> tuple[bool, Any]:
    """Walks `path` through `patch_node` IN PARALLEL with `base_node`,
    because a strategic-merge patch is a PARTIAL document: a list inside
    it is matched to base's list by the item's own "name", never by raw
    array position. Returns (found, value): found=False as soon as the
    patch simply doesn't reach this deep — base remains authoritative."""
    p, b = patch_node, base_node
    for segment in path:
        if isinstance(segment, int):
            if not isinstance(b, list) or not (0 <= segment < len(b)):
                return False, None
            base_item = b[segment]
            if isinstance(base_item, dict) and _STRATEGIC_MERGE_KEY in base_item:
                if not isinstance(p, list):
                    return False, None
                match_name = base_item[_STRATEGIC_MERGE_KEY]
                patch_item = next(
                    (item for item in p if isinstance(item, dict) and item.get(_STRATEGIC_MERGE_KEY) == match_name), None
                )
                if patch_item is None:
                    return False, None
                p, b = patch_item, base_item
            else:
                if not isinstance(p, list) or not (0 <= segment < len(p)):
                    return False, None
                p, b = p[segment], base_item
        else:
            if not isinstance(b, dict) or segment not in b:
                return False, None
            if not isinstance(p, dict) or segment not in p:
                return False, None
            p, b = p[segment], b[segment]
    return True, p


def _to_json_pointer(path: list[Any]) -> str:
    return "/" + "/".join(str(p) for p in path)


def _find_inline_json6902_override(inline_patches: list[dict[str, Any]], kind: Optional[str], path: list[Any]) -> Optional[dict[str, Any]]:
    """Matches by the patch's own declared target.kind and the operation's
    path (as a JSON Pointer). Returns the matching operation, so the
    caller can report exactly what overrides it — never attempts to
    rewrite a patch embedded as a YAML string inside kustomization.yaml
    (too high a risk of corrupting hand-authored formatting there)."""
    if kind is None:
        return None
    pointer = _to_json_pointer(path)
    for entry in inline_patches:
        if entry.get("target", {}).get("kind") != kind:
            continue
        try:
            ops = _yaml_read.load(entry["patch"]) or []
        except Exception:
            continue
        if not isinstance(ops, list):
            continue
        for op in ops:
            if isinstance(op, dict) and op.get("path") == pointer:
                return op
    return None


def extract_target_fields(
    target_fields: list[dict[str, Any]],
    base_path: str,
    environment_path: str,
    hint_source: Optional[dict[str, Any]] = None,
    service: str = "",
    environment: str = "",
    cluster: Optional[str] = None,
    ref: Optional[str] = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Fetches only the files target_fields declares and extracts only the
    declared key paths. Returns (extracted_values, manifest_files,
    unresolved, patch_overrides, inline_overrides).

    Base vs. overlay: for every field declared "in": "base", also checks
    whether the environment's kustomization.yaml overrides that SAME
    field. A field overridden by a separate strategic-merge FILE is
    transparently redirected (extracted[key] becomes the patch's real,
    deployed value; patch_overrides[key] records where to write the
    change instead of base). A field overridden by an INLINE RFC 6902
    patch is never auto-redirected — reported in inline_overrides for the
    caller to halt on.

    "in" is "base"/"environment" for every existing entry, OR a raw path
    template containing <cluster>/<environment>/<service> for an entry
    that reaches outside the service's own directories entirely (e.g. a
    shared ingress config keyed by cluster).

    A field path may contain _ARRAY_INDEX_SENTINEL, resolved here ONLY
    deterministically (sole applicable entry, or an unambiguous hint);
    anything genuinely ambiguous is left in `unresolved` for the caller
    to send to HITL. Once resolved, the spec's field-path list is mutated
    IN PLACE so every later consumer reuses the same resolved index.

    A field path containing _ROUTE_INDEX_SENTINEL is deliberately SKIPPED
    here — it can resolve to zero/one/many concrete paths at once, so
    it's handled by _resolve_route_matches instead.
    """
    fetched: dict[str, str] = {}  # local cache: never fetch the same file twice in one run
    manifest_files: dict[str, dict[str, Any]] = {}
    extracted: dict[str, Any] = {}
    unresolved: list[dict[str, Any]] = []
    patch_overrides: dict[str, dict[str, Any]] = {}
    inline_overrides: list[dict[str, Any]] = []

    env_patches_loaded = False
    env_patch_docs: dict[str, Any] = {}
    env_inline_patches: list[dict[str, Any]] = []

    def _ensure_env_patches_loaded() -> None:
        nonlocal env_patches_loaded, env_patch_docs, env_inline_patches
        if env_patches_loaded:
            return
        env_patches_loaded = True
        kustomization_text = _fetch_file_content(f"{environment_path}/kustomization.yaml", ref=ref)
        if kustomization_text is None:
            return
        file_paths, env_inline_patches = _parse_kustomization_patches(kustomization_text)
        for name in file_paths:
            rel = name[2:] if name.startswith("./") else name
            patch_path = f"{environment_path}/{rel}"
            content = _fetch_file_content(patch_path, ref=ref)
            if content is None:
                continue
            manifest_files[patch_path] = {"content": content, "label": "environment overlay patch (deterministic)"}
            try:
                env_patch_docs[patch_path] = _yaml_read.load(content)
            except Exception:
                continue

    for spec in target_fields:
        in_value = spec.get("in", "base")
        if in_value == "base":
            dir_path = base_path
        elif in_value == "environment":
            dir_path = environment_path
        else:
            dir_path = _substitute_placeholders(in_value, service, environment, cluster)
            if dir_path is None:
                continue  # needs <cluster> but none mapped for this environment — skip, not an error
        file_path = f"{dir_path}/{spec['file']}"

        if file_path not in fetched:
            content = _fetch_file_content(file_path, ref=ref)
            if content is None:
                continue
            fetched[file_path] = content
            manifest_files[file_path] = {"content": content, "label": "target_fields (deterministic)"}

        kind = spec.get("kind")
        if kind:
            parsed = _select_document(_parse_yaml_documents(file_path, fetched[file_path]), kind)
            if parsed is None:
                continue  # this file doesn't contain the expected document kind
        else:
            parsed = _parse_yaml_or_json(file_path, fetched[file_path])

        for key, key_path in spec.get("fields", {}).items():
            if _ROUTE_INDEX_SENTINEL in key_path:
                continue  # handled separately by _resolve_route_matches — see docstring
            if _ARRAY_INDEX_SENTINEL in key_path:
                sentinel_pos = key_path.index(_ARRAY_INDEX_SENTINEL)
                array = _get_field(parsed, key_path[:sentinel_pos])
                candidates = _resolve_array_index_candidates(array, key_path[sentinel_pos + 1:])
                if not candidates:
                    continue  # not applicable in this file at all — never guessed, never a value
                if len(candidates) == 1:
                    resolved = candidates[0]
                else:
                    resolved = _hint_match_for_candidates(hint_source, candidates) if hint_source else None
                if resolved is None:
                    unresolved.append({"field": key, "file": file_path, "path": key_path, "sentinel_pos": sentinel_pos, "candidates": candidates})
                    continue
                key_path[sentinel_pos] = resolved["index"]  # mutates target_fields in place

            value = _get_field(parsed, key_path)

            if in_value == "base" and _ARRAY_INDEX_SENTINEL not in key_path:
                _ensure_env_patches_loaded()
                inline_op = _find_inline_json6902_override(env_inline_patches, parsed.get("kind") if isinstance(parsed, dict) else None, key_path)
                if inline_op is not None:
                    inline_overrides.append({"field": key, "base_file": file_path, "op": inline_op, "kustomization_file": f"{environment_path}/kustomization.yaml"})
                else:
                    for patch_file, patch_doc in env_patch_docs.items():
                        found, patch_value = _resolve_strategic_merge_path(patch_doc, parsed, key_path)
                        if found:
                            patch_overrides[key] = {"file": patch_file, "path": list(key_path), "value": patch_value}
                            value = patch_value  # the patch's value is the real, deployed one — base's is not
                            break

            if value is not None:
                # When the same field key is declared in more than one file,
                # the two files can genuinely hold different values if
                # they've drifted out of sync — setdefault (first spec in
                # target_fields wins, deterministically) keeps this from
                # silently flip-flopping by declaration order; the mutation
                # safety check reads each file's own exact value directly.
                extracted.setdefault(key, value)

    return extracted, manifest_files, unresolved, patch_overrides, inline_overrides


# Which target_fields key(s) an entry's field-selection resolves to

_FIELD_SELECTION_CACHE_KEY = "_resolved_field_selection"


def _is_route_dependent_field(key_path: list[Any]) -> bool:
    """True for a field resolved via the Service-port <-> VirtualService
    dependency — never a human-facing "which field" choice; always
    applied alongside whichever primary field was actually selected."""
    return _ROUTE_INDEX_SENTINEL in key_path


def _selectable_field_names(target_fields: list[dict[str, Any]]) -> list[str]:
    """Field keys eligible for "which field" HITL disambiguation —
    excludes any _is_route_dependent_field."""
    return [key for spec in target_fields for key, path in spec.get("fields", {}).items() if not _is_route_dependent_field(path)]


def resolve_field_selection(target_fields: list[dict[str, Any]], known_values: dict[str, Any], matched_change: str) -> Optional[list[str]]:
    """Determines exactly which of an entry's declared target_fields keys
    the human meant — a list, normally one field, but every one when the
    human says "both". Tries: no ambiguity at all -> deterministic
    "both"/substring/fuzzy matching against what's already known -> a
    bounded LLM call -> asking directly. Cached on known_values so every
    later lookup in this run reuses this same resolution."""
    field_names = _selectable_field_names(target_fields)
    distinct_names = list(dict.fromkeys(field_names))
    if len(distinct_names) <= 1:
        return distinct_names if distinct_names else None

    if _FIELD_SELECTION_CACHE_KEY in known_values:
        return known_values[_FIELD_SELECTION_CACHE_KEY]

    selected: Optional[list[str]] = None
    for key, value in known_values.items():
        if key in _NON_SELECTOR_KEYS or key == _FIELD_SELECTION_CACHE_KEY or not isinstance(value, str):
            continue
        if _is_both_answer(value):
            selected = list(distinct_names)
            break
        match = _match_field_key(distinct_names, value)
        if match:
            selected = [match]
            break

    if not selected:
        context = {k: v for k, v in known_values.items() if k not in _NON_SELECTOR_KEYS and k != _FIELD_SELECTION_CACHE_KEY and isinstance(v, str)}
        llm_match = _llm_pick(_FIELD_SELECTION_SYSTEM_PROMPT, f"Valid field names:\n{json.dumps(distinct_names)}\n\nWhat the human said:\n{json.dumps(context)}", distinct_names)
        if llm_match:
            selected = [llm_match]

    if not selected:
        selected = prompt_for_field_clarification(distinct_names, matched_change)

    known_values[_FIELD_SELECTION_CACHE_KEY] = selected
    return selected


def resolve_selected_field(target_fields: list[dict[str, Any]], known_values: dict[str, Any]) -> Optional[list[str]]:
    """Returns the target_fields key(s) already resolved for this run by
    resolve_field_selection()'s cache — or, for entries with no ambiguity
    at all, computes it directly with no cache needed."""
    field_names = _selectable_field_names(target_fields)
    distinct_names = list(dict.fromkeys(field_names))
    if len(distinct_names) <= 1:
        return distinct_names if distinct_names else None
    return known_values.get(_FIELD_SELECTION_CACHE_KEY)


# Pre-check evaluation — 100% deterministic, no LLM

def _evaluate_deterministic_pre_checks(pre_checks: list[str], known_values: dict[str, Any], target_fields: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Resolves every pre-check with plain Python — no LLM. A check whose
    declared data isn't covered by this entry's target_fields is honestly
    marked "cannot_verify" rather than guessed."""
    field_names = [key for spec in (target_fields or []) for key in spec.get("fields", {})]
    selected = resolve_selected_field(target_fields, known_values) if target_fields else None
    requested = known_values.get("requested_value")

    def _selects(key: Optional[str]) -> bool:
        return bool(selected) and key is not None and key in selected

    resolved: list[dict[str, Any]] = []

    for check in pre_checks:
        text = check.lower()
        result: Optional[dict[str, Any]] = None

        if "target exists" in text and "target_exists" in known_values:
            exists = known_values["target_exists"]
            result = {"derivation": f"target_exists={exists!r}", "result": "passed" if exists else "failed", "reason": "target is present" if exists else "target does not exist"}

        elif "base" in text and "overlay" in text and "base_path_exists" in known_values and "environment_path_exists" in known_values:
            both = known_values["base_path_exists"] and known_values["environment_path_exists"]
            result = {
                "derivation": f"base_path_exists={known_values['base_path_exists']!r}, environment_path_exists={known_values['environment_path_exists']!r}",
                "result": "passed" if both else "failed",
                "reason": "both base and overlay paths exist" if both else "base or overlay path missing",
            }

        elif "no-op" in text and selected and requested is not None:
            # selected may name more than one field ("both") — only a true
            # no-op if EVERY selected field's known current value equals
            # requested; any one that differs makes this a real change.
            currents = {key: known_values[key] for key in selected if known_values.get(key) is not None}
            if currents:
                is_noop = all(value == requested for value in currents.values())
                result = {
                    "derivation": f"current {currents}, requested={requested!r}",
                    "result": "failed" if is_noop else "passed",
                    "reason": "requested value equals current value for every selected field" if is_noop else "requested value differs from current for at least one selected field",
                }

        elif _RANGE_IN_TEXT_RE.search(text):
            result = _numeric_in_range(text, requested)

        elif "kubernetes" in text and "quantity" in text and requested is not None:
            valid = _is_valid_k8s_quantity(requested)
            result = {"derivation": f"requested_value={requested!r}", "result": "passed" if valid else "failed", "reason": "valid Kubernetes quantity" if valid else "not a valid Kubernetes quantity"}

        elif "integer" in text and requested is not None:
            if "percentage" in text:
                is_valid = (isinstance(requested, int) and not isinstance(requested, bool) and 0 <= requested <= 100) or (isinstance(requested, str) and _is_percentage_string(requested))
                reason_ok, reason_bad = "valid integer or percentage", "not a valid integer or percentage (0-100)"
            else:
                is_valid = isinstance(requested, int) and not isinstance(requested, bool) and requested >= 0
                reason_ok, reason_bad = "valid non-negative integer", "not a valid non-negative integer"
            result = {"derivation": f"requested_value={requested!r}", "result": "passed" if is_valid else "failed", "reason": reason_ok if is_valid else reason_bad}

        elif selected and (("min" in text and "max" in text) or ("request" in text and "limit" in text)):
            # Same comparison shape, two phrasings: "min <= max" (replicas,
            # VPA) and "request <= limit" (CPU/memory). Unit-suffixed values
            # (e.g. "50m") are parsed via _parse_k8s_quantity, not compared as strings.
            is_minmax = "min" in text and "max" in text
            lo_key = next((k for k in field_names if ("min" if is_minmax else "request") in k.lower()), None)
            hi_key = next((k for k in field_names if ("max" if is_minmax else "limit") in k.lower()), None)
            lo_raw = requested if _selects(lo_key) else (known_values.get(lo_key) if lo_key else None)
            hi_raw = requested if _selects(hi_key) else (known_values.get(hi_key) if hi_key else None)
            lo_val = _parse_k8s_quantity(lo_raw)
            hi_val = _parse_k8s_quantity(hi_raw)
            if lo_val is not None and hi_val is not None:
                ok = lo_val <= hi_val
                result = {
                    "derivation": f"{lo_key or 'min'}={lo_raw!r} ({lo_val}), {hi_key or 'max'}={hi_raw!r} ({hi_val})",
                    "result": "passed" if ok else "failed",
                    "reason": "lower bound does not exceed upper bound" if ok else "lower bound exceeds upper bound",
                }

        if result is None:
            result = {"derivation": "insufficient repo/input data declared for this entry to verify deterministically", "result": "cannot_verify", "reason": "no deterministic rule or declared target_fields data covers this check yet"}
        result["check"] = check
        resolved.append(result)

    return resolved


# CPU/memory/storage unit resolution helpers used by validate()'s STEP 4.6

def _quantity_unit_requirement_label(pre_checks: list[str]) -> Optional[str]:
    """Memory/storage quantities where pre_checks declare a "Valid
    Kubernetes memory/storage quantity" check must never have their unit
    silently assumed — a bare number is genuinely missing information.
    CPU is handled separately (its suffixes are case-SENSITIVE with
    different meanings, e.g. "m" vs "M") via a qualifier-confirmation flow
    instead of this unit-suffix flow. Returns a label ("memory"/"storage")
    or None."""
    for check in pre_checks:
        text = check.lower()
        if "kubernetes" in text and "quantity" in text and "cpu" not in text:
            if "memory" in text:
                return "memory"
            if "storage" in text:
                return "storage"
            return "quantity"
    return None


def _requires_cpu_quantity_confirmation(pre_checks: list[str]) -> bool:
    return any("kubernetes" in c.lower() and "cpu" in c.lower() and "quantity" in c.lower() for c in pre_checks)


def _load_config_changes_meta(config_changes_raw: str) -> tuple[dict[str, str], list[str]]:
    """Reads the generic, non-per-change top-level fields: environment_clusters and cross_cutting_paths."""
    try:
        parsed = json.loads(config_changes_raw)
    except json.JSONDecodeError:
        return {}, []
    return parsed.get("environment_clusters", {}) or {}, parsed.get("cross_cutting_paths", []) or []


def _substitute_placeholders(template: str, service: str, environment: str, cluster: Optional[str]) -> Optional[str]:
    """Fills <service>/<environment>/<cluster> into a path template.
    Returns None if the template needs <cluster> but none could be resolved."""
    if "<cluster>" in template and not cluster:
        return None
    return template.replace("<service>", service).replace("<environment>", environment).replace("<cluster>", cluster or "")


# validate()

def validate(input_request: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
    timing = {"start": time.perf_counter(), "last": time.perf_counter()}

    _step(verbose, "STEP 1 — Read input.json", timing)
    if verbose:
        for line in json.dumps(input_request, indent=2).splitlines():
            print(f"  {line}")

    if not CONFIG_CHANGES_FILE.exists():
        raise ValidationToolError(f"config_changes.json not found at {CONFIG_CHANGES_FILE}")
    config_changes_raw = CONFIG_CHANGES_FILE.read_text()
    environment_clusters, cross_cutting_paths = _load_config_changes_meta(config_changes_raw)

    _step(verbose, "STEP 2 — Read config_changes.json (the single source of truth)", timing)
    _kv(verbose, "path", CONFIG_CHANGES_FILE)
    _kv(verbose, "environment_clusters", environment_clusters)
    _kv(verbose, "cross_cutting_paths", cross_cutting_paths)

    change_label = input_request.get("configuration_change", "unknown")

    _step(verbose, "STEP 3 — GATE B3: Target Verification & Action Classification (Groq)", timing)
    all_entries = json.loads(config_changes_raw).get("config_changes", [])
    match = match_change_candidates(config_changes_raw, input_request)
    candidates = match.get("candidates", [])
    for name in _deterministic_entry_candidates(change_label, all_entries):
        if name not in candidates:
            candidates.append(name)
    _kv(verbose, "candidates", candidates)
    _kv(verbose, "match_reason", match.get("match_reason"))

    if not candidates:
        return {
            "configuration_change": change_label,
            "status": "INVALID",
            "reasons": [match.get("match_reason", "No matching configuration change found in config_changes.json.")],
        }

    if len(candidates) > 1:
        # Generic enough to plausibly mean more than one entry — ask rather than guess.
        _step(verbose, "STEP 3.5 — [Ambiguous] -> HITL: DISAMBIGUATE configuration-change type", timing)
        if verbose:
            print("  Could plausibly mean:")
            for name in candidates:
                _item(verbose, name)
        chosen = prompt_for_change_disambiguation(candidates)
        if chosen is None:
            return {"configuration_change": change_label, "status": "INCOMPLETE", "missing": [f"Which configuration change did you mean: {', '.join(candidates)}"]}
        matched_change = chosen
        matched_entry = next((e for e in all_entries if e.get("request") == matched_change), None) or {}
        human_requirements = check_human_requirements(matched_entry, input_request)
    else:
        matched_change = candidates[0]
        matched_entry = next((e for e in all_entries if e.get("request") == matched_change), None) or {}
        human_requirements = match.get("human_requirements", [])

    human_requirements = _deterministic_satisfy_desired_value(human_requirements, input_request)
    human_requirements = _deterministic_satisfy_option_requirements(human_requirements, input_request)

    target_fields = matched_entry.get("target_fields")
    pre_checks = matched_entry.get("pre_checks", [])
    relevant_paths = matched_entry.get("relevant_paths", [])

    if verbose:
        print(f"\n  human_must_provide ({len(human_requirements)}):")
        _table(verbose, [
            (
                r.get("requirement", ""),
                (
                    f"OK <- {r.get('source_field')}={r['_resolved_value']!r} (resolved from input.json)"
                    if r.get("satisfied") and r.get("_resolved_value") is not None
                    else f"OK <- {r.get('source_field')}" if r.get("satisfied") else "MISSING"
                ),
            )
            for r in human_requirements
        ])
        print(f"\n  pre_checks ({len(pre_checks)}, from config_changes.json):")
        for name in pre_checks:
            _item(verbose, name)
        if relevant_paths:
            print(f"\n  relevant_paths ({len(relevant_paths)}, unresolved):")
            for path in relevant_paths:
                _item(verbose, path)

    missing = [r["requirement"] for r in human_requirements if not r.get("satisfied", False)]

    known_values = dict(input_request)
    for requirement in human_requirements:
        if requirement.get("_resolved_value") is not None:
            known_values[_slugify(requirement["requirement"])] = requirement["_resolved_value"]
    manifest_files: dict[str, dict[str, Any]] = {}

    if missing:
        _step(verbose, "STEP 4 — [Incomplete] -> HITL B3: LOOP (clarify variables)", timing)
        try:
            collected = prompt_for_missing_requirements(missing, human_requirements, input_request, pre_checks)
        except ConflictError as exc:
            _step(verbose, "STEP 4 — [CONFLICT] input.json value vs. HITL answer could not be reconciled", timing)
            return {
                "configuration_change": change_label, "matched_change": matched_change, "status": "CONFLICT",
                "reasons": [str(exc)], "conflict": {"field": exc.field, "input_json_value": exc.original_value, "hitl_value": exc.hitl_value},
            }
        if collected is None:
            return {"configuration_change": change_label, "matched_change": matched_change, "status": "INCOMPLETE", "missing": missing}
        known_values.update(collected)
        _kv(verbose, "collected from human", collected)
    else:
        _step(verbose, "STEP 4 — [Valid]: all human_must_provide fields satisfied, HITL loop skipped", timing)

    # Canonicalize the "desired value" under a fixed key regardless of what
    # the input/human actually called it, so downstream logic can rely on
    # a single "requested_value" key.
    if "requested_value" not in known_values:
        desired = _find_value_by_keyword(human_requirements, known_values, "desired")
        if desired is not None:
            known_values["requested_value"] = desired
    if isinstance(known_values.get("requested_value"), str):
        known_values["requested_value"] = _coerce(known_values["requested_value"])

    # A memory/storage value given without a unit is never assumed — stop
    # and ask. A unit differing from the allowed set ONLY by casing is a
    # provably-same-meaning normalization (never a re-ask); anything else
    # unrecognized is a genuine INVALID.
    unit_label = _quantity_unit_requirement_label(pre_checks)
    if unit_label:
        raw_requested = known_values.get("requested_value")
        parts = _quantity_numeric_and_unit(raw_requested)
        unit_suffix = parts[1] if parts else None
        if unit_suffix == "":
            _step(verbose, "STEP 4.6 — Missing unit: HITL ask for quantity unit", timing)
            unit = prompt_for_quantity_unit(unit_label)
            if not unit:
                return {"configuration_change": change_label, "matched_change": matched_change, "status": "INCOMPLETE", "missing": [f"Unit for the requested {unit_label} value (e.g. Gi, Mi, Ki)"]}
            known_values["requested_value"] = f"{raw_requested}{unit}"
            _kv(verbose, "requested_value (with unit)", known_values["requested_value"])
        elif unit_suffix and unit_suffix not in _QUANTITY_UNITS:
            canonical_by_lower = {u.lower(): u for u in _QUANTITY_UNITS}
            canonical_unit = canonical_by_lower.get(unit_suffix.lower())
            if canonical_unit is not None:
                canonical_value = f"{parts[0]}{canonical_unit}"
                _step(verbose, f"STEP 4.6 — Unit casing normalized: {raw_requested!r} -> {canonical_value!r}", timing)
                known_values["requested_value"] = canonical_value
                _kv(verbose, "requested_value (unit casing normalized)", canonical_value)
            else:
                _step(verbose, "STEP 4.6 — Unit present but not a supported quantity unit: INVALID", timing)
                return {
                    "configuration_change": change_label, "matched_change": matched_change, "status": "INVALID",
                    "reasons": [f"Unsupported/invalid {unit_label} quantity unit {unit_suffix!r} in {raw_requested!r} — allowed units: {', '.join(_QUANTITY_UNITS)}."],
                }
        # else: already an allowed unit, or not quantity-shaped at all —
        # left for the "Valid Kubernetes ... quantity" pre-check to catch.

    elif _requires_cpu_quantity_confirmation(pre_checks):
        # A bare CPU number is syntactically valid but never treated as
        # proof of unambiguous intent (could mean whole cores or
        # millicores) — always confirmed via HITL, never assumed.
        raw_requested = known_values.get("requested_value")
        cpu_parts = _quantity_numeric_and_unit(raw_requested)
        if cpu_parts and cpu_parts[1] == "":
            _step(verbose, "STEP 4.6 — Missing/ambiguous CPU qualifier: HITL clarification required (cores vs millicores cannot be assumed)", timing)
            qualifier = prompt_for_cpu_quantity_qualifier(cpu_parts[0])
            if qualifier is None:
                return {"configuration_change": change_label, "matched_change": matched_change, "status": "INCOMPLETE", "missing": ["Whether the CPU value means whole cores or millicores (e.g. 19, or 19m)"]}
            known_values["requested_value"] = f"{cpu_parts[0]}{qualifier}"
            _kv(verbose, "requested_value (qualifier confirmed)", known_values["requested_value"])

    # Normalize which target_fields key the human means (replicas' min/max,
    # CPU/memory's request/limit). Only place the LLM may influence which
    # field gets mutated, and even then only among fixed declared names.
    if target_fields:
        field_names = _selectable_field_names(target_fields)
        if len(set(field_names)) > 1:
            _step(verbose, "STEP 4.5 — Field-name normalization: typo/abbreviation-tolerant, LLM-assisted if needed", timing)
            selected_field = resolve_field_selection(target_fields, known_values, matched_change)
            _kv(verbose, "resolved field", selected_field)
            if not selected_field:
                return {"configuration_change": change_label, "matched_change": matched_change, "status": "INCOMPLETE", "missing": [f"Which field did you mean: {', '.join(sorted(set(field_names)))}"]}

    # ── Step 3-CFG: Target Existence Check — a real GitHub check, not an LLM guess ──
    service = _find_value_by_keyword(human_requirements, known_values, "service") or known_values.get("service")
    environment = _find_value_by_keyword(human_requirements, known_values, "environment") or known_values.get("environment")

    # Every read from here on in this run is pinned to this exact commit
    # SHA — a cross-file consistency check must never compare values that
    # could have come from two different repository states.
    pinned_ref = _resolve_pinned_ref()

    _step(verbose, "STEP 5 — Step 3-CFG: TARGET EXISTENCE CHECK (real GitHub API calls, not an LLM guess)", timing)
    _kv(verbose, "service", service)
    _kv(verbose, "environment", environment)
    _kv(verbose, "GitOps repo", GITOPS_REPO)
    _kv(verbose, "pinned ref (commit SHA)", pinned_ref)

    if service and environment:
        existence = check_target_existence(str(service), str(environment), ref=pinned_ref)
        known_values["target_exists"] = existence["base_exists"] and existence["environment_exists"]
        known_values["base_path_exists"] = existence["base_exists"]
        known_values["environment_path_exists"] = existence["environment_exists"]

        _table(verbose, [
            (existence["base_path"], f"exists={existence['base_exists']}"),
            (existence["environment_path"], f"exists={existence['environment_exists']}"),
        ])

        if not known_values["target_exists"]:
            missing_paths = [path for exists, path in ((existence["base_exists"], existence["base_path"]), (existence["environment_exists"], existence["environment_path"])) if not exists]
            _step(verbose, "STEP 6 — [Not Found] -> HITL: EXIT (service not found, must onboard first)", timing)
            return {
                "configuration_change": change_label, "matched_change": matched_change, "status": "INVALID",
                "reasons": [f"Service '{service}' is not onboarded for environment '{environment}' in the GitOps repo (missing: {', '.join(missing_paths)}). Must onboard the service before this change can proceed."],
            }

        if target_fields:
            # Deterministic path: config_changes.json already tells us
            # exactly which file(s)/key path(s) to read — no LLM needed to
            # "discover" anything, and only those exact files are fetched.
            _step(verbose, "STEP 6-8 (deterministic) — target_fields: fetch only declared files, parse, extract", timing)
            if verbose:
                print("  target_fields (from config_changes.json, drives what's read — no LLM):")
                for spec in target_fields:
                    dirpart = "base" if spec.get("in", "base") == "base" else "environment"
                    print(f"    {dirpart}/.../{spec['file']}  fields: {list(spec.get('fields', {}))}")
            cluster = environment_clusters.get(str(environment))
            extracted, manifest_files, unresolved, patch_overrides, inline_overrides = extract_target_fields(
                target_fields, existence["base_path"], existence["environment_path"], hint_source=known_values,
                service=str(service), environment=str(environment), cluster=cluster, ref=pinned_ref,
            )
            known_values.update(extracted)
            if patch_overrides:
                known_values["_patch_overrides"] = patch_overrides
                if verbose:
                    print("\n  base vs. environment-overlay: some fields are overridden by an environment patch:")
                    for key, info in patch_overrides.items():
                        _kv(verbose, f"{key} (redirected to)", f"{info['file']} = {info['value']!r}")

            if inline_overrides:
                # A field overridden by an INLINE patch (not a separate
                # file) is never silently written to base only — base's
                # value there isn't what's actually deployed, and this
                # tool does not rewrite operations embedded as a YAML
                # string inside kustomization.yaml.
                _step(verbose, "STEP 6-8 — [Halt] Field(s) overridden by an inline environment patch — cannot safely auto-mutate", timing)
                return {
                    "configuration_change": change_label, "matched_change": matched_change, "status": "INVALID",
                    "reasons": [
                        (
                            f"Field '{o['field']}' in '{o['base_file']}' is overridden by an inline Kustomize "
                            f"patch in '{o['kustomization_file']}' (operation: {o['op']!r}) — base's own value "
                            f"is not what's actually deployed for this field. This pipeline does not "
                            f"automatically rewrite inline patch operations embedded inside kustomization.yaml; "
                            f"please edit that file directly for this field."
                        )
                        for o in inline_overrides
                    ],
                }

            # An ambiguous array field (e.g. Service spec.ports[]) with no
            # unambiguous hint is NEVER defaulted to index 0 — always asked.
            for amb in unresolved:
                _step(verbose, f"STEP 6-8 — Multiple applicable entries for '{amb['field']}': HITL clarification required (never assumes array position)", timing)
                resolved_index = prompt_for_array_disambiguation(amb["field"], amb["candidates"])
                if resolved_index is None:
                    candidate_labels = [c["name"] or f"index {c['index']}" for c in amb["candidates"]]
                    return {"configuration_change": change_label, "matched_change": matched_change, "status": "INCOMPLETE", "missing": [f"Which '{amb['field']}' entry to change: {', '.join(candidate_labels)}"]}
                amb["path"][amb["sentinel_pos"]] = resolved_index
                reparsed = _parse_yaml_or_json(amb["file"], manifest_files[amb["file"]]["content"])
                value = _get_field(reparsed, amb["path"])
                if value is not None:
                    known_values.setdefault(amb["field"], value)
                    extracted.setdefault(amb["field"], value)
                _kv(verbose, f"{amb['field']} (resolved index)", resolved_index)

            if verbose:
                print(f"\n  files fetched: {len(manifest_files)}")
                for path in manifest_files:
                    print(f"    • {path}")
                print("\n  extracted values (plain YAML/JSON parsing, no LLM):")
                for key, value in extracted.items():
                    _kv(verbose, key, value)
        else:
            # No target_fields declared — resolve extra repo paths for
            # context/existence display only (never sent to the LLM).
            cluster = environment_clusters.get(str(environment))
            labeled_prefixes: list[tuple[str, str]] = [
                ("Step 3-CFG base_path (default, always read)", existence["base_path"]),
                ("Step 3-CFG environment_path (default, always read)", existence["environment_path"]),
            ]
            for template in cross_cutting_paths:
                resolved = _substitute_placeholders(template, str(service), str(environment), cluster)
                if resolved is None:
                    print(f"[WARN] Skipping cross_cutting_paths entry '{template}': no cluster mapped for environment '{environment}' in config_changes.json's environment_clusters.", file=sys.stderr)
                    continue
                labeled_prefixes.append(("config_changes.json: cross_cutting_paths", resolved))
            for template in relevant_paths:
                resolved = _substitute_placeholders(template, str(service), str(environment), cluster)
                if resolved is None:
                    print(f"[WARN] Skipping relevant_paths entry '{template}': no cluster mapped for environment '{environment}' in config_changes.json's environment_clusters.", file=sys.stderr)
                    continue
                labeled_prefixes.append((f"config_changes.json: '{matched_change}' entry's relevant_paths", resolved))

            _step(verbose, "STEP 6 — [Exists]: resolve extra repo paths (cross_cutting_paths + relevant_paths)", timing)
            _kv(verbose, "cluster", cluster)
            if verbose:
                print("\n  repo paths to read (path <- which config_changes.json field asked for it):")
            _table(verbose, [(path, f"<- {label}") for label, path in labeled_prefixes])

            _step(verbose, "STEP 7 — GitOps Manifest Discovery: fetching real file existence from GitHub (parallel, no LLM)", timing)
            try:
                manifest_files = fetch_target_manifests(labeled_prefixes)
                if verbose:
                    print(f"  files fetched: {len(manifest_files)}")
                _table(verbose, [(path, f"{len(info['content'])} chars", f"<- {info['label']}") for path, info in manifest_files.items()])
            except GitOpsAccessError as exc:
                print(f"[WARN] Could not read repo manifests: {exc}", file=sys.stderr)
    else:
        _step(verbose, "STEP 6-8 — skipped: service/environment not resolved", timing)

    _step(verbose, "STEP 9 — Pre-check Evaluation: 100% deterministic code, no LLM", timing)
    if verbose:
        print("  known_values:")
        for line in json.dumps(known_values, indent=2).splitlines():
            print(f"    {line}")

    pre_check_results = _evaluate_deterministic_pre_checks(pre_checks, known_values, target_fields)

    # Service-port <-> VirtualService dependency: only triggered when this
    # entry's target_fields declares the route-matching sentinel; a
    # "failed" row flows through the same failed-pre-check handling below.
    dependency_rows, route_matches = _evaluate_service_port_virtualservice_dependency(known_values, manifest_files, target_fields or [], ref=pinned_ref)
    if dependency_rows:
        dependency_checks = {row["check"] for row in dependency_rows}
        pre_check_results = [r for r in pre_check_results if r.get("check") not in dependency_checks] + dependency_rows
    if route_matches is not None:
        known_values["_route_matches"] = route_matches

    # One logical change, atomically: generic across every entry that
    # mirrors the same field into more than one file.
    mirror_rows = _evaluate_mirrored_field_consistency(manifest_files, target_fields or [], pre_checks, ref=pinned_ref)
    if mirror_rows:
        mirror_checks = {row["check"] for row in mirror_rows}
        pre_check_results = [r for r in pre_check_results if r.get("check") not in mirror_checks] + mirror_rows

    if verbose and pre_check_results:
        print(f"\n  resolved ({len(pre_check_results)}, no LLM):")
        for r in pre_check_results:
            print(f"    [{r['result'].upper()}] {r['check']}  ({r['derivation']})")
    failed = [r for r in pre_check_results if r.get("result") == "failed"]
    pending = [r for r in pre_check_results if r.get("result") == "cannot_verify"]

    _step(verbose, "STEP 10 — Validation Result", timing)

    if failed:
        return {
            "configuration_change": change_label, "matched_change": matched_change, "status": "INVALID",
            "reasons": [f"{r['check']}: {r.get('reason', '')}" for r in failed],
            "pending_agent_verification": [r["check"] for r in pending],
        }

    return {
        "configuration_change": change_label,
        "matched_change": matched_change,
        "status": "VALID",
        "pending_agent_verification": [r["check"] for r in pending],
        # Everything mutation.py's _compute_mutation() needs to act on this
        # result without re-running the pipeline. Additive only.
        "_internal": {
            "matched_entry": matched_entry,
            "known_values": known_values,
            "manifest_files": manifest_files,
            # The pristine, untouched input.json — kept alongside
            # known_values (never had an original key silently replaced
            # with a conflicting HITL value) for end-to-end traceability.
            "original_input": input_request,
        },
    }


def print_result(result: dict[str, Any]) -> None:
    print(f"\n{_RULE}")
    print(f"Configuration Change: {result['configuration_change']}")
    print(f"Status:               {result['status']}")
    print(_RULE)

    if result["status"] == "VALID":
        print("All required human inputs are available.")
        print("All required validations defined in config_changes.json have been satisfied.")
        pending = result.get("pending_agent_verification") or []
        if pending:
            print("\nPending agent verification before execution (requires live system access):")
            for check in pending:
                print(f"  • {check}")
        print("\nReady for the next stage.")

    elif result["status"] == "INCOMPLETE":
        print("Missing information:")
        for field in result["missing"]:
            print(f"  • {field}")
        print("\nPlease provide the missing information in the terminal.")

    elif result["status"] == "CONFLICT":
        conflict = result.get("conflict", {})
        print("A human-provided answer conflicts with the original input.json request:")
        print(f"  field           {conflict.get('field')}")
        print(f"  input.json says {conflict.get('input_json_value')!r}")
        print(f"  HITL answer was {conflict.get('hitl_value')!r}")
        print("\ninput.json is the source of truth and was NOT modified.")
        print("No validation, mutation, commit, or PR was performed.")
        print("Halted — please re-run with a consistent value, or correct input.json directly.")

    else:  # INVALID
        print("Validation failed:")
        for reason in result["reasons"]:
            print(f"  • {reason}")
        pending = result.get("pending_agent_verification") or []
        if pending:
            print("\nPending agent verification before execution (requires live system access):")
            for check in pending:
                print(f"  • {check}")
        print("\nConfiguration change cannot proceed.")
