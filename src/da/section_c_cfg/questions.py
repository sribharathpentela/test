"""Section C's HITL, inverted to match Section A.

Section C was built as a console tool: six `prompt_for_*` functions in
`engine/core/llm_hitl.py` each blocked on `input()`. Section A does the
opposite -- `Step4A` returns a question and is called again with the answer,
because a supervisor drives it and there is no stdin to read
([ADR 0022](../../../docs/decisions/0022-da-never-prompts.md)).

The two were reconciled by taking Section A's own mechanism rather than
inventing one. `Step4A` does not suspend and resume a half-finished
computation; it **recomputes every round from scratch** against an append-only
store, which is what [ADR 0010](../../../docs/decisions/0010-validation-on-facts-store-write-path.md)
argues makes multi-round HITL safe rather than merely convenient: there is no
partial state for a late answer to corrupt, and no ordering dependency between
rounds.

So Section C works the same way. An `AnswerStore` accumulates answers; a run
replays from the beginning each time; and the first question without an answer
raises `NeedsAnswer` to end the round. The 686 lines of interpretation logic in
`llm_hitl.py` -- deterministic match first, bounded LLM fallback, never a
silent guess -- are untouched. Only the six `input()` calls became
`current_asker().ask(...)`.

Three askers, one seam:

| asker | `ask` returns | used by |
|---|---|---|
| `ConsoleAsker` | what was typed | the CLI, unchanged |
| `ScriptedAsker` | a canned answer | tests, offline |
| `SuspendingAsker` | a stored answer, or raises `NeedsAnswer` | the pipeline |

`ConsoleAsker` is the default, so anything not going through the pipeline keeps
behaving exactly as it did.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class CfgQuestion(BaseModel):
    """One thing Section C needs a human to supply.

    `key` is what an answer is filed under, and it carries the attempt number.
    That is not decoration: a replayed run must be able to ask the *same*
    question again after a bad answer, and an unchanged key would just hand
    back the bad answer forever. `field` is the stable part, for a caller that
    wants to group attempts together.
    """

    key: str
    field: str
    prompt: str
    attempt: int = 1
    max_attempts: int = 1
    allowed_values: List[str] = Field(default_factory=list)
    # Free text is accepted where the answer cannot be enumerated -- a desired
    # CPU value, a replica count.
    open_ended: bool = False
    why: str = ""
    context_lines: List[str] = Field(default_factory=list)

    @staticmethod
    def make(field: str, prompt: str, attempt: int = 1, **kw) -> "CfgQuestion":
        return CfgQuestion(key="%s#%d" % (field, attempt), field=field,
                           prompt=prompt, attempt=attempt, **kw)


class NeedsAnswer(Exception):
    """Raised to end a round when a question has no stored answer.

    Not an error. It is how a replayed run stops at the first thing it does not
    know, the same place `Step4A` returns `NEEDS_HITL`.
    """

    def __init__(self, question: CfgQuestion):
        super().__init__("needs an answer for %r" % question.field)
        self.question = question


class AnswerStore(BaseModel):
    """Append-only, for the reason ADR 0010 gives.

    An answer is never overwritten, so replaying a run can only ever add
    information. A second answer to the same key means the caller is confused
    about which round it is in, and that is worth a refusal rather than a
    silent overwrite.
    """

    answers: Dict[str, str] = Field(default_factory=dict)

    def add(self, key: str, value: str) -> None:
        if key in self.answers:
            raise ValueError(
                "'%s' already answered with %r; the store is append-only so a "
                "replay cannot change it." % (key, self.answers[key]))
        self.answers[key] = value

    def add_all(self, new: Dict[str, str]) -> None:
        for key, value in new.items():
            self.add(key, value)

    def get(self, key: str) -> Optional[str]:
        return self.answers.get(key)


# -- the askers -----------------------------------------------------------

class ConsoleAsker(object):
    """What Section C did before, and still does outside the pipeline."""

    def ask(self, question: CfgQuestion) -> Optional[str]:
        for line in question.context_lines:
            print(line)
        try:
            return input("  %s\n  > " % question.prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            return None


class ScriptedAsker(object):
    """Answers from a dict, for tests. Unknown keys give up rather than block."""

    def __init__(self, answers: Dict[str, str], by_field: bool = True):
        self.answers = dict(answers)
        # Tests care about *what* is asked, not which attempt, so a field name
        # matches any attempt of it unless an exact key is given.
        self.by_field = by_field
        self.asked: List[CfgQuestion] = []

    def ask(self, question: CfgQuestion) -> Optional[str]:
        self.asked.append(question)
        if question.key in self.answers:
            return self.answers[question.key]
        if self.by_field and question.field in self.answers:
            return self.answers[question.field]
        return None


class SuspendingAsker(object):
    """Returns a stored answer, or ends the round by raising `NeedsAnswer`."""

    def __init__(self, store: AnswerStore):
        self.store = store
        self.asked: List[CfgQuestion] = []

    def ask(self, question: CfgQuestion) -> Optional[str]:
        self.asked.append(question)
        stored = self.store.get(question.key)
        if stored is not None:
            return stored
        raise NeedsAnswer(question)


# -- the seam -------------------------------------------------------------

_current: ContextVar = ContextVar("section_c_asker", default=None)


def current_asker():
    """The asker in force. Defaults to the console, so the CLI is unchanged."""
    return _current.get() or ConsoleAsker()


@contextmanager
def using_asker(asker):
    """Install an asker for the duration of one run.

    A context variable rather than a threaded-through parameter: the six prompt
    sites sit deep inside `validation.py`'s call tree, and adding an argument to
    every frame between them would be a far larger edit to untested code than
    the inversion itself. A `ContextVar` also stays correct if two runs ever
    share a process.
    """
    token = _current.set(asker)
    try:
        yield asker
    finally:
        _current.reset(token)
