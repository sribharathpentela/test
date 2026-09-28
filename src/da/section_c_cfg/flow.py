"""Section C as a state machine, shaped like `Step4A`.

    flow = ConfigChangeFlow(request)
    result = flow.advance()
    while result.status is CfgStatus.NEEDS_HITL:
        result = flow.submit({result.question.key: answer})

Three terminal states and nothing else, the same three `Step4A` has: the run
is ready, a human must answer, or it halted and will not guess.

**Every round replays the whole run from the beginning.** That looks wasteful
and is the point -- it is what `Step4A` does, for the reason
[ADR 0010](../../../docs/decisions/0010-validation-on-facts-store-write-path.md)
gives: an append-only store means there is no partial state for a late answer
to corrupt and no ordering dependency between rounds. Suspending a half-run
mid-call-stack would have both problems, and would need the 686 lines of
`llm_hitl.py` restructured to allow it.

Section C asks one question at a time rather than a set. That is not a design
choice, it is what the code is: `validation.py` discovers the next thing it
needs only after resolving the last, so a question set cannot be computed up
front the way a slot schema can. `Step4A` returns a `HitlRequest` holding many
questions; this returns one, and the caller loops.
"""

import io
from contextlib import redirect_stdout
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from .questions import (AnswerStore, CfgQuestion, NeedsAnswer, SuspendingAsker,
                        using_asker)

# A run that asks this many questions is looping rather than converging.
MAX_ROUNDS = 12


class CfgStatus(str, Enum):
    COMPLETE = "complete"
    NEEDS_HITL = "needs_hitl"
    HALT = "halt"


class CfgResult(BaseModel):
    status: CfgStatus
    round: int = 0
    question: Optional[CfgQuestion] = None
    # The raw result from the engine -- validation verdict, mutation status,
    # and the simulated PR plan. Left untyped because it is the engine's own
    # dict and typing it here would be a second place to keep in step.
    outcome: Optional[Dict[str, Any]] = None
    asked: List[str] = Field(default_factory=list)
    # Whatever the engine printed this round, captured rather than emitted.
    trace: str = ""
    message: str = ""

    @property
    def needs_human(self) -> bool:
        return self.status is CfgStatus.NEEDS_HITL


def _default_runner(request: Dict[str, Any]) -> Dict[str, Any]:
    """Validate, mutate, and produce the simulated PR.

    Imported lazily: the engine pulls in `ruamel.yaml` and the GitHub reader,
    and a caller that only wants the state machine should not pay for them.
    """
    from .engine.workflow.run import run_config_change
    return run_config_change(request, verbose=False, execute=True)


class ConfigChangeFlow(object):
    """Holds the answers that survive a HITL round trip."""

    def __init__(self,
                 request: Dict[str, Any],
                 runner: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
                 answers: Optional[AnswerStore] = None,
                 max_rounds: int = MAX_ROUNDS):
        self.request = dict(request)
        # Injected so a test can drive the state machine without GitHub, Groq
        # or a real manifest -- the same reason `DAPipeline` takes a git client
        # and a completer.
        self.runner = runner or _default_runner
        self.answers = answers or AnswerStore()
        self.max_rounds = max_rounds
        self.round = 0

    def advance(self) -> CfgResult:
        """Replay the run against every answer known so far."""
        if self.round >= self.max_rounds:
            return CfgResult(
                status=CfgStatus.HALT, round=self.round,
                message="Asked %d questions without converging; halting rather "
                        "than asking again." % self.round)

        asker = SuspendingAsker(self.answers)

        # The engine writes its trace straight to stdout, 139 print() calls of
        # it. Under a supervisor that is wrong twice over: it interleaves with
        # the caller's own output, since the caller reports stages only after
        # this returns; and because every round replays the run from the start,
        # round two reprints everything round one printed.
        #
        # So it is captured, not emitted. The caller decides whether a trace is
        # worth showing, which is the same thing a UI will need. Section A has
        # never printed from inside the pipeline -- this is Section C reaching
        # the shape Section A already has, the cheap way. The proper fix is an
        # event seam like `questions.py`'s asker, and it should be designed
        # against a real UI rather than guessed at now.
        buffer = io.StringIO()
        try:
            with using_asker(asker), redirect_stdout(buffer):
                outcome = self.runner(self.request)
        except NeedsAnswer as needed:
            self.round += 1
            return CfgResult(
                status=CfgStatus.NEEDS_HITL, round=self.round,
                question=needed.question,
                asked=[q.key for q in asker.asked],
                trace=buffer.getvalue(),
                message="Waiting on %r." % needed.question.field)
        except Exception as exc:                # noqa: BLE001 - reported, not raised
            return CfgResult(
                status=CfgStatus.HALT, round=self.round,
                asked=[q.key for q in asker.asked],
                trace=buffer.getvalue(),
                message="The config-change run failed: %s" % exc)

        return CfgResult(
            status=CfgStatus.COMPLETE, round=self.round, outcome=outcome,
            asked=[q.key for q in asker.asked],
            trace=buffer.getvalue(),
            message=_summarise(outcome))

    def submit(self, answers: Dict[str, str]) -> CfgResult:
        """Add answers and replay. The store refuses to overwrite (ADR 0010)."""
        try:
            self.answers.add_all(answers)
        except ValueError as exc:
            return CfgResult(status=CfgStatus.HALT, round=self.round,
                             message=str(exc))
        return self.advance()


def _summarise(outcome: Dict[str, Any]) -> str:
    validation = (outcome or {}).get("validation") or {}
    mutation = (outcome or {}).get("mutation") or {}
    github = (outcome or {}).get("github") or {}
    return "validation %s; mutation %s; PR %s" % (
        validation.get("status", "?"),
        mutation.get("status", "?"),
        github.get("status", "?"))
