"""The Deployment Agent, end to end: a Jira ticket in, the change out.

    python run_da.py DEVOPS-888                    # you answer the questions
    python run_da.py DEVOPS-888 --live             # real model for intake too
    python run_da.py DEVOPS-888 --out artifacts/   # write the unit and PR body
    python run_da.py DEVOPS-888 --simulate         # fabricate answers (CI only)

**Answering is the default.** `--simulate` fills every question with a made-up
value, which is useful in CI and nowhere else: the rendered unit then contains
strings like `admin_username = "simulated-admin_username"` in output that
otherwise looks ready to apply. It is opt-in for that reason.

Every stage prints as it happens, including the ones that halt. Module and
platform content is fetched from Git at the pinned ref (ADR 0021); nothing is
read from a working copy.

Unless `--simulate` is passed, the run stops and asks whenever DA needs a human:

* **Gate B** -- a missing baseline field. The library halts here by design (DA is
  supervisor-driven and never reads stdin); the *demo* asks and resumes, which is
  what a supervisor would do.
* **Step 4a HITL** -- every blocking slot, with its type, allowed values and the
  reason it is being asked.

Without `-i`, answers are invented, marked `simulated` and disclosed everywhere.
A simulated answer is not human input.
"""

import argparse
import io
import json
import os
import sys

# This file lives at the repo root, so the root is simply where it sits.
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from src.da.pipeline import DAPipeline, PipelineStatus, Stage    # noqa: E402

SAMPLES = os.path.join(ROOT, "src", "da", "samples")
RULE = "=" * 78
ENVIRONMENTS = ["dev", "qa", "staging", "prod"]


# --------------------------------------------------------------------------
# Asking a human
# --------------------------------------------------------------------------

DEV_PHASE = "Raised PR with these details - In development Phase"


def ask(prompt, allowed=None, default=None, required=True):
    """Prompt until we get something usable. Ctrl-C leaves the demo cleanly."""
    while True:
        try:
            raw = input("      %s%s\n      > "
                        % (prompt, " [%s]" % default if default else "")).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n  cancelled.")
            raise SystemExit(130)
        if not raw and default is not None:
            return default
        if not raw:
            if not required:
                return None
            print("      (a value is needed)")
            continue
        if allowed and raw not in allowed:
            print("      (must be one of: %s)" % ", ".join(allowed))
            continue
        return raw


def answer_question(question, interactive):
    """One HITL answer, plus how it was obtained."""
    if not interactive:
        if question.is_secret:
            return ({"env_var_name": "SIMULATED_%s" % question.variable.upper()},
                    "SIMULATED, not human input")
        return "simulated-%s" % question.variable, "SIMULATED, not human input"

    print()
    print("      %s" % question.variable)
    if question.why:
        print("        why      : %s" % question.why)
    if question.json_type:
        print("        type     : %s" % ", ".join(question.json_type))
    if question.allowed_values:
        print("        allowed  : %s" % ", ".join(str(v) for v in question.allowed_values))
    if question.example:
        print("        example  : %s" % question.example)

    if question.is_secret:
        # Rule 10: a secret is an {env_var_name} object, never a literal. It must
        # be structurally impossible to type a password here.
        print("        SECRET   : give the ENV VAR NAME, never the value itself.")
        name = ask("environment variable holding it (e.g. AKS_SSH_KEY)")
        return {"env_var_name": name}, "typed by a human"

    allowed = [str(v) for v in question.allowed_values] or None
    return ask(question.prompt, allowed=allowed), "typed by a human"


def answer_placement_question(question, interactive):
    """Where the unit goes. Not a variable, so not a `HitlQuestion`.

    The folder *name* is read out of the app repo; what is asked is the group,
    which is an ownership judgement the repo does not record (ADR 0026).
    """
    if not interactive:
        free = [g for g in question.allowed_values
                if "%s/" % g not in question.why]
        pick = (free or question.allowed_values or ["shared"])[0]
        return pick, "SIMULATED, first group with room"

    print()
    print("      %s" % question.field)
    if question.why:
        print("        evidence : %s" % question.why)
    if question.allowed_values:
        shown = [question.labels.get(v, v) for v in question.allowed_values]
        print("        allowed  : %s" % ", ".join(shown))
        if question.open_ended:
            print("                   (or anything else)")

    allowed = None if question.open_ended else (question.allowed_values or None)
    return ask(question.prompt, allowed=allowed), "typed by a human"


