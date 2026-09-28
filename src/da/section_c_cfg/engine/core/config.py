#!/usr/bin/env python3
"""
config.py
---------
Environment/config loading, shared constants, and small dependency-free
utilities (string coercion, Kubernetes quantity parsing, verbose-trace
printing) used across every other module in this pipeline. Nothing here
depends on any other module in this package — every other module may
safely import from here with no risk of a circular import.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Optional

# config.py now lives one level deeper (config_change_modules/core/), so
# BASE_DIR goes up two levels to config_change_modules/ — where
# config_changes.json, input.json, and mutation_output/ actually live.
BASE_DIR = Path(__file__).resolve().parent.parent


# Environment, paths and the GitOps repo are the pipeline's, not this
# section's: a section is an aid to reading, and anything it needs *in order to
# run* belongs to the agent that runs it. This module had its own dotenv
# loader, its own name resolution and its own repo defaults, which is how it
# ended up reading a different `.env` from Section A.
from ....config import (MissingConfig, OUTPUT_DIR, REPO_ROOT,  # noqa: E402
                        gitops_settings)

MUTATION_OUTPUT_DIR = OUTPUT_DIR / "config_changes"

# The request processed when none is named. A fixture, kept with the other
# fixtures rather than beside the code that reads it.
SAMPLE_REQUEST = REPO_ROOT / "src" / "da" / "samples" / "config_change_request.json"

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b").strip()
CONFIG_CHANGES_FILE = Path(os.environ.get("CONFIG_CHANGES_FILE", str(BASE_DIR / "config_changes.json")))

# Bound at import, as module constants, because `github.py` reads them by name.
_gitops = gitops_settings()
GITHUB_TOKEN = _gitops.pat
GIT_API_BASE_URL = _gitops.api_base_url
GITOPS_REPO = _gitops.repo
GITOPS_REPO_BRANCH = _gitops.branch


def require_gitops_config() -> None:
    """Fail loudly, naming what to set. Called before the first repo read."""
    _gitops.require()

_MAX_HITL_ATTEMPTS = 3  # every interactive clarification loop gets this many tries; a blank answer counts
_FUZZY_MATCH_CUTOFF = 0.75  # similarity-ratio floor for typo-tolerant matching (e.g. "limt" -> "limit")

# Filenames/paths that must never be mutated by this tool, regardless of
# any model judgment — a deterministic backstop so a plaintext secret is
# never written.
_SECRET_PATH_MARKERS = ("secret", "sealedsecret")


def use_utf8_console() -> None:
    """Make stdout/stderr UTF-8 regardless of the platform default.

    Every banner, bullet and rule in this tool is non-ASCII. On Windows the
    console defaults to cp1252, which cannot encode any of them, so the first
    `_step()` call raises `UnicodeEncodeError` and takes the whole run with it
    -- before a single check has run. Called from the entry points rather than
    at import, so importing this module never reconfigures a caller's streams.
    """
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):       # not a real tty, or too old
            pass


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return slug or "value"


def _coerce(value: str) -> Any:
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


# ─────────────────────────────────────────────────────────────────────────
# Kubernetes quantity (CPU/memory/storage) parsing — pure math/regex, no
# GitHub/LLM dependency, shared by validation.py (pre-checks, unit
# normalization) and llm_hitl.py (HITL answer sanity-checking).
# ─────────────────────────────────────────────────────────────────────────

_BINARY_QUANTITY_SUFFIXES = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_DECIMAL_QUANTITY_SUFFIXES = {"k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18, "m": 1e-3, "u": 1e-6, "n": 1e-9, "": 1.0}
_QUANTITY_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)(Ei|Pi|Ti|Gi|Mi|Ki|E|P|T|G|M|k|m|u|n)?$")
_QUANTITY_ANY_SUFFIX_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*$")
_RANGE_IN_TEXT_RE = re.compile(r"(\d+)\s*[-–]\s*(\d+)")
_QUANTITY_UNITS = ["Ki", "Mi", "Gi", "Ti"]  # units realistically relevant to memory/storage sizing


def _parse_k8s_quantity(value: Any) -> Optional[float]:
    """Parses a Kubernetes resource quantity (e.g. "500m", "2", "512Mi")
    into a plain float in its base unit. Returns None if invalid."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    match = _QUANTITY_RE.match(value.strip())
    if not match:
        return None
    number = float(match.group(1))
    suffix = match.group(2) or ""
    if suffix in _BINARY_QUANTITY_SUFFIXES:
        return number * _BINARY_QUANTITY_SUFFIXES[suffix]
    return number * _DECIMAL_QUANTITY_SUFFIXES.get(suffix, 1.0)


