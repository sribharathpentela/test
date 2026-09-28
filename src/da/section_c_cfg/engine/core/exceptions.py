#!/usr/bin/env python3
"""
exceptions.py
-------------
Shared exception classes for the whole pipeline (validation.py,
llm_hitl.py, github.py, mutation.py, run.py). A true
leaf module — depends on nothing else in this package.
"""

from __future__ import annotations

from typing import Any


class ValidationToolError(Exception):
    """Base class for all errors raised by this tool."""


class GroqApiError(ValidationToolError):
    """Groq could not be reached, or did not return usable JSON."""


class GitOpsAccessError(ValidationToolError):
    """The GitOps repo could not be reached."""


class ConflictError(ValidationToolError):
    """A HITL-collected value conflicts with a value input.json already gave.
    input.json is the source of truth — HITL may fill missing information or
    resolve which field an existing value applies to, never silently replace it."""

    def __init__(self, field: str, original_value: Any, hitl_value: Any):
        self.field = field
        self.original_value = original_value
        self.hitl_value = hitl_value
        super().__init__(
            f"CONFLICT on '{field}': input.json already specifies {original_value!r}, but the "
            f"human-provided answer was {hitl_value!r} — these could not be reconciled by any "
            f"approved deterministic normalization rule."
        )


class MutationError(ValidationToolError):
    """Step 4a could not safely proceed; no file was written."""
