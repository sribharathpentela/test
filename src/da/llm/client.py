"""The one place the agent talks to a language model.

There were three. `intake/parser.py` built a `Groq` client in its constructor,
`slots/filler.py` built one inside `groq_completer`, and Section C's
`llm_hitl.py` built one per call. All three sent the same request -- system
prompt, user prompt, temperature 0, JSON mode -- and disagreed on everything
around it: one retried a rate limit, one pinned a seed, one wrapped failures in
its own exception type and the others let the vendor's through.

Three copies of a call is three places to fix a bug in. It also meant the model
could not be changed without editing three files in two sections, which is the
opposite of what `src/da/config.py` promises about moving environments.

**Still Groq.** Swapping to a Databricks foundation model is a change to
`groq_completer` and the settings that pick it -- one file, once the endpoint
can be verified from inside the workspace. That is why this exists now rather
than after: the swap is not a rewrite if there is only one client to swap.

The contract is `Completer`, which Section A's slot filler already used and the
pipeline already injects:

    completer(system_prompt, user_prompt) -> str

A function rather than a class, because every caller wants exactly one thing
and a fake is then a lambda.
"""

import json
import os
import time
from typing import Any, Callable, Dict, Optional

# `(system_prompt, user_prompt) -> raw response text`
Completer = Callable[[str, str], str]

DEFAULT_MODEL = "openai/gpt-oss-120b"
MAX_RETRIES = 4


class LLMError(Exception):
    """The model could not be reached, or did not answer usably."""


def model_name(override: Optional[str] = None) -> str:
    return override or os.getenv("GROQ_MODEL") or DEFAULT_MODEL


def groq_completer(model: Optional[str] = None,
                   api_key: Optional[str] = None,
                   max_retries: int = MAX_RETRIES) -> Completer:
    """A `Completer` backed by Groq: temperature 0, seeded, JSON mode.

    `groq` is imported inside the function so that everything not calling a
    model -- which is most of the agent, and all of its offline tests -- runs
    with no key, no package and no network.
    """
    from groq import Groq                       # noqa: PLC0415 - deliberate

    key = api_key or os.getenv("GROQ_API_KEY")
    if not key:
        raise LLMError(
            "GROQ_API_KEY is not set. See .env.example; the agent does not "
            "guess a credential.")

    client = Groq(api_key=key)
    name = model_name(model)

    def complete(system_prompt: str, user_prompt: str) -> str:
        # A slot schema for a large module runs to a few thousand tokens, so a
        # run over several tickets can trip the per-minute limit. Retrying is
        # safe: temperature is 0 and the call has no side effects.
        delay = 1.0
        for attempt in range(max_retries + 1):
            try:
                response = client.chat.completions.create(
                    model=name,
                    temperature=0.0,
                    seed=0,
                    response_format={"type": "json_object"},
                    messages=[{"role": "system", "content": system_prompt},
                              {"role": "user", "content": user_prompt}],
                )
                return response.choices[0].message.content or ""
            except Exception as exc:                      # noqa: BLE001
                retryable = "rate_limit" in str(exc) or "429" in str(exc)
                if not retryable or attempt == max_retries:
                    raise LLMError("Groq request failed: %s" % exc) from exc
                time.sleep(delay)
                delay *= 2
        return ""

    return complete


_default: Optional[Completer] = None


def default_completer() -> Completer:
    """The process-wide completer, built once.

    Built lazily rather than at import: importing a module must not require a
    credential, or the offline suite cannot import the code it tests.
    """
    global _default
    if _default is None:
        _default = groq_completer()
    return _default


def set_default_completer(completer: Optional[Completer]) -> None:
    """Override the process-wide completer, or reset it with None.

    For a caller that builds its own -- a test, or a supervisor that has
    already decided which model to spend."""
    global _default
    _default = completer


def complete_json(system_prompt: str, user_prompt: str,
                  completer: Optional[Completer] = None) -> Dict[str, Any]:
    """Ask for JSON and get a dict, or fail saying what came back instead.

    Every caller asked in JSON mode and then parsed; the ones that did not
    report the raw response on a parse failure left nothing to debug with.
    """
    raw = (completer or default_completer())(system_prompt, user_prompt)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMError(
            "The model did not return valid JSON: %s\nRaw response: %s"
            % (exc, (raw or "")[:500])) from exc