def _is_valid_k8s_quantity(value: Any) -> bool:
    return _parse_k8s_quantity(value) is not None


def _is_percentage_string(value: str) -> bool:
    try:
        number = float(value.strip().rstrip("%"))
    except ValueError:
        return False
    return 0 <= number <= 100


def _numeric_in_range(check_text: str, requested: Any) -> Optional[dict[str, Any]]:
    """If the check text states a numeric range (e.g. "range 1-65535"),
    validates requested against it. The bound always comes from
    config_changes.json's own check text, never hardcoded here."""
    match = _RANGE_IN_TEXT_RE.search(check_text)
    if not match or requested is None:
        return None
    lo, hi = int(match.group(1)), int(match.group(2))
    value: Any = requested
    if "percentage" in check_text and isinstance(value, str) and value.strip().endswith("%"):
        if not _is_percentage_string(value):
            return {"derivation": f"requested_value={requested!r}", "result": "failed", "reason": "not a valid percentage"}
        value = float(value.strip().rstrip("%"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return {"derivation": f"requested_value={requested!r}", "result": "failed", "reason": "value is not numeric"}
    ok = lo <= value <= hi
    return {
        "derivation": f"requested_value={value!r}, allowed range={lo}-{hi}",
        "result": "passed" if ok else "failed",
        "reason": f"within allowed range {lo}-{hi}" if ok else f"outside allowed range {lo}-{hi}",
    }


def _quantity_numeric_and_unit(value: Any) -> Optional[tuple[str, str]]:
    """Splits a quantity into (numeric_str, unit_str) BEFORE and
    SEPARATELY from checking whether that unit is allowed — "unit
    missing", "unit present but unrecognized", and "unit present with
    only a casing mismatch" are three different conditions. Returns None
    if `value` isn't a numeric-prefixed quantity-shaped string at all."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value), ""
    if not isinstance(value, str):
        return None
    match = _QUANTITY_ANY_SUFFIX_RE.match(value.strip())
    return (match.group(1), match.group(2)) if match else None


# ─────────────────────────────────────────────────────────────────────────
# Verbose trace helpers
# ─────────────────────────────────────────────────────────────────────────

_RULE = "─" * 78


def _step(verbose: bool, title: str, timing: dict[str, float]) -> None:
    now = time.perf_counter()
    since_last = now - timing["last"]
    total = now - timing["start"]
    timing["last"] = now
    if verbose:
        print(f"\n{_RULE}")
        print(title)
        print(_RULE)
        print(f"  elapsed +{since_last:.2f}s   total {total:.2f}s")


def _log(verbose: bool, msg: str) -> None:
    if verbose:
        print(msg)


def _kv(verbose: bool, label: str, value: Any) -> None:
    if verbose:
        print(f"  {label:<22} {value}")


def _item(verbose: bool, text: str, indent: int = 2) -> None:
    if verbose:
        print(f"{' ' * indent}• {text}")


def _table(verbose: bool, rows: list[tuple[str, ...]], indent: int = 2) -> None:
    """Prints rows as left-aligned columns (last column not padded)."""
    if not verbose or not rows:
        return
    n_cols = len(rows[0])
    widths = [max(len(row[i]) for row in rows) for i in range(n_cols)]
    pad = " " * indent
    for row in rows:
        cells = [row[i].ljust(widths[i]) if i < n_cols - 1 else row[i] for i in range(n_cols)]
        print(f"{pad}• " + "   ".join(cells))
