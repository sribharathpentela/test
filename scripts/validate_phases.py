"""Run and validate Phases 1-3 against the real sample repos.

    python scripts/validate_phases.py           # full report
    python scripts/validate_phases.py --quiet    # verdict only

Offline by default: no LLM, no GitHub, no Azure. Reads the two sample repos
from `Desktop/HON/` (never `Desktop/DA/` -- the copies have diverged).

Set DA_LIVE_LLM=1 to additionally call Groq with the worked-example ticket.

Exit code 0 if every check passes, 1 otherwise.
"""

import argparse
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

HON = os.path.join(os.path.expanduser("~"),
                   "OneDrive - KnackForge Soft Solutions Pvt Ltd",
                   "Desktop", "HON")
MODULES_REPO = os.path.join(HON, "pss_fabric_azure_modules_iac_sample")
APP_REPO = os.path.join(HON, "pss_fabric_app_iac_sample")

# Helper modules -- never provisioned directly, and Keyvault-secrets has no
# template (Q4). None of the three is one of "the 27".
HELPERS = {"Shared/naming", "Shared/tagging", "Security/Keyvault-secrets"}

RESULTS = []


def check(label, got, expected, detail=""):
    """Record one expectation. `expected` of None means "report, do not judge"."""
    if expected is None:
        RESULTS.append((None, label, got, detail))
        return True
    ok = got == expected
    RESULTS.append((ok, label, got, detail or ("expected %r" % (expected,))))
    return ok


