#!/usr/bin/env python3
"""
llm_hitl.py
-----------
Human-input understanding: LLM (Groq) calls and every interactive HITL
prompt/clarification loop. The LLM here is strictly scoped to matching
free text to a fixed set of options given to it — it never reads
repository content, never picks a mutation target, never invents a
value outside what it was given. Depends only on config.py and
exceptions.py (never on github.py, validation.py, or mutation.py — this
module has no repository access of its own).
"""

from __future__ import annotations

import difflib
import json
import re
from typing import Any, Optional


from .config import (
    _MAX_HITL_ATTEMPTS, _FUZZY_MATCH_CUTOFF, _QUANTITY_UNITS,
    _slugify, _coerce, _is_valid_k8s_quantity, _is_percentage_string, _RANGE_IN_TEXT_RE,
    _parse_k8s_quantity,
)
from .exceptions import GroqApiError, ConflictError
from ...questions import CfgQuestion, current_asker


def _norm(text: Any) -> str:
    """Lowercase, alnum-only normalization used by every deterministic
    text matcher below."""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _closest(hint_norm: str, options: list[str]) -> Optional[str]:
    """Similarity-ratio fallback (e.g. a typo like 'limt' for 'limit'),
    tried only after exact/substring matching found nothing or found more
    than one candidate."""
    normalized = {_norm(o): o for o in options}
    close = difflib.get_close_matches(hint_norm, list(normalized), n=1, cutoff=_FUZZY_MATCH_CUTOFF)
    return normalized[close[0]] if close else None


# ─────────────────────────────────────────────────────────────────────────
# Groq calls
# ─────────────────────────────────────────────────────────────────────────