def answer_cfg_question(question, interactive):
    """One Section C answer.

    A `CfgQuestion` is not a `HitlQuestion`: it has no variable, no JSON type
    and no secret flag, because it answers "which change, which field, what
    value" rather than "what does this `variables.tf` entry take".
    """
    if not interactive:
        if question.allowed_values:
            return question.allowed_values[0], "SIMULATED, first allowed value"
        return "simulated-%s" % question.field, "SIMULATED, not human input"

    print()
    print("      %s   (attempt %d of %d)"
          % (question.field, question.attempt, question.max_attempts))
    if question.why:
        print("        why      : %s" % question.why)
    for line in question.context_lines:
        print("        %s" % line)
    if question.allowed_values:
        print("        allowed  : %s" % ", ".join(question.allowed_values))
        if question.open_ended:
            print("                   (or anything else -- this list is a guide)")

    # Deliberately no `allowed=` here. Section C interprets its own answers --
    # `prompt_for_change_disambiguation` matches on substrings and then falls
    # back to a bounded model pick that may only return one of the offered
    # options. Validating against the list first meant "I mean CPU request"
    # was rejected by the runner before the matcher that would have resolved
    # it ever ran, and the engine's own retry budget never advanced.
    #
    # Answer by name rather than position: the candidate order is ranked with
    # the model's help and is not stable between runs, so "2" can mean a
    # different entry each time.
    return ask(question.prompt), "typed by a human"


class PromptingParser(object):
    """Wraps the intake parser and fills Gate B's gaps by asking.

    The pipeline itself halts on a missing baseline field, on purpose -- it is
    driven by a supervisor and must not block on stdin. Asking belongs to the
    caller, so it lives here, in the demo, not in `pipeline.py`.
    """

    REQUIRED = [
        ("environment", "Environment", ENVIRONMENTS),
        ("target_service_repo", "Target service repo", None),
        ("resource_type", "Resource module type (e.g. AKS, Managed-Redis)", None),
    ]

    def __init__(self, inner, interactive):
        self.inner = inner
        self.interactive = interactive

    def parse_raw_jira_ticket(self, raw):
        intake = self.inner.parse_raw_jira_ticket(raw)
        if not self.interactive or not intake.is_da_task:
            return intake

        # Gate B validates an *IaC* request. Asking its fields for a
        # config-change ticket produced "Resource module type (e.g. AKS,
        # Managed-Redis)" on a request to raise a CPU limit -- and worse,
        # `_cfg_request_from_intake` maps `resource_type` to
        # `configuration_change`, so whatever was typed became the name of the
        # change. `gvjhb` went in and INVALID came out, for a reason that
        # looked unrelated. The pipeline already skips Gate B for
        # `config_changes`; this is the caller catching up.
        if (intake.classification or "") != "cloud_service_iac":
            return intake

        missing = [(f, label, allowed) for f, label, allowed in self.REQUIRED
                   if not (getattr(intake, f, None) or "").strip()]
        if not missing:
            return intake

        print()
        print("  GATE B - %d baseline field(s) the ticket does not state:"
              % len(missing))
        for field, label, allowed in missing:
            value = ask(label, allowed=allowed)
            setattr(intake, field, value)
            print("        %s = %r  [typed by a human]" % (field, value))
        return intake


# --------------------------------------------------------------------------
# Offline stand-ins
# --------------------------------------------------------------------------

def stub_completer(_system, _user):
    """Quotes nothing, so the loop and the HITL path run with no LLM."""
    return json.dumps({"values": {}, "evidence": {}})


