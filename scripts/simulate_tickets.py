"""Drive every sample Jira payload through Step 4a and report the variable sets.

    python scripts/simulate_tickets.py              # all samples, stub filler
    DA_LIVE_LLM=1 python scripts/simulate_tickets.py  # use the real model
    python scripts/simulate_tickets.py DEVOPS-777     # one ticket
    python scripts/simulate_tickets.py DEVOPS-888 --render   # also write the unit

`--render` writes `<key>.hcl` (the unit) and `<key>.md` (the PR body carrying
the closure proof) to `.agent/artifacts/`, and only for a ticket whose closure
proof permits it.

This validates the **algorithm**, not the platform: whether the declared
variables of the requested service are segregated correctly, whether values
stated in the ticket land in the right variables, and whether the ASK / CONFIRM
/ MAY sets move as facts arrive.

Repo names, PATs, module refs and path casing are deliberately out of scope
here -- the sample tickets predate the monorepo and name per-service repos that
no longer exist, which does not affect any of the above.

HITL is **simulated**: when a round ends with a non-empty ASK set, the harness
invents an answer, marks it `simulated-hitl`, and discloses it. Those values are
not real human input and are labelled as such everywhere they appear.
"""

import argparse
import glob
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

HON = os.path.join(os.path.expanduser("~"),
                   "OneDrive - KnackForge Soft Solutions Pvt Ltd",
                   "Desktop", "HON")
MODULES_REPO = os.path.join(HON, "pss_fabric_azure_modules_iac_sample")
APP_REPO = os.path.join(HON, "pss_fabric_app_iac_sample")
SAMPLES = os.path.join(HERE, "..", "src", "da", "samples")

MAX_HITL_ROUNDS = 2


# --------------------------------------------------------------------------
# Step 3 stand-in: which module is this ticket asking for?
# --------------------------------------------------------------------------

def resolve_module(text, modules):
    """Match the ticket against the module catalogue.

    A stand-in for Step 3 discovery, deliberately dumb: it scores each module by
    how much of its name appears in the ticket. What matters for this harness is
    only that the *right* module is picked for an in-context ticket and that
    **no** module is picked for an out-of-context one.
    """
    lowered = " ".join(text.lower().split())
    best, best_score = None, 0

    for module_path in modules:
        name = module_path.rsplit("/", 1)[-1]
        tokens = [t for t in name.replace("_", "-").split("-") if len(t) > 1]
        if not tokens:
            continue
        hits = sum(1 for t in tokens if t.lower() in lowered)
        if hits != len(tokens):
            continue                      # every token must appear
        score = sum(len(t) for t in tokens)
        if score > best_score:
            best, best_score = module_path, score
    return best


# --------------------------------------------------------------------------
# Simulated HITL
# --------------------------------------------------------------------------

def simulated_answer(name, spec, template):
    """Invent a plausible answer for a blocking slot.

    Marked and disclosed everywhere. The point is to prove the *loop* closes and
    the sets move, not to guess what a human would really say.
    """
    entry = template.entries.get(name) if template else None
    if entry is not None and entry.example:
        return entry.example.strip().strip('"'), "template example"
    if spec.allowed_values:
        return spec.allowed_values[0], "first authoritative enum value"
    if spec.has_default and spec.default not in (None, "", {}, []):
        return spec.default, "module default"

    kind = spec.type_tree.kind.value if spec.type_tree else "string"
    if kind == "bool":
        return True, "invented"
    if kind == "number":
        return 1, "invented"
    if kind in ("map", "object"):
        return {}, "invented"
    if kind in ("list", "set"):
        return [], "invented"
    return "simulated-%s" % name.replace("_", "-"), "invented"


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def tier_counts(schema):
    counts = {"ASK": 0, "CONFIRM": 0, "MAY": 0}
    for slot in schema.slots.values():
        counts[slot.tier.value] += 1
    return counts


