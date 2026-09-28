#!/usr/bin/env python3
"""
mutation.py
-----------
Step 4a: surgical file mutation. Only reached once validation.validate()
has returned VALID. Resolves the target(s) purely from target_fields (no
LLM involved at all here), re-fetches fresh from GitHub, cross-checks the
current value against what validation discovered, and — only if it
matches — edits just that field and writes the result under
mutation_output/. Nothing here is ever committed or pushed — that's
run.py's job (Step 5, a separate file).
"""

from __future__ import annotations

import datetime
import json
import re
from io import StringIO
from typing import Any, Optional

from ruamel.yaml import YAML
from ruamel.yaml.composer import ComposerError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, SingleQuotedScalarString

from ..core.config import MUTATION_OUTPUT_DIR, _SECRET_PATH_MARKERS, _slugify, _RULE
from ..core.exceptions import MutationError, GitOpsAccessError
from ..core.github import _fetch_file_content
from .validation import validate, print_result, resolve_selected_field, _is_route_dependent_field, _get_field, _select_document

_yaml = YAML()  # round-trip parsing — preserves comments/formatting/quotes
_yaml.preserve_quotes = True
# Sequence indent is deliberately NOT set globally — ruamel has no per-file
# auto-detection for it (unlike block-scalar chomping), so _dump_structured()/
# _dump_yaml_documents() detect each file's own indent style before every dump.


def _looks_like_secret_path(path: str) -> bool:
    lowered = path.lower()
    return any(marker in lowered for marker in _SECRET_PATH_MARKERS)


def _fetch_fresh_file(path: str) -> str:
    """Re-fetches via the Contents API (never the CDN-backed raw URL) —
    the same non-CDN read path validation used, so both reads agree on
    the true current state unless the file genuinely changed between them."""
    try:
        content = _fetch_file_content(path)
    except GitOpsAccessError as exc:
        raise MutationError(f"Could not re-fetch '{path}' to verify current value: {exc}") from exc
    if content is None:
        raise MutationError(f"Could not re-fetch '{path}': file not found (HTTP 404).")
    return content


def _parse_structured(path: str, text: str) -> Any:
    if path.endswith((".yaml", ".yml")):
        try:
            # Pad a missing trailing newline before parsing (and strip it
            # back off in _dump_structured) — otherwise ruamel can make an
            # unrelated, untouched block scalar at the end of the document
            # round-trip with a different chomping style.
            return _yaml.load(text if text.endswith("\n") else text + "\n")
        except ComposerError as exc:
            raise MutationError(
                f"'{path}' contains multiple YAML documents (separated by '---'), but no \"kind\" was "
                f"declared for it in config_changes.json — add \"kind\": \"<the intended document's Kind>\" "
                f"to that target_fields spec. Underlying parser error: {exc}"
            ) from exc
    if path.endswith(".json"):
        return json.loads(text)
    raise MutationError(f"'{path}' is not a YAML or JSON file — don't know how to parse it for mutation.")


def _load_yaml_documents(text: str) -> list[Any]:
    """Like _parse_structured's YAML branch, but for a file that may
    bundle more than one YAML document — returns every document, in
    order, so none is ever silently dropped."""
    return list(_yaml.load_all(text if text.endswith("\n") else text + "\n"))


def _select_mutation_document(path: str, documents: list[Any], kind: str) -> Any:
    """Same lookup as validation._select_document (never "the first one"
    by position), but for mutation: raises MutationError (never silently
    falls back to a guess) if no document has that kind, instead of
    returning None."""
    doc = _select_document(documents, kind)
    if doc is None:
        raise MutationError(f"'{path}' does not contain a document with kind '{kind}'.")
    return doc


_SEQ_ITEM_RE = re.compile(r"^(?P<indent>\s*)-\s")
_MAPPING_KEY_RE = re.compile(r"^(?P<indent>\s*)\S.*:\s*(#.*)?$")


def _detect_sequence_indent(text: str) -> tuple[int, int]:
    """Detects the (sequence, offset) pair for ruamel.yaml.indent() from
    this file's own first "key:\\n  - item" pattern, so a dump reproduces
    its actual list-indentation convention instead of always defaulting
    to ruamel's "flush" style."""
    lines = text.splitlines()
    for i in range(1, len(lines)):
        seq_match = _SEQ_ITEM_RE.match(lines[i])
        if not seq_match:
            continue
        key_match = _MAPPING_KEY_RE.match(lines[i - 1])
        if not key_match:
            continue
        key_indent = len(key_match.group("indent"))
        seq_indent = len(seq_match.group("indent"))
        if seq_indent > key_indent:
            offset = seq_indent - key_indent
            return offset + 2, offset
        return 2, 0
    return 2, 0