class OfflineParser(object):
    """Reads the ticket's own words instead of calling the intake model.

    Deliberately literal: it exercises the *pipeline*, not the extraction.
    Use --live to exercise extraction.
    """

    def parse_raw_jira_ticket(self, raw):
        import re
        from src.da.schemas.base_intake import GateAIntakeOutput, TicketMetadata

        ticket = raw.get("jira", raw)
        text = "%s %s" % (ticket.get("summary", ""), ticket.get("description", ""))
        repo = re.search(r"pss_[a-z_]+", text)
        env = re.search(r"\b(dev|qa|staging|prod)\b", text, re.I)
        prefix = re.search(r"[Rr]esource prefix:?\s*([A-Za-z0-9-]+)", text)

        resource = None
        for candidate in ["AKS", "Managed-Redis", "PostgreSQL-Flexible-Server",
                          "storage_account", "load-balancer", "front-door"]:
            if candidate.lower().replace("-", " ") in text.lower().replace("-", " "):
                resource = candidate
                break

        # Section C, only once no IaC module matched -- a ticket naming a
        # module is infrastructure even if it also says "cpu".
        if resource is None:
            cfg = self._config_change(text, ticket)
            if cfg is not None:
                return cfg

        return GateAIntakeOutput(
            is_da_task=bool(resource),
            rejection_reason=None if resource else "no known resource type named",
            classification="cloud_service_iac" if resource else None,
            ticket_metadata=TicketMetadata(ticket_id=ticket.get("key", "UNKNOWN")),
            environment=env.group(1).lower() if env else None,
            target_service_repo=repo.group(0) if repo else None,
            resource_type=resource,
            resource_prefix=prefix.group(1) if prefix else None)


    # Phrases that name a config-change type in `config_changes.json`. Only
    # enough to route the demo -- Section C then resolves the exact entry
    # against all 28, fuzzily, and asks when it cannot.
    CONFIG_HINTS = (
        "cpu", "memory", "replica", "port", "cache", "timeout", "window size",
        "page size", "utilization", "rolling update", "storage capacity",
        "minimum availability", "interval", "buffer",
    )

    def _config_change(self, text, ticket):
        """A config-change intake, or None if the ticket is not one."""
        import re
        from src.da.schemas.base_intake import GateAIntakeOutput, TicketMetadata

        lowered = text.lower()
        if not any(hint in lowered for hint in self.CONFIG_HINTS):
            return None

        service = re.search(r"\bcf-[a-z0-9]+(?:-[a-z0-9]+)*", lowered)
        env = re.search(r"\b(dev|qa|staging|prod)\b", lowered)
        # A Kubernetes quantity: 300m, 12, 512Mi, 2Gi.
        value = re.search(r"\bto\s+([0-9]+(?:\.[0-9]+)?[a-zA-Z]{0,2})\b", lowered)

        extracted = {}
        if value:
            extracted["target_value"] = value.group(1)

        # `configuration_change` is left to Section C unless the ticket states
        # a catalogue name outright. Guessing it here would put the demo's
        # wording in front of the catalogue's own matching.
        stated = re.search(r"([A-Za-z ]+?(?:change))\b", text)

        return GateAIntakeOutput(
            is_da_task=True,
            classification="config_changes",
            ticket_metadata=TicketMetadata(ticket_id=ticket.get("key", "UNKNOWN")),
            environment=env.group(1) if env else None,
            target_service_repo=service.group(0) if service else None,
            resource_type=stated.group(1).strip() if stated else None,
            extracted_inputs=extracted)


# --------------------------------------------------------------------------

def find_ticket(key):
    for name in (key, key + ".json"):
        direct = name if os.path.isfile(name) else os.path.join(SAMPLES, name)
        if os.path.isfile(direct):
            return direct
    available = sorted(f[:-5] for f in os.listdir(SAMPLES) if f.endswith(".json"))
    raise SystemExit("no such ticket: %s\navailable: %s" % (key, ", ".join(available)))


def show_trace(trace):
    """Section C's engine trace, which the pipeline captured rather than let it
    print. Shown here, after the stages, so it cannot interleave with them."""
    print()
    print(RULE)
    print("ENGINE TRACE  (captured, last round only)")
    print(RULE)
    for line in trace.splitlines():
        print("  %s" % line)


def show_stages(result, already):
    for record in result.stages[already:]:
        print("  [%s] %-10s %s" % ("  ok  " if record.ok else " HALT ",
                                   record.stage.value, record.message[:88]))
    return len(result.stages)


