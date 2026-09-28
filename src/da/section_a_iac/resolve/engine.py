"""The resolution loop and the closure proof. `01-algorithm.md` section 8.

    for V in contract.variables:  resolve(V)
    if ASK set is empty: render, else emit a slot schema for the next HITL round

Render only if **every** declared variable landed in exactly one branch and the
ASK set is empty. An unclassified variable is a bug in the algorithm, not a
value to guess -- halt.

Iterating `contract.variables` (never the template) is ADR 0008: storage_account
sets 13 variables the template never mentions, and a template-driven loop misses
all of them.
"""

from typing import Dict, List, Optional

from ..contracts.models import ModuleContract, PlatformContext, TemplateMap
from .branches import resolve
from .facts import FactsStore
from .models import (Action, Branch, ClosureProof, FactSource, Resolution)


def resolve_module(contract: ModuleContract,
                   template: Optional[TemplateMap],
                   platform: PlatformContext,
                   facts: Optional[FactsStore] = None) -> ClosureProof:
    """Resolve every declared variable and return the closure proof.

    Pure recomputation: this reads the facts store and writes nothing to it, so
    a HITL round can simply call it again with more facts.
    """
    facts = facts if facts is not None else FactsStore(contract)
    proof = ClosureProof(module_path=contract.module_path)

    _propagate_aliases(facts, platform)

    for name in sorted(contract.variables):
        spec = contract.variables[name]
        try:
            resolution = resolve(spec, facts, contract, template, platform)
        except Exception as exc:                        # noqa: BLE001
            proof.unclassified.append(name)
            proof.halts.append("%s: resolver raised %s: %s"
                               % (name, type(exc).__name__, exc))
            continue

        if resolution is None:
            proof.unclassified.append(name)
            proof.halts.append(
                "%s reached no branch -- this is an algorithm bug, not a value "
                "to guess" % name)
            continue

        proof.resolutions[name] = resolution
        if resolution.action == Action.ASK:
            proof.ask_set.append(name)
        elif resolution.action == Action.UNWRAP and not facts.has(name):
            # A blueprint offers a shape; it does not demand entries. It blocks
            # only when the variable is genuinely required -- in practice the
            # primary payload map, whose emptiness means the module provisions
            # nothing.
            #
            # The load-balancer template blueprints backend_pools, lb_rules,
            # lb_probes, nat_rules and frontend_ip_configurations. Blocking on
            # all five turned a ticket that asked for one load balancer into six
            # questions about collections it never mentioned.
            if spec.is_required:
                proof.ask_set.append(name)
        elif resolution.action == Action.SECRET and not facts.has(name):
            proof.ask_set.append(name)

    proof.warnings.extend(facts.warnings)
    for rejection in facts.rejections:
        proof.halts.append("rejected fact: %s" % rejection)

    proof.ask_set = sorted(set(proof.ask_set))
    _check_closure(contract, proof)

    proof.message = "%s: %d variables, %d branches used, ASK=%d, %s" % (
        contract.module_path, len(contract.variables),
        len(proof.by_branch()), len(proof.ask_set),
        "may render" if proof.may_render else "blocked")
    return proof


def _check_closure(contract: ModuleContract, proof: ClosureProof) -> None:
    """Every declared variable must land in exactly one branch."""
    missing = sorted(set(contract.variables) - set(proof.resolutions)
                     - set(proof.unclassified))
    for name in missing:
        proof.unclassified.append(name)
        proof.halts.append("%s was never resolved" % name)
    proof.unclassified = sorted(set(proof.unclassified))


def _propagate_aliases(facts: FactsStore, platform: PlatformContext) -> None:
    """Overriding one member of an alias pair must set the other.

    `common.hcl:37` has `region = local.location`: one value under two names. A
    ticket saying "deploy to westus" has to emit both, or the resource lands in
    westus with a name still claiming eastus (ADR 0002).
    """
    for pair in platform.alias_pairs:
        present = [name for name in pair if facts.user_stated(name)]
        if len(present) != 1:
            continue
        source_name = present[0]
        record = sorted(facts.user_stated(source_name),
                        key=lambda r: r.round)[-1]
        for name in pair:
            if name == source_name:
                continue
            facts.append(
                name, record.value, record.source,
                evidence=record.evidence, round=record.round)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def render_provenance_table(proof: ClosureProof) -> str:
    """The closure proof as the markdown table that goes in the PR body."""
    rows = proof.provenance_table()
    width = max([len(r["variable"]) for r in rows] + [8])
    lines = ["| %-*s | branch | source | value | note |" % (width, "variable"),
             "|%s|---|---|---|---|" % ("-" * (width + 2))]
    for row in rows:
        lines.append("| %-*s | %s | %s | %s | %s |" % (
            width, row["variable"], row["branch"], row["source"],
            row["value"], row["note"]))
    return "\n".join(lines)


def summarise(proof: ClosureProof) -> Dict[str, object]:
    counts: Dict[str, int] = {}
    for resolution in proof.resolutions.values():
        counts[resolution.action.value] = counts.get(resolution.action.value, 0) + 1
    return {
        "module": proof.module_path,
        "variables": len(proof.resolutions) + len(proof.unclassified),
        "by_branch": {("B%d" % k): len(v) for k, v in proof.by_branch().items()},
        "by_action": counts,
        "ask": proof.ask_set,
        "unclassified": proof.unclassified,
        "disclosures": sum(1 for r in proof.resolutions.values() if r.disclose),
        "may_render": proof.may_render,
    }