def heading(text):
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def main(quiet=False):
    # ---- 0. environment ---------------------------------------------------
    heading("0. Environment")
    for label, path in (("modules repo", MODULES_REPO), ("app_iac repo", APP_REPO)):
        present = os.path.isdir(path)
        print("  %-14s %s  %s" % (label, "OK " if present else "MISSING", path))
        if not present:
            print("\n  Cannot continue. See .agent/journal.md for expected paths.")
            return 1
    try:
        import hcl2  # noqa: F401
        import pydantic  # noqa: F401
    except ImportError as exc:
        print("  missing dependency: %s  ->  pip install -r requirements.txt" % exc)
        return 1
    print("  dependencies   OK  (python-hcl2, pydantic)")

    from da.section_a_iac.contracts.module_contract import build_contract, discover_modules
    from da.section_a_iac.contracts.platform_context import build_platform_context
    from da.section_a_iac.contracts.reconcile import (pair_modules_with_templates, reconcile,
                                        slug_for_module)
    from da.section_a_iac.contracts.template_map import build_template_map, discover_templates
    from da.section_a_iac.contracts.models import EntryState, ReconcileStatus, RequiredTier
    from da.section_a_iac.resolve.engine import resolve_module
    from da.section_a_iac.resolve.facts import FactsStore
    from da.section_a_iac.resolve.models import Action, Branch, FactSource, Verdict

    # ---- 1. Phase 1: the four artifacts -----------------------------------
    heading("1. Phase 1 - contract builders")

    modules = discover_modules(MODULES_REPO)
    templates = discover_templates(MODULES_REPO)
    platform = build_platform_context(APP_REPO)

    contracts, failures = {}, {}
    for module_path in modules:
        try:
            contracts[module_path] = build_contract(
                os.path.join(MODULES_REPO, *module_path.split("/")), module_path)
        except Exception as exc:                                  # noqa: BLE001
            failures[module_path] = "%s: %s" % (type(exc).__name__, exc)

    print("  module directories found     : %d" % len(modules))
    print("  contracts built              : %d" % len(contracts))
    print("  build failures               : %d" % len(failures))
    for path, message in failures.items():
        print("      %s -> %s" % (path, message[:90]))
    print("  templates found              : %d" % len(templates))
    print("  platform context             : %s" % platform.message)

    check("every module builds a contract", len(failures), 0)
    check("templates discovered", len(templates), 27)
    check("auto-injected keys", len(platform.auto_injected), 8)
    check("pointer catalogue locals", len(platform.pointer_locals), 7)
    check("alias pairs", platform.alias_pairs, [["location", "region"]])
    check("module_ref (read, never chosen)", platform.module_ref, "v1.0.6")

    # Key counts the docs pin down.
    def t1_count(path):
        return len([n for n, v in contracts[path].variables.items()
                    if RequiredTier.T1 in v.tiers])

    print()
    print("  T1 counts (declared with no default):")
    for path, expected in (("Containers/AKS", 22),
                           ("Storage/storage_account", 13),
                           ("Databases/Managed-Redis", 9)):
        got = t1_count(path)
        print("      %-30s %3d   (expected %d)" % (path, got, expected))
        check("T1 count %s" % path, got, expected)

    # ---- 2. Phase 1: reconciliation ---------------------------------------
    heading("2. Phase 1 - reconciliation (contract x template)")

    paired = pair_modules_with_templates(modules, templates)
    statuses, case_mismatches = {}, {}
    for module_path in modules:
        if module_path in HELPERS or module_path not in contracts:
            continue
        template_path = paired[module_path]
        template = (build_template_map(template_path, slug_for_module(module_path))
                    if template_path else None)
        result = reconcile(contracts[module_path], template)
        statuses[module_path] = result.status
        for finding in result.findings:
            if finding.code == "source_path_case_mismatch":
                case_mismatches[module_path] = finding.key

    halted = sorted(p for p, s in statuses.items() if s == ReconcileStatus.HALT)
    warned = sorted(p for p, s in statuses.items() if s == ReconcileStatus.WARN)
    print("  modules reconciled           : %d" % len(statuses))
    print("  OK / WARN / HALT             : %d / %d / %d"
          % (len(statuses) - len(warned) - len(halted), len(warned), len(halted)))
    print("  HALT                         : %s" % (halted or "none"))
    check("HALT set", halted,
          ["Backup/Recovery-Services-Vault", "Databases/Redis"])

    print()
    # D2 was withdrawn on 2026-09-21: the three mismatches were corruption in
    # our own mirror, not defects in Honeywell's repo. They were corrected at
    # source and the mirror re-tagged, so a clean audit is now the expected
    # result and a non-zero count means the mirror has drifted again.
    print("  casing mismatches (D2 withdrawn -- expected clean):")
    for module_path in sorted(case_mismatches):
        print("      %-32s template says %r"
              % (module_path, case_mismatches[module_path]))
    check("casing mismatches detected", len(case_mismatches), 0)

    # ---- 3. Phase 2: the closure proof ------------------------------------
    heading("3. Phase 2 - closure proof (every variable -> exactly one branch)")

    total_vars = 0
    unclassified = {}
    branch_totals = {}
    if not quiet:
        print("  %-42s %5s %5s %6s" % ("module", "vars", "ask", "unclass"))
        print("  " + "-" * 62)
    for module_path in sorted(modules):
        if module_path in HELPERS or module_path not in contracts:
            continue
        contract = contracts[module_path]
        template_path = paired[module_path]
        template = (build_template_map(template_path, slug_for_module(module_path))
                    if template_path else None)
        proof = resolve_module(contract, template, platform)

        total_vars += len(contract.variables)
        if proof.unclassified:
            unclassified[module_path] = proof.unclassified
        for branch, names in proof.by_branch().items():
            branch_totals[branch] = branch_totals.get(branch, 0) + len(names)
        if not quiet:
            print("  %-42s %5d %5d %6d" % (module_path, len(contract.variables),
                                           len(proof.ask_set),
                                           len(proof.unclassified)))
    print("  " + "-" * 62)
    print("  variables resolved           : %d" % total_vars)
    print("  UNCLASSIFIED                 : %d   <- must be 0" % len(unclassified))
    print("  by branch                    : %s"
          % {("B%d" % k): v for k, v in sorted(branch_totals.items())})
    check("no unclassified variables", len(unclassified), 0)
    check("full corpus resolved", total_vars > 700, True,
          "got %d variables" % total_vars)

    # ---- 4. Phase 2: the worked example -----------------------------------
    heading("4. Phase 2 - worked example (01-algorithm.md section 10)")

    contract = contracts["Databases/Managed-Redis"]
    template = build_template_map(
        os.path.join(MODULES_REPO, "docs", "examples", "managed-redis",
                     "terragrunt.template.hcl"), "managed-redis")

    print('  Ticket: "Provision Managed Redis in QA. Resource prefix:')
    print('          subscription-redis. SKU: Balanced_B3, High Availability: true."')
    print()

    before = resolve_module(contract, template, platform)
    print("  round 0 (no facts)  ASK = %s" % before.ask_set)

    facts = FactsStore(contract)
    facts.append("resource_prefix", "subscription-redis", FactSource.JIRA,
                 evidence="Resource prefix: subscription-redis")
    facts.append("managed_redis_instances.primary.sku_name", "Balanced_B3",
                 FactSource.JIRA, evidence="SKU: Balanced_B3")
    facts.append("high_availability_enabled", True, FactSource.JIRA,
                 evidence="High Availability: true")
    after = resolve_module(contract, template, platform, facts)
    print("  round 1 (ticket)    ASK = %s   may_render = %s"
          % (after.ask_set or "[]", after.may_render))

    check("round 0 leaves questions", len(before.ask_set) > 0, True)
    check("ticket facts close the ASK set", after.ask_set, [])
    check("closure proven -> may render", after.may_render, True)

    print()
    print("  Disclosures for the PR body (template differs from module default):")
    # Naming values read from the ticket are disclosed too, for a different
    # reason; this check is about template divergence.
    disclosed = sorted(n for n, r in after.resolutions.items()
                       if r.disclose and r.source is FactSource.TEMPLATE)
    for name in disclosed:
        print("      %-36s %s" % (name, after.resolutions[name].disclosure[:76]))
    check("disclosure set", disclosed,
          ["access_keys_authentication_enabled", "eviction_policy",
           "persistence_rdb_backup_frequency"])

    payload = after.resolutions["managed_redis_instances"]
    print()
    print("  payload map assembled from dotted facts:")
    print("      managed_redis_instances = %r" % (payload.value,))
    check("payload map assembly", payload.value,
          {"primary": {"sku_name": "Balanced_B3"}})

    # ---- 5. Phase 2: the guardrails ---------------------------------------
    heading("5. Phase 2 - guardrails")

    # Branch 1 beats Branch 2: the user always wins (ADR 0002).
    override = FactsStore(contract)
    override.append("env", "nonprod", FactSource.JIRA, evidence="env: nonprod")
    env_resolution = resolve_module(contract, template, platform,
                                    override).resolutions["env"]
    print("  ticket sets env=nonprod      -> branch %d, %s, disclosed=%s"
          % (int(env_resolution.branch), env_resolution.action.value,
             env_resolution.disclose))
    check("Branch 1 beats auto-injected", env_resolution.branch, Branch.USER_FACT)

    # Alias propagation: location <-> region.
    alias = FactsStore(contract)
    alias.append("location", "westus", FactSource.JIRA, evidence="deploy to westus")
    alias_proof = resolve_module(contract, template, platform, alias)
    print("  ticket sets location=westus  -> region resolves to %r"
          % alias_proof.resolutions["region"].value)
    check("alias propagation", alias_proof.resolutions["region"].value, "westus")

    # Validation is a gate inside facts.append() (ADR 0010).
    aks = contracts["Containers/AKS"]
    gate = FactsStore(aks)
    rejected = gate.append("automatic_upgrade_channel", "banana", FactSource.JIRA)
    print("  bad enum value 'banana'      -> %s (rung %s), store size %d"
          % (rejected.verdict.value,
             rejected.rung.value if rejected.rung else "-", len(gate)))
    check("bad enum rejected on the write path", rejected.verdict, Verdict.REJECT)
    check("rejected value never stored", len(gate), 0)

    # Platform values are validated too -- not a trust exemption.
    plat = FactsStore(contract)
    plat_outcome = plat.append("high_availability_enabled", "not-a-bool",
                               FactSource.PLATFORM)
    print("  platform value, wrong type   -> %s" % plat_outcome.verdict.value)
    check("platform values validated too", plat_outcome.verdict, Verdict.REJECT)

    # ---- 6. Phase 3: slot filling -----------------------------------------
    heading("6. Phase 3 - slot filling (schema inversion + evidence gate)")

    import json as _json
    from da.section_a_iac.slots.filler import SlotFiller, apply_to_facts
    from da.section_a_iac.slots.schema import build_slot_schema

    ticket = ("Provision Managed Redis in QA. Resource prefix: "
              "subscription-redis. SKU: Balanced_B3, High Availability: true.")
    round0 = resolve_module(contract, template, platform)
    schema = build_slot_schema(contract, round0, template, platform)
    document = schema.json_schema()

    print("  slots generated              : %d (%d blocking)"
          % (len(schema.slots), len(schema.blocking)))
    print("  blocking slots               : %s" % schema.blocking)
    print("  additionalProperties         : %s" % document["additionalProperties"])
    print("  every slot nullable          : %s"
          % all("null" in e.get("type", [])
                for e in document["properties"].values()))
    print("  sku_name maps to             : %s" % schema.path_for("sku_name"))
    check("schema forbids invented keys", document["additionalProperties"], False)
    check("payload leaf maps to its fact path", schema.path_for("sku_name"),
          "managed_redis_instances.primary.sku_name")

    def _stub(values, evidence):
        def complete(system_prompt, user_prompt):
            return _json.dumps({"values": values, "evidence": evidence})
        return complete

    honest = SlotFiller(_stub(
        {"resource_prefix": "subscription-redis", "sku_name": "Balanced_B3",
         "high_availability_enabled": True},
        {"resource_prefix": "Resource prefix: subscription-redis",
         "sku_name": "SKU: Balanced_B3",
         "high_availability_enabled": "High Availability: true"})).fill(
        ticket, schema)
    print()
    print("  honest extraction            : %d filled, %d dropped"
          % (len(honest.accepted), len(honest.dropped)))
    check("honest extraction accepted", len(honest.accepted), 3)

    hostile = SlotFiller(_stub(
        {"eviction_policy": "AllKeysLRU", "redis_sku": "Balanced_B99"},
        {"eviction_policy": "policy should be AllKeysLRU",
         "redis_sku": "SKU: Balanced_B99"})).fill(ticket, schema)
    reasons = sorted(s.drop_reason.value for s in hostile.dropped)
    print("  hallucinated extraction      : %d filled, dropped for %s"
          % (len(hostile.accepted), reasons))
    check("invented value dropped", len(hostile.accepted), 0)
    check("renamed slot rejected", hostile.invented_keys, ["redis_sku"])

    filled = FactsStore(contract)
    apply_to_facts(honest, filled, FactSource.JIRA)
    closed = resolve_module(contract, template, platform, filled)
    print()
    print("  fill -> facts -> re-resolve  : ASK = %s, may_render = %s"
          % (closed.ask_set or "[]", closed.may_render))
    check("slot filling closes the ASK set", closed.ask_set, [])

    live = os.getenv("DA_LIVE_LLM")
    if live:
        from dotenv import load_dotenv
        from da.section_a_iac.slots.filler import groq_completer
        load_dotenv(os.path.join(HERE, "..", ".env"))
        print()
        print("  DA_LIVE_LLM set - calling Groq with the same ticket...")
        real = SlotFiller(groq_completer()).fill(ticket, schema)
        print("      %s, invented keys: %s"
              % (real.message, real.invented_keys or "none"))
        live_facts = FactsStore(contract)
        apply_to_facts(real, live_facts, FactSource.JIRA)
        live_proof = resolve_module(contract, template, platform, live_facts)
        print("      ASK = %s, may_render = %s"
              % (live_proof.ask_set or "[]", live_proof.may_render))
        check("live extraction invents nothing", real.invented_keys, [])
        check("live extraction closes the ASK set", live_proof.ask_set, [])
    else:
        print()
        print("  (set DA_LIVE_LLM=1 to also call Groq with this ticket)")

    # ---- verdict ----------------------------------------------------------
    heading("Verdict")
    passed = [r for r in RESULTS if r[0] is True]
    failed = [r for r in RESULTS if r[0] is False]
    for ok, label, got, detail in failed:
        print("  FAIL  %-42s got %r  (%s)" % (label, got, detail))
    print("  checks passed: %d / %d" % (len(passed), len(passed) + len(failed)))
    print()
    if failed:
        print("  RESULT: FAILED")
        return 1
    print("  RESULT: PASSED - Phases 1-3 behave as the docs specify.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true",
                        help="skip the per-module table")
    args = parser.parse_args()
    try:
        sys.exit(main(quiet=args.quiet))
    except Exception:                                              # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