def run_one(path, platform, modules, completer, verbose=True, render_dir=None):
    from da.section_a_iac.contracts.module_contract import build_contract
    from da.section_a_iac.contracts.reconcile import slug_for_module
    from da.section_a_iac.contracts.template_map import build_template_map
    from da.section_a_iac.resolve.engine import resolve_module as resolve
    from da.section_a_iac.resolve.facts import FactsStore
    from da.section_a_iac.resolve.models import Action, FactSource
    from da.section_a_iac.slots.filler import SlotFiller, apply_to_facts
    from da.section_a_iac.slots.schema import build_slot_schema

    payload = json.load(io.open(path, encoding="utf-8-sig"))
    ticket = payload.get("jira", payload)
    key = ticket.get("key", os.path.basename(path))
    text = "%s\n\n%s" % (ticket.get("summary", ""), ticket.get("description", ""))

    print()
    print("=" * 78)
    print("%s  -  %s" % (key, ticket.get("summary", "")))
    print("=" * 78)

    # ---- Step 3 stand-in ---------------------------------------------------
    module_path = resolve_module(text, modules)
    if module_path is None:
        print("  module            : NONE MATCHED")
        print("  verdict           : OUT OF CONTEXT -> HALT before Step 4a")
        print("                      no module in the catalogue provides this;")
        print("                      DA stops instead of forcing a fit.")
        return {"key": key, "module": None, "out_of_context": True,
                "closed": False}
    print("  module            : %s" % module_path)

    contract = build_contract(
        os.path.join(MODULES_REPO, *module_path.split("/")), module_path)
    slug = slug_for_module(module_path)
    template_path = os.path.join(MODULES_REPO, "docs", "examples", slug,
                                 "terragrunt.template.hcl")
    template = (build_template_map(template_path, slug)
                if os.path.isfile(template_path) else None)
    print("  variables declared: %d" % len(contract.variables))

    facts = FactsStore(contract)
    rounds = []

    # ---- round 0 -----------------------------------------------------------
    proof = resolve(contract, template, platform, facts)
    schema = build_slot_schema(contract, proof, template, platform)
    counts = tier_counts(schema)
    print()
    print("  ROUND 0 (nothing known)")
    print("     branches used  : %s" % {("B%d" % k): len(v)
                                        for k, v in proof.by_branch().items()})
    print("     ASK / CONFIRM / MAY : %(ASK)d / %(CONFIRM)d / %(MAY)d" % counts)
    print("     blocking       : %s" % (proof.ask_set or "none"))
    rounds.append(("round 0", counts, list(proof.ask_set)))

    # ---- Stage 2: read the ticket -----------------------------------------
    result = SlotFiller(completer).fill(text, schema)
    print()
    print("  STAGE 2 - values read from the ticket")
    if not result.accepted:
        print("     (nothing extracted)")
    for slot in sorted(result.accepted, key=lambda s: s.name):
        print("     %-30s -> %-40s = %r"
              % (slot.name, slot.path, slot.value))
    dropped = [s for s in result.dropped if s.drop_reason.value != "null"]
    for slot in dropped:
        print("     DROPPED %-22s = %-22r (%s)"
              % (slot.name, slot.value, slot.drop_reason.value))
    if result.invented_keys:
        print("     INVENTED KEYS  : %s" % result.invented_keys)
    rejected = apply_to_facts(result, facts, FactSource.JIRA, round=0)
    for message in rejected:
        print("     REJECTED BY THE LADDER: %s" % message)

    # ---- HITL rounds -------------------------------------------------------
    simulated = []
    for turn in range(1, MAX_HITL_ROUNDS + 1):
        proof = resolve(contract, template, platform, facts)
        schema = build_slot_schema(contract, proof, template, platform)
        counts = tier_counts(schema)
        label = "round %d" % turn
        print()
        print("  ROUND %d (after %s)" % (turn, "the ticket" if turn == 1
                                         else "HITL turn %d" % (turn - 1)))
        print("     ASK / CONFIRM / MAY : %(ASK)d / %(CONFIRM)d / %(MAY)d" % counts)
        print("     blocking       : %s" % (proof.ask_set or "none"))
        rounds.append((label, counts, list(proof.ask_set)))

        if not proof.ask_set:
            break

        print("     HITL TRIGGERED - simulating human answers:")
        for name in proof.ask_set:
            spec = contract.variables[name]
            value, basis = simulated_answer(name, spec, template)
            outcome = facts.append(name, value, FactSource.HITL,
                                   evidence="simulated-hitl", round=turn)
            flag = "" if outcome.accepted else "  REJECTED: %s" % outcome.message
            print("        %-28s = %-24r [simulated, %s]%s"
                  % (name, value, basis, flag))
            simulated.append((name, value, basis))

    # ---- closure -----------------------------------------------------------
    proof = resolve(contract, template, platform, facts)
    print()
    print("  CLOSURE")
    print("     unclassified   : %d" % len(proof.unclassified))
    print("     ASK set        : %s" % (proof.ask_set or "empty"))
    print("     may_render     : %s" % proof.may_render)

    rendered = [n for n, r in proof.resolutions.items() if r.renders]
    omitted = [n for n, r in proof.resolutions.items()
               if r.action == Action.OMIT]
    print("     would emit     : %d lines, %d omitted" % (len(rendered),
                                                          len(omitted)))

    disclosures = [(n, r) for n, r in sorted(proof.resolutions.items())
                   if r.disclose]
    if disclosures:
        print()
        print("  DISCLOSURES for the PR body:")
        for name, resolution in disclosures:
            print("     %-30s %s" % (name, resolution.disclosure[:64]))
    if simulated:
        print("     %-30s %d value(s) supplied by SIMULATED HITL, not by a human"
              % ("[simulation notice]", len(simulated)))

    # ---- Phase 5: write the unit, but only after the closure proof --------
    written = []
    if render_dir:
        from da.section_a_iac.render.hcl_writer import render_unit
        from da.section_a_iac.render.pr_body import render_pr_body

        print()
        if not proof.may_render:
            print("  RENDER SKIPPED - the closure proof does not permit it "
                  "(ASK set is not empty).")
        else:
            result_hcl = render_unit(proof, contract, template, platform)
            body = render_pr_body(proof, contract, result_hcl, facts,
                                  ticket_key=key)
            if not os.path.isdir(render_dir):
                os.makedirs(render_dir)
            unit_path = os.path.join(render_dir, "%s.hcl" % key)
            body_path = os.path.join(render_dir, "%s.md" % key)
            io.open(unit_path, "w", encoding="utf-8",
                    newline="\n").write(result_hcl.unit_hcl)
            io.open(body_path, "w", encoding="utf-8",
                    newline="\n").write(body)
            written = [unit_path, body_path]
            print("  WROTE")
            repo_root = os.path.abspath(os.path.join(HERE, ".."))
            for item in written:
                shown = os.path.relpath(item, repo_root)
                print("     %s" % (item if shown.startswith("..") else shown))
            if simulated:
                print("     NOTE: %d value(s) in this unit are SIMULATED HITL, "
                      "not human input." % len(simulated))

    return {"key": key, "module": module_path, "out_of_context": False,
            "written": written,
            "closed": proof.may_render, "rounds": rounds,
            "simulated": simulated, "jira_facts": len(result.accepted)}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(only=None, render_dir=None):
    from da.section_a_iac.contracts.module_contract import discover_modules
    from da.section_a_iac.contracts.platform_context import build_platform_context

    if not os.path.isdir(MODULES_REPO):
        print("sample repos not found at %s" % HON)
        return 1

    platform = build_platform_context(APP_REPO)
    modules = [m for m in discover_modules(MODULES_REPO)
               if not m.startswith("Shared/")]

    if os.getenv("DA_LIVE_LLM"):
        from dotenv import load_dotenv
        from da.section_a_iac.slots.filler import groq_completer
        load_dotenv(os.path.join(HERE, "..", ".env"))
        completer = groq_completer()
        print("slot filler: LIVE Groq")
    else:
        completer = _stub_completer()
        print("slot filler: offline stub (set DA_LIVE_LLM=1 for the real model)")

    paths = sorted(glob.glob(os.path.join(SAMPLES, "*.json")))
    if only:
        paths = [p for p in paths if only.lower() in os.path.basename(p).lower()]

    summaries = [run_one(p, platform, modules, completer,
                         render_dir=render_dir) for p in paths]

    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print("  %-14s %-34s %-7s %s" % ("ticket", "module", "closed", "note"))
    for item in summaries:
        if item["out_of_context"]:
            print("  %-14s %-34s %-7s %s"
                  % (item["key"], "(none)", "-", "out of context, halted"))
            continue
        note = "%d from ticket" % item["jira_facts"]
        if item["simulated"]:
            note += ", %d simulated HITL" % len(item["simulated"])
        print("  %-14s %-34s %-7s %s"
              % (item["key"], item["module"],
                 "yes" if item["closed"] else "NO", note))
    return 0


def _stub_completer():
    """Deterministic offline stand-in: quotes nothing, so nothing is accepted.

    Useful for exercising the loop and the HITL simulation with no network. Run
    with DA_LIVE_LLM=1 to see real extraction.
    """
    def complete(system_prompt, user_prompt):
        return json.dumps({"values": {}, "evidence": {}})
    return complete


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticket", nargs="?", help="e.g. DEVOPS-777")
    parser.add_argument("--render", metavar="DIR", nargs="?",
                        const=os.path.join(HERE, "..", ".agent", "artifacts"),
                        help="write <key>.hcl and <key>.md per closed ticket "
                             "(default: .agent/artifacts)")
    args = parser.parse_args()
    sys.exit(main(args.ticket, render_dir=args.render))