def _dump_yaml_documents(path: str, documents: list[Any], original_text: str = "") -> str:
    """Like _dump_structured's YAML branch, but re-serializes EVERY
    document (not just the mutated one), '---' separators included."""
    sequence, offset = _detect_sequence_indent(original_text)
    _yaml.indent(mapping=2, sequence=sequence, offset=offset)
    buf = StringIO()
    _yaml.dump_all(documents, buf)
    text = buf.getvalue()
    if not original_text.endswith("\n") and text.endswith("\n"):
        text = text[:-1]
    return text


def _dump_structured(path: str, data: Any, original_text: str = "") -> str:
    if path.endswith((".yaml", ".yml")):
        sequence, offset = _detect_sequence_indent(original_text)
        _yaml.indent(mapping=2, sequence=sequence, offset=offset)
        buf = StringIO()
        _yaml.dump(data, buf)
        text = buf.getvalue()
        if not original_text.endswith("\n") and text.endswith("\n"):
            text = text[:-1]
        return text
    if path.endswith(".json"):
        # Match the original file's style (compact vs. pretty, indent
        # width) instead of always reformatting — json has no round-trip
        # mode like ruamel.yaml.
        if "\n" not in original_text.strip():
            return json.dumps(data, separators=(",", ":"))
        indent = 2
        for line in original_text.splitlines()[1:]:
            stripped = line.lstrip(" ")
            if stripped and stripped != line:
                indent = len(line) - len(stripped)
                break
        text = json.dumps(data, indent=indent)
        return text + "\n" if original_text.endswith("\n") else text
    raise MutationError(f"'{path}' is not a YAML or JSON file — don't know how to serialize it.")


def _walk_to_parent(data: Any, target_path: list[Any]) -> tuple[Any, Any]:
    """Returns (parent_container, final_key) for target_path, raising
    MutationError if any segment along the way doesn't exist."""
    if not target_path:
        raise MutationError("Empty target_path — nothing to mutate.")
    node = data
    for segment in target_path[:-1]:
        try:
            node = node[segment]
        except (KeyError, IndexError, TypeError) as exc:
            raise MutationError(f"Field path {target_path} does not exist in the file (stopped at '{segment}'): {exc}") from exc
    final_key = target_path[-1]
    try:
        node[final_key]
    except (KeyError, IndexError, TypeError) as exc:
        raise MutationError(f"Field path {target_path} does not exist in the file (missing final key '{final_key}'): {exc}") from exc
    return node, final_key


def _values_match(actual: Any, expected: Any) -> bool:
    return str(actual).strip() == str(expected).strip()


_QUANTITY_SPLIT_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*([A-Za-z%]*)$")


def _split_quantity_unit(value: str) -> Optional[tuple[str, str]]:
    """Splits a quantity string into (number, unit) — e.g. "100Gi" ->
    ("100", "Gi"). Purely syntactic — covers storage/CPU/memory and any
    other numeric+unit field alike."""
    match = _QUANTITY_SPLIT_RE.match(value.strip())
    return (match.group(1), match.group(2)) if match else None


def _coerce_like(reference: Any, new_value: Any) -> Any:
    """Coerces new_value to the same type/shape as the field's current
    value. A quantity field whose current value carries a unit (e.g.
    "100Gi") never loses it: a bare new number carries over the CURRENT
    value's unit; a new value that already specifies its own unit is used
    exactly as given. A quoted string field never loses its quote style —
    ruamel's preserve_quotes attaches quoting to the specific loaded
    string instance, so the new content is re-wrapped in that same class."""
    if isinstance(reference, bool):
        if isinstance(new_value, bool):
            return new_value
        return str(new_value).strip().lower() in ("true", "1", "yes")
    if isinstance(reference, str) and not isinstance(new_value, bool):
        content = str(new_value)
        ref_parts = _split_quantity_unit(reference)
        if ref_parts is not None and ref_parts[1]:
            ref_unit = ref_parts[1]
            if isinstance(new_value, (int, float)):
                content = f"{new_value}{ref_unit}"
            elif isinstance(new_value, str):
                new_parts = _split_quantity_unit(new_value)
                if new_parts is not None:
                    content = new_value if new_parts[1] else f"{new_parts[0]}{ref_unit}"
        if isinstance(reference, (DoubleQuotedScalarString, SingleQuotedScalarString)):
            return type(reference)(content)
        return content
    if isinstance(reference, int) and not isinstance(new_value, bool):
        try:
            return int(new_value)
        except (TypeError, ValueError):
            return new_value
    if isinstance(reference, float):
        try:
            return float(new_value)
        except (TypeError, ValueError):
            return new_value
    return new_value