def main(argv=None):
    # Section C's banners are non-ASCII and the Windows console is cp1252, so
    # without this the first thing it prints takes the run down. This is an
    # entry point, so setting it up here rather than in a library is correct.
    from src.da.section_c_cfg.engine.core.config import use_utf8_console
    use_utf8_console()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticket", nargs="?", default="DEVOPS-888")
    parser.add_argument("--live", action="store_true",
                        help="use the real model for intake and slot filling")
    parser.add_argument("--simulate", action="store_true",
                        help="fabricate every answer instead of asking. For CI: "
                             "the output looks complete but the values are made "
                             "up, so never demo with this.")
    parser.add_argument("--trace", action="store_true",
                        help="also print Section C's engine trace, which is "
                             "captured rather than emitted so it cannot "
                             "interleave with the stage report")
    parser.add_argument("--out", metavar="DIR",
                        help="write <key>.hcl and <key>.md here on completion")
    args = parser.parse_args(argv)
    interactive = not args.simulate

    path = find_ticket(args.ticket)
    raw = json.load(io.open(path, encoding="utf-8-sig"))
    ticket = raw.get("jira", raw)

    print()
    print(RULE)
    print("%s  -  %s" % (ticket.get("key", "?"), ticket.get("summary", "")))
    print(RULE)
    print("  source     : %s" % os.path.relpath(path, ROOT))
    print("  slot filler: %s" % ("LIVE model" if args.live else "offline stub"))
    print("  answers    : %s" % ("you" if interactive
                                    else "SIMULATED - not human input"))
    print()

    inner = None
    if not args.live:
        inner = OfflineParser()
    elif interactive:
        from src.da.intake.parser import JiraIntakeParser
        inner = JiraIntakeParser()

    pipeline = DAPipeline(
        completer=None if args.live else stub_completer,
        intake_parser=PromptingParser(inner, interactive) if inner else None)

    result = pipeline.start(raw)
    seen = show_stages(result, 0)

    simulated = []
    while result.status is PipelineStatus.NEEDS_HITL:
        # Section C asks one question per round rather than a set: it discovers
        # what it needs only after resolving the last thing, so there is no
        # schema to compute the whole set from up front.
        if result.placement_question is not None:
            question = result.placement_question
            print()
            print("  PLACEMENT - %s" % result.message)
            value, basis = answer_placement_question(question, interactive)
            if not interactive:
                print("     %-26s = %r  [%s]" % (question.field, value, basis))
            result = pipeline.submit({question.field: value})
            seen = show_stages(result, seen)
            continue

        if result.cfg_question is not None:
            question = result.cfg_question
            print()
            print("  HITL round %d - %s" % (result.cfg_round, question.field))
            value, basis = answer_cfg_question(question, interactive)
            if not interactive:
                print("     %-26s = %r  [%s]" % (question.field, value, basis))
                simulated.append(question.field)
                if question.why:
                    print("        why: %s" % question.why[:70])
            result = pipeline.submit({question.key: value})
            seen = show_stages(result, seen)
            continue

        print()
        print("  HITL - %d question(s) DA cannot answer from the ticket:"
              % len(result.hitl_request.questions))
        answers = {}
        for question in result.hitl_request.questions:
            value, basis = answer_question(question, interactive)
            answers[question.path] = value
            if not interactive:
                print("     %-26s = %r  [%s]" % (question.path, value, basis))
                simulated.append(question.path)
                if question.why:
                    print("        why: %s" % question.why[:70])
        result = pipeline.submit(answers)
        seen = show_stages(result, seen)

    if args.trace and result.cfg_trace:
        show_trace(result.cfg_trace)

    print()
    if result.status is PipelineStatus.HALTED:
        print("  HALTED at %s" % result.halted_at.value)
        print("  %s" % result.message)
        if result.halted_at is Stage.GATE_B and not interactive:
            print()
            print("  Re-run with -i to supply the missing field yourself.")
        print()
        print("  Nothing was rendered. DA stops rather than guessing.")
        return 1

    print(RULE)
    print("COMPLETE")
    print(RULE)

    if result.classification == "config_changes":
        outcome = result.cfg_outcome or {}
        print("  section   : C - configuration change")
        print("  validation: %s" % (outcome.get("validation") or {}).get("status"))
        print("  mutation  : %s" % (outcome.get("mutation") or {}).get("status"))
        plan = result.pr_plan
        if plan:
            print()
            print("  %s" % DEV_PHASE)
            print("    branch  : %s  (base %s)" % (plan["branch"], plan["base_branch"]))
            for target in plan["files"]:
                print("    commit  : %s" % target)
            print("    title   : %s" % plan["pr_title"])
            print()
            print("    PR body")
            for line in plan["pr_body"].splitlines():
                print("      %s" % line)
        if simulated:
            print()
            print("  NOTE      : %d answer(s) SIMULATED, not human input: %s"
                  % (len(simulated), ", ".join(simulated)))
        return 0

    print("  section   : A - infrastructure as code")
    print("  module    : %s @ %s" % (result.module_path, result.module_ref))
    print("  source    : %s" % result.git_source_url)
    print("  inputs    : %d rendered, %d omitted" % (result.rendered_count,
                                                     result.omitted_count))
    print("  placement : %r" % result.proposed_unit_path)
    if result.placement and result.placement.evidence:
        for line in result.placement.evidence[:3]:
            print("              evidence: %s" % line)
    if result.placement_is_provisional:
        print("              ^ PROVISIONAL. The real convention is <app>/<Resource>")
        print("                (shared/AKS, la/FunctionApp/android); deriving it needs")
        print("                the app dimension that M5-M7 add.")
    if simulated:
        print("  NOTE      : %d value(s) are SIMULATED, not human input: %s"
              % (len(simulated), ", ".join(simulated)))

    if args.out:
        if not os.path.isdir(args.out):
            os.makedirs(args.out)
        key = ticket.get("key", "ticket")
        for suffix, text in [(".hcl", result.unit_hcl), (".md", result.pr_body)]:
            target = os.path.join(args.out, key + suffix)
            io.open(target, "w", encoding="utf-8", newline="\n").write(text)
            print("  wrote     : %s" % os.path.relpath(target, ROOT))
    else:
        print()
        print(RULE)
        print(result.unit_hcl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