def _call_groq_json(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """The agent's one model client, behind this section's exception type.

    Only the error type is section-local: everything in `validation.py`
    catches `GroqApiError`, and a model failure here has always meant the same
    thing as any other tool error -- report it, do not guess.
    """
    from ....llm.client import LLMError, complete_json

    try:
        return complete_json(system_prompt, user_prompt)
    except LLMError as exc:
        raise GroqApiError(str(exc)) from exc


def _llm_pick(system_prompt: str, user_prompt: str, options: list[str]) -> Optional[str]:
    """Bounded LLM call shared by every disambiguation/option-resolution
    prompt below: may only return one of `options`, verbatim, or None —
    never invents a value outside the given list."""
    selected = _call_groq_json(system_prompt, user_prompt).get("selected")
    return selected if selected in options else None


_IDENTIFY_SYSTEM_PROMPT = """\
Identify which configuration-change entry (or entries) a request could mean, \
then — only if exactly one is a clear, unambiguous match — check which of \
that entry's required fields the input JSON already has.

You get: a list of entries (each with a "request" name and its \
human_must_provide list) and an input JSON.

1. Decide which entries the input's "configuration_change" (and any other \
free text in the input) could plausibly mean, by meaning, not exact wording.
   - If exactly one entry is a clear, specific match (e.g. "replica_count" \
matches "Replica count change"), put just that one in "candidates".
   - If the request is generic and could plausibly mean more than one entry \
— e.g. "memory" alone, when separate entries exist for "Memory request/limit \
change", "Memory target utilization change", "Memory minimum allowed change" \
and "Memory maximum allowed change" — put ALL plausible entries in \
"candidates". Never guess a single one when the request doesn't say enough \
to tell them apart; a human will be asked to pick.
   - If nothing plausibly matches any entry, "candidates" is an empty list.
2. ONLY when "candidates" has exactly one entry: for each item in that \
entry's human_must_provide, decide if the input JSON already has a value \
for it by meaning — mark satisfied + which input field, or not satisfied. \
If "candidates" has zero or more than one entry, leave "human_requirements" \
as an empty list — it will be resolved later once the entry is confirmed.

Respond with one JSON object, no other text:
{
  "candidates": ["<matching entry's 'request'>", "..."],
  "match_reason": "<one sentence>",
  "human_requirements": [
    {"requirement": "<verbatim human_must_provide item>", "satisfied": true|false, "source_field": "<input key, or null>"}
  ]
}
"""

_HUMAN_REQUIREMENTS_SYSTEM_PROMPT = """\
Check which of a configuration-change entry's required human-provided \
fields are already present in the given input JSON.

You get: a human_must_provide list and an input JSON.

For each item, decide if the input JSON already has a value for it by \
meaning (not exact wording) — mark satisfied + which input field, or not \
satisfied.

Respond with one JSON object, no other text:
{
  "human_requirements": [
    {"requirement": "<verbatim human_must_provide item>", "satisfied": true|false, "source_field": "<input key, or null>"}
  ]
}
"""

_DISAMBIGUATE_SYSTEM_PROMPT = """\
A human was asked to pick which configuration-change entry they meant from \
a fixed list of candidates, and gave a free-text answer that didn't \
literally match any candidate's wording. Pick exactly the one candidate \
(verbatim, character-for-character as given) their answer refers to, by \
meaning and domain knowledge — e.g. "average utilization" or "the average" \
means the same thing as a candidate named "... target utilization change" \
(Kubernetes HPA's field for this is literally named "averageUtilization", \
so "average" and "target" are synonyms here); "minimum"/"floor" means the \
same as "... minimum allowed change"; "cap"/"ceiling" means the same as \
"... maximum allowed change". If the answer doesn't clearly and \
unambiguously match exactly one candidate, respond with null — never \
invent a candidate that wasn't given.

Respond with one JSON object, no other text:
{"selected": "<one candidate string, verbatim from the given list, or null>"}
"""

_FIELD_SELECTION_SYSTEM_PROMPT = """\
A human is requesting a Kubernetes configuration change and must specify \
exactly which one of several possible fields they mean. You get the fixed \
list of valid field names for this request, and everything the human said \
(their original request text plus any answers they gave). Pick exactly \
the one field name (verbatim, character-for-character as given) they \
mean, using meaning, common abbreviations, and typo tolerance — e.g. \
"req", "requessttt", or "request cpu" all mean a field ending in \
"_request"; "limt", "limit", or "cpu lim" all mean a field ending in \
"_limit"; "min replica"/"minimum replicas" means a field named \
"minReplicas"; "max replica"/"maximum replicas"/"maxreplca" means a field \
named "maxReplicas". If nothing given clearly and unambiguously points to \
exactly one of the given field names — e.g. a bare "cpu" or "memory" with \
no request/limit qualifier at all — respond with null. Never invent a \
field name that wasn't given.

Respond with one JSON object, no other text:
{"selected": "<one field name, verbatim from the given list, or null>"}
"""

_HITL_OPTION_SYSTEM_PROMPT = """\
A human was asked a question with a fixed set of listed options and gave \
a free-text answer that doesn't cleanly match any option's wording. Pick \
exactly the one option (verbatim, character-for-character as given) their \
answer means, using meaning, common abbreviations, and typo tolerance. If \
the answer is unrelated noise, or doesn't clearly and unambiguously match \
exactly one option, respond with null — never invent an option that \
wasn't given.

Respond with one JSON object, no other text:
{"selected": "<one option, verbatim from the given list, or null>"}
"""


# ─────────────────────────────────────────────────────────────────────────
# Configuration-change type matching + disambiguation
# ─────────────────────────────────────────────────────────────────────────

def match_change_candidates(config_changes_raw: str, input_request: dict[str, Any]) -> dict[str, Any]:
    """Matches the request to one or more config_changes.json entries by
    meaning. Returns MULTIPLE candidates rather than guessing when the
    request is generic (e.g. "memory" alone) — the caller sends that to
    HITL. When there's exactly one candidate, human_requirements is also
    resolved in this same call."""
    entries = json.loads(config_changes_raw).get("config_changes", [])
    summary = [{"request": e.get("request"), "human_must_provide": e.get("human_must_provide", [])} for e in entries]
    user_prompt = f"Entries:\n{json.dumps(summary)}\n\nInput JSON:\n{json.dumps(input_request)}"
    return _call_groq_json(_IDENTIFY_SYSTEM_PROMPT, user_prompt)


def _deterministic_entry_candidates(raw_text: str, all_entries: list[dict[str, Any]]) -> list[str]:
    """Deterministic FLOOR under the LLM's own candidate list: substring-
    matches raw_text against every entry's 'request' name AND every field
    key declared in its target_fields (an abbreviation like "max" barely
    appears in any title, but matches field names like "maxReplicas").
    Only ever used to ADD candidates the LLM might have dropped, never to
    remove ones it found via broader synonym reasoning."""
    text_norm = _norm(raw_text)
    if not text_norm:
        return []
    matches = []
    for entry in all_entries:
        name = entry.get("request", "")
        searchable = [name] + [key for spec in entry.get("target_fields", []) for key in spec.get("fields", {})]
        for text in searchable:
            candidate_norm = _norm(text)
            if candidate_norm and (text_norm in candidate_norm or candidate_norm in text_norm):
                matches.append(name)
                break
    return matches


def check_human_requirements(entry: dict[str, Any], input_request: dict[str, Any]) -> list[dict[str, Any]]:
    """Checks one entry's human_must_provide satisfaction — used only after
    an ambiguous match has been resolved by the human."""
    human_must_provide = entry.get("human_must_provide", [])
    if not human_must_provide:
        return []
    user_prompt = f"human_must_provide:\n{json.dumps(human_must_provide)}\n\nInput JSON:\n{json.dumps(input_request)}"
    return _call_groq_json(_HUMAN_REQUIREMENTS_SYSTEM_PROMPT, user_prompt).get("human_requirements", [])


def prompt_for_change_disambiguation(candidates: list[str]) -> Optional[str]:
    """The request was generic enough to plausibly mean more than one
    entry — asks rather than guesses. Bidirectional substring match first
    (covers most answers, no LLM call), then a bounded LLM call for
    domain synonyms (e.g. "average" vs "target" utilization) a plain
    substring match can't catch. Re-asks on failure up to
    _MAX_HITL_ATTEMPTS; only EOF/Ctrl-C ends the loop early."""
    print("\nThis request could mean more than one configuration change:")
    for i, name in enumerate(candidates, 1):
        print(f"  {i}. {name}")
    for attempt in range(1, _MAX_HITL_ATTEMPTS + 1):
        answer = current_asker().ask(CfgQuestion.make(
            "configuration_change", "Which one did you mean? (number or name)",
            attempt=attempt, max_attempts=_MAX_HITL_ATTEMPTS,
            allowed_values=list(candidates),
            why="the request matches more than one entry in config_changes.json"))
        if answer is None:
            print()
            return None
        answer = answer.strip()
        if not answer:
            print(f"  No answer given ({attempt}/{_MAX_HITL_ATTEMPTS}) — please pick one of the options above.")
            continue
        if answer.isdigit():
            index = int(answer) - 1
            if 0 <= index < len(candidates):
                return candidates[index]
            print(f"  '{answer}' isn't one of the numbered options above ({attempt}/{_MAX_HITL_ATTEMPTS}) — please try again.")
            continue
        answer_norm = _norm(answer)
        substring_matches = [name for name in candidates if answer_norm in _norm(name) or _norm(name) in answer_norm]
        if len(substring_matches) == 1:
            return substring_matches[0]
        # Zero matches, or ambiguous (a substring of more than one candidate,
        # e.g. "target utilization" matching both a CPU and Memory entry) —
        # never silently pick one; try the LLM, then re-ask.
        matched = _llm_pick(_DISAMBIGUATE_SYSTEM_PROMPT, f"Candidates:\n{json.dumps(candidates)}\n\nHuman's answer:\n{answer}", candidates)
        if matched:
            return matched
        print(f"  I couldn't match '{answer}' to any of the options above ({attempt}/{_MAX_HITL_ATTEMPTS}) — please try again.")
    return None


# ─────────────────────────────────────────────────────────────────────────
# Array-candidate matching (used by validation.py's array disambiguation)
# ─────────────────────────────────────────────────────────────────────────

def _match_candidate_by_hint(candidates: list[dict[str, Any]], hint: Any) -> Optional[dict[str, Any]]:
    """Matches a hint (a port name/value already given) to one array
    candidate ({"index", "name", "value"} dicts): exact name/value ->
    hint-is-substring-of-name (ONE DIRECTION ONLY — a candidate name can
    be a genuine prefix of another's, e.g. "http" of "https"/"https-8081";
    the reverse direction would make plain "http" falsely match hint
    "https" too) -> similarity ratio. Returns None if a stage matches
    zero or more than one candidate — never guesses."""
    hint_norm = _norm(hint)
    if not hint_norm:
        return None
    exact = [
        c for c in candidates
        if hint_norm == (_norm(c["name"]) if c["name"] else None) or hint_norm == _norm(c["value"])
    ]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    substring = [c for c in candidates if c["name"] and hint_norm in _norm(c["name"])]
    if len(substring) == 1:
        return substring[0]
    if substring:
        return None
    named = {_norm(c["name"]): c for c in candidates if c["name"]}
    close = difflib.get_close_matches(hint_norm, list(named), n=1, cutoff=_FUZZY_MATCH_CUTOFF)
    return named[close[0]] if close else None


def _hint_match_for_candidates(known_values: dict[str, Any], candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Checks every already-provided string field (except structural
    bookkeeping) for one that unambiguously identifies which array
    candidate was meant — never asked again if it's already there."""
    excluded = {"configuration_change", "service", "environment"}
    for key, value in known_values.items():
        if key in excluded or not isinstance(value, str) or not value.strip():
            continue
        matched = _match_candidate_by_hint(candidates, value)
        if matched:
            return matched
    return None


def prompt_for_array_disambiguation(field_label: str, candidates: list[dict[str, Any]]) -> Optional[int]:
    """Reached when more than one array entry is applicable and no hint in
    input.json unambiguously picks one. NEVER defaults to index 0 or any
    other position — always asks."""
    print(f"\nThis Service has multiple entries applicable to '{field_label}':")
    for c in candidates:
        label = c["name"] if c["name"] else f"(unnamed, index {c['index']})"
        print(f"  - {label}: {c['value']}")
    for attempt in range(1, _MAX_HITL_ATTEMPTS + 1):
        answer = current_asker().ask(CfgQuestion.make(
            "select:%s" % field_label, "Which one do you want to change?",
            attempt=attempt, max_attempts=_MAX_HITL_ATTEMPTS,
            allowed_values=[str(c["name"] or c["index"]) for c in candidates],
            why="more than one entry under %r could be the target" % field_label))
        if answer is None:
            print()
            return None
        answer = answer.strip()
        if not answer:
            print(f"  No answer given ({attempt}/{_MAX_HITL_ATTEMPTS}) — please specify which one.")
            continue
        matched = _match_candidate_by_hint(candidates, answer)
        if matched:
            return matched["index"]
        print(f"  I couldn't match '{answer}' to one of the listed entries ({attempt}/{_MAX_HITL_ATTEMPTS}) — please try again.")
    return None


# ─────────────────────────────────────────────────────────────────────────
# Field-name matching/selection (which of an entry's target_fields keys)
# ─────────────────────────────────────────────────────────────────────────

_NON_SELECTOR_KEYS = {"configuration_change", "service", "environment"}  # raw request metadata, never a "which field" answer
_BOTH_SYNONYMS = {"both", "all", "everything", "allofthem", "eachofthem", "each"}


def _match_field_key(field_names: list[str], hint: str) -> Optional[str]:
    """Substring match between a hint (e.g. 'req', 'limit') and a
    target_fields key name — ambiguous (matches more than one candidate,
    e.g. bare "replicas" against both "minReplicas"/"maxReplicas") returns
    None rather than guessing, so the caller escalates to the LLM/HITL."""
    hint_norm = _norm(hint)
    if not hint_norm:
        return None
    matches = [name for name in field_names if hint_norm in _norm(name)]
    if len(matches) == 1:
        return matches[0]
    return None if matches else _closest(hint_norm, field_names)


def _is_both_answer(raw_answer: str) -> bool:
    """True when the human's answer means "apply this to every one of the
    entry's fields" — checked before single-field matching, since "both"
    isn't itself a field name."""
    return _norm(raw_answer) in _BOTH_SYNONYMS


def prompt_for_field_clarification(candidates: list[str], matched_change: str) -> Optional[list[str]]:
    """Only reached when neither deterministic matching nor the LLM could
    tell which declared field was meant. "both" (or a synonym) is always a
    valid answer, meaning every listed field."""
    pretty = " or ".join(candidates)
    print(f"\nI could not clearly identify which field you mean for '{matched_change}'.")
    for attempt in range(1, _MAX_HITL_ATTEMPTS + 1):
        answer = current_asker().ask(CfgQuestion.make(
            "which_field", "Please confirm: %s, or both?" % pretty,
            attempt=attempt, max_attempts=_MAX_HITL_ATTEMPTS,
            allowed_values=list(candidates) + ["both"],
            why="'%s' declares more than one field and the request did not say which"
                % matched_change))
        if answer is None:
            print()
            return None
        answer = answer.strip()
        if not answer:
            print(f"  No answer given ({attempt}/{_MAX_HITL_ATTEMPTS}) — please confirm {pretty}, or both.")
            continue
        if _is_both_answer(answer):
            return list(candidates)
        match = _match_field_key(candidates, answer)
        if match:
            return [match]
        match = _llm_pick(_FIELD_SELECTION_SYSTEM_PROMPT, f"Valid field names:\n{json.dumps(candidates)}\n\nWhat the human said:\n{json.dumps({'clarification_answer': answer})}", candidates)
        if match:
            return [match]
        print(f"  I still couldn't match '{answer}' to {pretty}, or both ({attempt}/{_MAX_HITL_ATTEMPTS}) — please try again.")
    return None


# ─────────────────────────────────────────────────────────────────────────
# Option-based requirement answers (e.g. "Request or limit")
# ─────────────────────────────────────────────────────────────────────────

_OPTION_LIST_RE = re.compile(r":\s*(.+)$")


def _requirement_options(requirement_text: str) -> Optional[list[str]]:
    """Returns the explicit option list a requirement's own text declares
    (e.g. "Which field: minReplicas, maxReplicas, or both" -> those three
    words), or None if it's free-text (e.g. "Desired CPU value")."""
    match = _OPTION_LIST_RE.search(requirement_text)
    option_text = match.group(1) if match else requirement_text
    if " or " not in option_text and "," not in option_text:
        return None
    return [o.strip().rstrip(".") for o in re.split(r",|\bor\b", option_text) if o.strip()]


def _deterministic_hitl_option_match(requirement_text: str, raw_answer: str) -> Optional[str]:
    """The deterministic half of option-answer matching (bidirectional
    substring -> fuzzy) — factored out so the SAME logic can also check
    whether a value input.json already supplied unambiguously answers an
    option-based requirement, without ever invoking the LLM for something
    this clear-cut."""
    options = _requirement_options(requirement_text)
    if options is None:
        return None
    answer_norm = _norm(raw_answer)
    if not answer_norm:
        return None
    matches = [o for o in options if answer_norm in _norm(o) or _norm(o) in answer_norm]
    if len(matches) == 1:
        return matches[0]
    return None if matches else _closest(answer_norm, options)


def resolve_hitl_option(requirement_text: str, raw_answer: str) -> Optional[str]:
    """Resolves a human's answer to one of requirement_text's own listed
    options. If the requirement has no option list, the raw answer is
    accepted as-is. Returns None when genuinely unresolved — the caller
    re-asks rather than accepting the raw, unmatched answer."""
    options = _requirement_options(requirement_text)
    if options is None:
        return raw_answer
    match = _deterministic_hitl_option_match(requirement_text, raw_answer)
    if match is not None:
        return match
    return _llm_pick(_HITL_OPTION_SYSTEM_PROMPT, f"Question: {requirement_text}\nOptions: {json.dumps(options)}\nHuman's answer: {raw_answer}", options)


# ─────────────────────────────────────────────────────────────────────────
# Quantity-unit HITL prompts
# ─────────────────────────────────────────────────────────────────────────

def prompt_for_cpu_quantity_qualifier(numeric_part: str) -> Optional[str]:
    """A bare CPU number (e.g. "19") is syntactically valid (whole cores),
    but syntactic validity isn't proof of unambiguous intent — it could
    equally mean "19m" (millicores). Always sent to HITL, never assumed;
    only the qualifier is asked for, never the number itself. Returns ""
    (whole cores), "m" (millicores), or None if unresolved."""
    print(f"\nThe CPU value '{numeric_part}' doesn't specify whether it means whole cores or millicores, and this cannot be assumed — which did you mean?")
    print(f"  1. {numeric_part} — whole cores")
    print(f"  2. {numeric_part}m — millicores")
    cores_answers = {"1", "cores", "core", "wholecores", "wholecore", numeric_part.lower()}
    milli_answers = {"2", "m", "millicores", "millicore", f"{numeric_part}m".lower()}
    for attempt in range(1, _MAX_HITL_ATTEMPTS + 1):
        answer = current_asker().ask(CfgQuestion.make(
            "cpu_unit",
            "Is %s whole cores (1) or millicores (2)?" % numeric_part,
            attempt=attempt, max_attempts=_MAX_HITL_ATTEMPTS,
            allowed_values=["1", "2"],
            why="a bare number is ambiguous for CPU; %s and %sm differ by 1000x"
                % (numeric_part, numeric_part)))
        if answer is None:
            print()
            return None
        answer = answer.strip()
        if not answer:
            print(f"  No answer given ({attempt}/{_MAX_HITL_ATTEMPTS}) — please choose 1 (cores) or 2 (millicores).")
            continue
        norm = re.sub(r"[^a-z0-9.]", "", answer.lower())
        if norm in cores_answers:
            return ""
        if norm in milli_answers:
            return "m"
        print(f"  I couldn't match '{answer}' to a choice ({attempt}/{_MAX_HITL_ATTEMPTS}) — please choose 1 (cores) or 2 (millicores).")
    return None


def prompt_for_quantity_unit(label: str) -> Optional[str]:
    """Only reached when a memory/storage value was given without a unit.
    Never silently assumes Gi/Mi/anything — asks. Matching is
    case-insensitive (a unit is never rejected for its capitalization)."""
    canonical_by_lower = {unit.lower(): unit for unit in _QUANTITY_UNITS}
    pretty = ", ".join(_QUANTITY_UNITS)
    print(f"\nWhat unit should be used for the {label} value? Supported units: {pretty}.")
    for attempt in range(1, _MAX_HITL_ATTEMPTS + 1):
        answer = current_asker().ask(CfgQuestion.make(
            "unit:%s" % label, "What unit should be used for the %s value?" % label,
            attempt=attempt, max_attempts=_MAX_HITL_ATTEMPTS,
            allowed_values=list(_QUANTITY_UNITS),
            why="the value carries no unit and one is required"))
        if answer is None:
            print()
            return None
        answer = answer.strip()
        if not answer:
            print(f"  No unit given ({attempt}/{_MAX_HITL_ATTEMPTS}) — please specify one ({pretty}).")
            continue
        canonical = canonical_by_lower.get(answer.lower())
        if canonical:
            return canonical
        print(f"  '{answer}' isn't a supported unit ({pretty}) ({attempt}/{_MAX_HITL_ATTEMPTS}) — please try again.")
    return None


# ─────────────────────────────────────────────────────────────────────────
# Missing-requirement resolution + conflict detection against input.json
# ─────────────────────────────────────────────────────────────────────────

def _resolve_requirement_value(requirement: dict[str, Any], known_values: dict[str, Any]) -> Any:
    source_field = requirement.get("source_field")
    if source_field and source_field in known_values:
        return known_values[source_field]
    return known_values.get(_slugify(requirement.get("requirement", "")))


def _find_value_by_keyword(human_requirements: list[dict[str, Any]], known_values: dict[str, Any], keyword: str) -> Optional[Any]:
    for requirement in human_requirements:
        if keyword in requirement.get("requirement", "").lower():
            value = _resolve_requirement_value(requirement, known_values)
            if value is not None:
                return value
    return None


def _sole_leftover_input_key(human_requirements: list[dict[str, Any]], input_request: dict[str, Any]) -> Optional[str]:
    """After every requirement's own claimed source_field and
    "configuration_change" are accounted for, the single input key left
    over structurally MUST be the desired value, regardless of what
    entry-specific wording calls it. None if zero or more than one remain."""
    reserved = {"configuration_change"}
    claimed = {req.get("source_field") for req in human_requirements if req.get("satisfied") and req.get("source_field")}
    leftover = [key for key, value in input_request.items() if key not in reserved and key not in claimed and value is not None]
    return leftover[0] if len(leftover) == 1 else None


def _deterministic_satisfy_desired_value(human_requirements: list[dict[str, Any]], input_request: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic floor under the LLM's own judgment of whether a
    "Desired ... value" requirement is already satisfied — the LLM can
    miss connecting entry-specific phrasing back to a generic input key.
    If there's exactly one leftover input key, any unsatisfied
    "Desired ..." requirement is marked satisfied by it."""
    leftover_key = _sole_leftover_input_key(human_requirements, input_request)
    if leftover_key is None:
        return human_requirements
    for requirement in human_requirements:
        if "desired" in requirement.get("requirement", "").lower() and not requirement.get("satisfied"):
            requirement["satisfied"] = True
            requirement["source_field"] = leftover_key
    return human_requirements


_OPTION_REQUIREMENT_EXCLUDED_KEYS = {"service", "environment"}


def _deterministic_satisfy_option_requirements(human_requirements: list[dict[str, Any]], input_request: dict[str, Any]) -> list[dict[str, Any]]:
    """input.json is the source of truth: an option-based requirement
    (e.g. "Request or limit") must never be asked of HITL if an explicit
    input.json value already answers it unambiguously (e.g.
    configuration_change="req" already means request_or_limit="Request").
    Deliberately never uses the LLM here — a value this clear-cut needs no
    LLM judgment, and per-run LLM inconsistency must never leave an
    already-resolvable value unresolved. An input field whose name
    EXACTLY matches the requirement always takes priority over a hint
    found elsewhere."""
    claimed = {req.get("source_field") for req in human_requirements if req.get("satisfied") and req.get("source_field")}
    for requirement in human_requirements:
        if requirement.get("satisfied"):
            continue
        text = requirement.get("requirement", "")
        if _requirement_options(text) is None:
            continue
        exact_key = _slugify(text)
        ordered_keys = [exact_key] + [k for k in input_request if k != exact_key] if exact_key in input_request else list(input_request)
        for key in ordered_keys:
            value = input_request.get(key)
            if key in _OPTION_REQUIREMENT_EXCLUDED_KEYS or key in claimed or not isinstance(value, str) or not value.strip():
                continue
            match = _deterministic_hitl_option_match(text, value)
            if match is not None:
                requirement["satisfied"] = True
                requirement["source_field"] = key
                requirement["_resolved_value"] = match
                claimed.add(key)
                break
    return human_requirements


def _values_equivalent(original: Any, hitl_value: Any) -> bool:
    """input.json is the immutable source of truth: a HITL answer for a
    field input.json already supplied may only be accepted if it's
    PROVABLY the same value — coerced equality, case/whitespace-
    insensitive string equality, or equal as Kubernetes quantities after
    unit conversion. Anything else is genuinely different."""
    o = _coerce(original) if isinstance(original, str) else original
    h = _coerce(hitl_value) if isinstance(hitl_value, str) else hitl_value
    if o == h:
        return True
    if isinstance(original, str) and isinstance(hitl_value, str) and original.strip().lower() == hitl_value.strip().lower():
        return True
    oq, hq = _parse_k8s_quantity(original), _parse_k8s_quantity(hitl_value)
    return oq is not None and hq is not None and oq == hq


def _looks_like_valid_value(raw_answer: str, pre_checks: list[str]) -> bool:
    """Sanity check for a 'Desired ... value' answer, using the same
    format signals validation.py's pre-check evaluator recognizes — so an
    answer meant for a different question (e.g. "both") is caught and
    re-asked immediately rather than failing much later, less helpfully."""
    combined = " ".join(pre_checks).lower()
    has_format_signal = "integer" in combined or "percentage" in combined or ("kubernetes" in combined and "quantity" in combined) or bool(_RANGE_IN_TEXT_RE.search(combined))
    if not has_format_signal:
        return True
    value = _coerce(raw_answer)
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    return _is_valid_k8s_quantity(value) or _is_percentage_string(value)


def _original_value_for_requirement(requirement: str, human_requirements: list[dict[str, Any]], input_request: dict[str, Any]) -> tuple[bool, Any]:
    """Checks whether input.json already supplied a value for `requirement`
    (even if upstream satisfaction-checking wrongly called it missing):
    an exact key match on the requirement's own slug, or — for "Desired
    ... value" — the sole-leftover-key reasoning. Returns (found, value)."""
    slug = _slugify(requirement)
    if slug in input_request and input_request[slug] is not None:
        return True, input_request[slug]
    if "desired" in requirement.lower():
        leftover_key = _sole_leftover_input_key(human_requirements, input_request)
        if leftover_key is not None:
            return True, input_request[leftover_key]
    return False, None


def prompt_for_missing_requirements(
    missing: list[str], human_requirements: list[dict[str, Any]], input_request: dict[str, Any], pre_checks: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    """Prompts for each missing requirement (up to _MAX_HITL_ATTEMPTS each).
    An option-based requirement's answer must resolve to one of its
    options (fuzzy match, then LLM) or is re-asked. A free-text "Desired
    ... value" is sanity-checked against pre_checks' declared format when
    one is declared. Once resolved, checks whether input.json already had
    an equivalent value for this requirement (_values_equivalent) — if so
    the ORIGINAL input.json value is kept as truth; if the two genuinely
    differ, raises ConflictError rather than silently picking either.
    Returns None (INCOMPLETE) if any requirement exhausts its attempts."""
    pre_checks = pre_checks or []
    print("\nRequired information missing:")
    for requirement in missing:
        print(f"  • {requirement}")
    print()

    collected: dict[str, Any] = {}
    for requirement in missing:
        options = _requirement_options(requirement)
        is_desired_value = options is None and "desired" in requirement.lower()
        found_original, original_value = _original_value_for_requirement(requirement, human_requirements, input_request)
        for attempt in range(1, _MAX_HITL_ATTEMPTS + 1):
            value = current_asker().ask(CfgQuestion.make(
                requirement, requirement,
                attempt=attempt, max_attempts=_MAX_HITL_ATTEMPTS,
                allowed_values=list(options or []),
                open_ended=options is None,
                why="config_changes.json lists this under human_must_provide "
                    "and the request did not supply it"))
            if value is None:
                print()
                return None
            value = value.strip()
            if not value:
                print(f"  No answer given ({attempt}/{_MAX_HITL_ATTEMPTS}) — please provide a value.")
                continue
            if is_desired_value and not _looks_like_valid_value(value, pre_checks):
                print(f"  '{value}' doesn't look like a valid value for this field ({attempt}/{_MAX_HITL_ATTEMPTS}) — please try again.")
                continue
            resolved = resolve_hitl_option(requirement, value)
            if resolved is not None:
                if found_original:
                    if _values_equivalent(original_value, resolved):
                        collected[_slugify(requirement)] = original_value
                    else:
                        raise ConflictError(requirement, original_value, resolved)
                else:
                    collected[_slugify(requirement)] = _coerce(resolved)
                break
            print(f"  I couldn't match '{value}' to one of the options ({', '.join(options)}) ({attempt}/{_MAX_HITL_ATTEMPTS}) — please try again.")
        else:
            return None
    return collected
