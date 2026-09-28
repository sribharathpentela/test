"""The facts store: append-only, path-keyed, recomputed from scratch every round.

`validate_value()` is a gate *inside* `append()`, not a call-site concern
(ADR 0010). If validation were per-call-site you would end up with six copies
that drift; on the write path it is structurally impossible for an unvalidated
value to exist in the store.

Every HITL round re-runs resolution from this store. Because it is append-only
there is no partial state for a new answer to corrupt and no ordering dependency
between rounds -- that is what makes multi-round HITL safe rather than merely
convenient.
"""

import datetime
from typing import Any, Dict, List, Optional

from ..contracts.models import ModuleContract
from .models import FactRecord, FactSource, Trust, ValidationOutcome, Verdict
from .validate import validate_value


class FactRejected(Exception):
    """A value failed the ladder and did not enter the store."""

    def __init__(self, path: str, outcome: ValidationOutcome):
        self.path = path
        self.outcome = outcome
        super(FactRejected, self).__init__(
            "%s rejected at rung %s: %s"
            % (path, outcome.rung.value if outcome.rung else "?", outcome.message))


class FactsStore(object):
    """Append-only. Later rounds shadow earlier ones; nothing is overwritten."""

    def __init__(self, contract: Optional[ModuleContract] = None):
        self.contract = contract
        self.records: List[FactRecord] = []
        self.warnings: List[str] = []
        self.rejections: List[str] = []

    # -- write path ---------------------------------------------------------

    def append(self, path: str, value: Any, source: FactSource,
               evidence: Optional[str] = None, round: int = 0,
               strict: bool = False) -> ValidationOutcome:
        """The only way a fact enters the store. Runs the ladder first.

        Platform values are validated too -- being platform-sourced is not a
        trust exemption (ADR 0010).
        """
        outcome = validate_value(path, value, self.contract)

        if outcome.verdict == Verdict.REJECT:
            self.rejections.append("%s: %s" % (path, outcome.message))
            if strict:
                raise FactRejected(path, outcome)
            return outcome

        if outcome.verdict == Verdict.WARN:
            self.warnings.append(outcome.message)

        self.records.append(FactRecord(
            path=path,
            value=value,
            source=source,
            trust=Trust.VERIFIED if source == FactSource.PLATFORM
            else Trust.UNTRUSTED,
            evidence=evidence,
            round=round,
            ts=datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds"),
        ))
        return outcome

    # -- read path ----------------------------------------------------------

    def get(self, path: str) -> Optional[FactRecord]:
        """The winning record for a path: the latest round wins."""
        matches = [r for r in self.records if r.path == path]
        if not matches:
            return None
        return sorted(matches, key=lambda r: r.round)[-1]

    def for_variable(self, name: str) -> List[FactRecord]:
        """Every record under a variable, including dotted leaf paths."""
        return [r for r in self.records
                if r.path == name or r.path.startswith(name + ".")]

    def has(self, name: str) -> bool:
        return bool(self.for_variable(name))

    def user_stated(self, name: str) -> List[FactRecord]:
        """Branch 1 candidates -- facts a human actually stated."""
        return [r for r in self.for_variable(name) if r.source.is_user_stated]

    def paths(self) -> List[str]:
        return sorted({r.path for r in self.records})

    def snapshot(self) -> Dict[str, Any]:
        return {r.path: r.value for r in
                sorted(self.records, key=lambda r: r.round)}

    def __len__(self) -> int:
        return len(self.records)