_FIELD_MISSING = object()


def _read_field_from_manifest(target_file: str, manifest_files: dict[str, dict[str, Any]], target_path: list[Any]) -> Any:
    """Reads target_path from target_file's OWN already-fetched content —
    never from known_values' flattened extraction, since when the same
    field key is declared in more than one file, those files can genuinely
    hold different values if they've drifted out of sync; using the
    shared value as the "expected current value" for the wrong file would
    trip a false-positive mismatch abort. Returns _FIELD_MISSING if the
    file doesn't exist or the path doesn't resolve."""
    info = manifest_files.get(target_file)
    if info is None:
        return _FIELD_MISSING
    try:
        parsed = _parse_structured(target_file, info["content"])
    except MutationError:
        return _FIELD_MISSING
    return _get_field(parsed, target_path, default=_FIELD_MISSING)


def _resolve_deterministic_targets(matched_entry: dict[str, Any], known_values: dict[str, Any], manifest_files: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The mutation target(s) (file + field path + current/new value) come
    purely from target_fields — no LLM ever identifies them. Normally one
    target, but an entry may mirror the same field across more than one
    file. A declared file/field is skipped (not an error) when the file
    wasn't fetched during validation, or exists without that exact
    declared field — never "modify a different field instead". Raises
    MutationError with a specific reason when the target can't be
    determined at all."""
    target_fields = matched_entry.get("target_fields")
    if not target_fields:
        raise MutationError(
            "No target_fields declared for this configuration-change entry in config_changes.json — "
            "deterministic mutation requires an exact declared file/field path, and this tool never "
            "falls back to an LLM to guess one."
        )
    service = known_values.get("service")
    environment = known_values.get("environment")
    requested = known_values.get("requested_value")
    if not service or not environment or requested is None:
        raise MutationError("service/environment/requested_value not fully resolved in known_values — cannot determine a mutation target.")
    selected = resolve_selected_field(target_fields, known_values)
    if not selected:
        field_names = sorted({key for spec in target_fields for key in spec.get("fields", {})})
        raise MutationError(
            f"This entry declares multiple possible fields ({', '.join(field_names)}), but the human's "
            f"answer to the 'which field' question could not be matched to any of them — cannot "
            f"determine which one to mutate. Re-run and answer more closely to one of those names."
        )

    # Reused from validation.py (extract_target_fields), never re-derived:
    # a field overridden by an environment Kustomize patch has no real
    # deployed effect if written to base instead.
    patch_overrides: dict[str, dict[str, Any]] = known_values.get("_patch_overrides") or {}

    targets: list[dict[str, Any]] = []
    for field_key in selected:
        # selected can name more than one field when the human said "both".
        override = patch_overrides.get(field_key)
        if override:
            targets.append({
                "is_secret": False, "target_file": override["file"], "target_path": override["path"],
                "current_value": override["value"], "new_value": requested,
                "reason": "redirected from base to the environment overlay's own Kustomize patch — base is overridden there and would have no real deployed effect",
            })
            continue  # base is not touched for this field at all — the patch is the only real target
        for spec in target_fields:
            if field_key not in spec.get("fields", {}):
                continue
            dir_path = f"base/{service}" if spec.get("in", "base") == "base" else f"environments/{environment}/{service}"
            target_file = f"{dir_path}/{spec['file']}"
            target_path = list(spec["fields"][field_key])
            current_value = _read_field_from_manifest(target_file, manifest_files, target_path)
            if current_value is _FIELD_MISSING:
                continue  # file missing, or exists without this exact declared field — skip, not an error
            targets.append({
                "is_secret": False, "target_file": target_file, "target_path": target_path,
                "current_value": current_value, "new_value": requested,
                "reason": "resolved deterministically from config_changes.json target_fields — no LLM used",
            })

    # Service-port <-> VirtualService dependency: validate() already found
    # every matching route and verified it against the Service's current
    # port (known_values["_route_matches"]) — reused here, never
    # re-derived, so a second independent pass could never disagree with
    # what was actually validated.
    route_matches = known_values.get("_route_matches")
    if route_matches:
        for spec in target_fields:
            for field_key, path in spec.get("fields", {}).items():
                if not _is_route_dependent_field(path):
                    continue
                target_file = next((p for p in manifest_files if p == spec["file"] or p.endswith(f"/{spec['file']}")), None)
                if target_file is None:
                    continue
                for match in route_matches:
                    targets.append({
                        "is_secret": False, "target_file": target_file, "target_path": match["path"],
                        "current_value": match["port"], "new_value": requested, "kind": spec.get("kind"),
                        "reason": "resolved deterministically via the Service-port <-> VirtualService dependency — no LLM used",
                    })

    return targets


def _compute_mutation(input_request: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
    """Runs validate() once; performs Step 4a only if VALID, reusing
    everything validate() already discovered via `_internal` — no repeat
    Groq calls or GitHub requests. On COMPLETED, the new file text is
    staged under the transient "_write" key — mutate() below owns all
    output-folder I/O."""
    validation_result = validate(input_request, verbose=verbose)
    print_result(validation_result)

    if validation_result["status"] != "VALID":
        return {"validation": validation_result, "mutation": {"status": "SKIPPED", "reason": f"Validation status was {validation_result['status']}, not VALID."}}

    internal = validation_result["_internal"]
    matched_entry_raw = internal["matched_entry"]
    known_values = internal["known_values"]
    manifest_files = internal["manifest_files"]

    if matched_entry_raw is None:
        return {"validation": validation_result, "mutation": {"status": "FAILED", "reason": "Matched config_changes.json entry unavailable."}}

    try:
        targets = _resolve_deterministic_targets(matched_entry_raw, known_values, manifest_files)
    except MutationError as exc:
        return {"validation": validation_result, "mutation": {"status": "FAILED", "reason": str(exc)}}

    if not targets:
        return {
            "validation": validation_result,
            "mutation": {"status": "FAILED", "reason": "The declared field was resolved, but none of its target_fields file(s)/path(s) exist for this service — nothing to mutate."},
        }
    if verbose:
        print(f"\n{_RULE}")
        print(f"STEP 4a — Surgical File Mutation: resolved deterministically from target_fields (no LLM), {len(targets)} file(s)")
        print(_RULE)
        for line in json.dumps(targets, indent=2).splitlines():
            print(f"  {line}")

    # All-or-nothing across every target: when the same field is mirrored
    # into more than one file, or "both" fields land in the same file, a
    # mismatch/refusal on any one aborts the whole mutation. Targets
    # sharing a file are grouped and applied on ONE parse/dump pass — two
    # independent dumps of the same file would silently discard one change.
    targets_by_file: dict[str, list[dict[str, Any]]] = {}
    for target in targets:
        if target.get("is_secret"):
            return {
                "validation": validation_result,
                "mutation": {
                    "status": "REFUSED",
                    "reason": "This is a secret/sealed-secret change. Plaintext secret mutation is never performed by "
                              "this tool — it requires the sealed-secrets sealing mechanism, which this tool has no access to.",
                },
            }

        target_file = target.get("target_file")
        target_path = target.get("target_path")

        if not target_file or not target_path:
            return {"validation": validation_result, "mutation": {"status": "FAILED", "reason": target.get("reason", "Could not identify a single unambiguous mutation target.")}}

        if target_file not in manifest_files:
            return {"validation": validation_result, "mutation": {"status": "FAILED", "reason": f"Resolved target '{target_file}', which was not among the files read during validation."}}

        if _looks_like_secret_path(target_file):
            return {
                "validation": validation_result,
                "mutation": {
                    "status": "REFUSED",
                    "reason": f"'{target_file}' looks like a secret/sealed-secret file by name. Refusing to mutate "
                              f"it directly, regardless of the model's own judgment, to guarantee no plaintext secret is ever written by this tool.",
                },
            }

        targets_by_file.setdefault(target_file, []).append(target)

    changes: list[dict[str, Any]] = []
    write_by_file: dict[str, str] = {}
    for target_file, file_targets in targets_by_file.items():
        # Re-fetch fresh (never the possibly-minutes-old discovery copy)
        # once per file. A file whose targets declare "kind" (the
        # multi-document ingress file) is read/written as ALL of its
        # documents together — never dropping or reordering the others.
        fresh_text = _fetch_fresh_file(target_file)
        kind = next((t.get("kind") for t in file_targets if t.get("kind")), None)
        if kind:
            documents = _load_yaml_documents(fresh_text)
            parsed = _select_mutation_document(target_file, documents, kind)
        else:
            parsed = _parse_structured(target_file, fresh_text)

        for target in file_targets:
            target_path = target["target_path"]
            try:
                parent, final_key = _walk_to_parent(parsed, list(target_path))
            except MutationError as exc:
                return {"validation": validation_result, "mutation": {"status": "FAILED", "reason": str(exc)}}

            actual_current_value = parent[final_key]
            claimed_current_value = target.get("current_value")
            if not _values_match(actual_current_value, claimed_current_value):
                return {
                    "validation": validation_result,
                    "mutation": {
                        "status": "ABORTED",
                        "reason": (
                            f"Current value mismatch — aborting to avoid an unsafe edit. "
                            f"Repo currently has {target_path} = {actual_current_value!r} in '{target_file}', "
                            f"but the value discovered/expected was {claimed_current_value!r}."
                        ),
                    },
                }

            new_value = _coerce_like(actual_current_value, target.get("new_value"))
            parent[final_key] = new_value
            changes.append({"file": target_file, "field_path": target_path, "previous_value": actual_current_value, "new_value": new_value, "kind": kind})

        write_by_file[target_file] = _dump_yaml_documents(target_file, documents, fresh_text) if kind else _dump_structured(target_file, parsed, fresh_text)

    return {
        "validation": validation_result,
        "mutation": {
            "status": "COMPLETED",
            "files": [{"file": c["file"], "field_path": c["field_path"], "previous_value": c["previous_value"], "new_value": c["new_value"], "kind": c.get("kind")} for c in changes],
        },
        "_write": [{"target_file": f, "text": text} for f, text in write_by_file.items()],
    }


def _run_slug(input_request: dict[str, Any]) -> str:
    change = input_request.get("configuration_change", "change")
    service = input_request.get("service", "service")
    environment = input_request.get("environment", "env")
    return _slugify(f"{change}_{service}_{environment}")


def mutate(input_request: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
    """Runs _compute_mutation(), then owns all output-folder I/O: every
    execution gets its own timestamped folder under mutation_output/
    (never overwriting a prior run), containing every mutated file plus a
    result.json with the full execution result. Never commits/pushes."""
    result = _compute_mutation(input_request, verbose=verbose)

    timestamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S%f")
    run_dir = MUTATION_OUTPUT_DIR / f"{timestamp}_{_run_slug(input_request)}"
    run_dir.mkdir(parents=True, exist_ok=True)

    pending_writes = result.pop("_write", None) or []
    output_paths: dict[str, str] = {}
    for pending in pending_writes:
        output_path = run_dir / pending["target_file"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(pending["text"])
        output_paths[pending["target_file"]] = str(output_path)
    if output_paths and result["mutation"]["status"] == "COMPLETED":
        for file_change in result["mutation"]["files"]:
            file_change["output_path"] = output_paths[file_change["file"]]

    result["output_dir"] = str(run_dir)
    (run_dir / "result.json").write_text(json.dumps(_serializable_result(result), indent=2))

    return result


def _serializable_result(result: dict[str, Any]) -> dict[str, Any]:
    """Trims large embedded manifest content before writing result.json —
    the mutated file itself is written alongside it with the full text."""
    trimmed = json.loads(json.dumps(result, default=str))
    internal = (trimmed.get("validation") or {}).get("_internal")
    if internal and internal.get("manifest_files"):
        internal["manifest_files"] = {path: {"label": info.get("label"), "chars": len(info.get("content", ""))} for path, info in internal["manifest_files"].items()}
    return trimmed


def print_mutation_result(result: dict[str, Any]) -> None:
    mutation = result["mutation"]
    print(f"\n{_RULE}")
    print("Step 4a Result")
    print(_RULE)

    if mutation["status"] == "COMPLETED":
        print("VALIDATION:  PASSED")
        print(f"MUTATION:    COMPLETED ({len(mutation['files'])} file{'s' if len(mutation['files']) != 1 else ''})")
        for change in mutation["files"]:
            print(f"\n  file          {change['file']}")
            print(f"  field path    {change['field_path']}")
            print(f"  value change  {change['previous_value']!r} -> {change['new_value']!r}")
            print(f"  (Written to local staging file — NOT committed/pushed: {change['output_path']})")
    elif mutation["status"] == "SKIPPED":
        print(f"VALIDATION:  {result['validation']['status']}")
        print("MUTATION:    SKIPPED")
        print(f"Reason: {mutation['reason']}")
    else:  # REFUSED / ABORTED / FAILED
        print("VALIDATION:  PASSED")
        print(f"MUTATION:    {mutation['status']}")
        print(f"Reason: {mutation['reason']}")
        print("\nNo file was written. No unrelated or partial changes were made.")

    print(f"\nExecution output folder: {result['output_dir']}")
    print("(contains result.json for this execution, plus the mutated file if COMPLETED)")
