"""The Deployment Agent, end to end: a Jira ticket in, a Terragrunt unit out.

    intake -> Gate A -> classify -> Gate B -> Step 3a discovery
           -> contracts -> Step 4a resolution -> closure proof -> render

Driven as a **state machine**, like `Step4A` which it wraps. DA is invoked by an
LLM-driven supervisor, so every outcome is a returned object: it never reads
stdin and never decides on a human's behalf.

    result = DAPipeline().start(raw_jira_payload)
    while result.status is PipelineStatus.NEEDS_HITL:
        result = pipeline.submit({question.path: answer, ...})

Three terminal states and nothing else:

| status | meaning |
|---|---|
| `COMPLETE`   | the closure proof passed and the unit is rendered |
| `NEEDS_HITL` | a human must answer before anything more can be decided |
| `HALTED`     | DA cannot proceed and will not guess |

Every source of repository content is a Git fetch at an explicit ref
(ADR 0021); nothing is read from a working copy.
"""

import os
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from .clients.git_client import GitClient
from .section_a_iac.contracts.materialise import MaterialiseError, MaterialisedRepo, materialise
from .section_a_iac.contracts.module_contract import build_contract
from .section_a_iac.contracts.platform_context import build_platform_context
from .section_a_iac.contracts.reconcile import pair_modules_with_templates
from .section_a_iac.contracts.template_map import build_template_map, discover_templates
from .section_a_iac.resolve.facts import FactsStore
from .section_a_iac.resolve.loop import HitlRequest, Step4A, Step4AStatus
from .schemas.base_intake import GateAIntakeOutput
from .section_a_iac.placement import (PlacementProposal, PlacementQuestion,
                                       propose_placement, survey_local)
from .section_c_cfg.flow import CfgResult, CfgStatus, ConfigChangeFlow
from .section_c_cfg.questions import CfgQuestion


class Stage(str, Enum):
    INTAKE = "intake"
    GATE_A = "gate_a"
    CLASSIFY = "classify"
    GATE_B = "gate_b"
    DISCOVERY = "discovery"
    PLATFORM = "platform"
    CONTRACT = "contract"
    RESOLVE = "resolve"
    RENDER = "render"
    CONFIG_CHANGE = "config_change"


class PipelineStatus(str, Enum):
    COMPLETE = "complete"
    NEEDS_HITL = "needs_hitl"
    HALTED = "halted"


class StageRecord(BaseModel):
    """One stage's outcome, in order, for the PR body and for debugging."""

    stage: Stage
    ok: bool
    message: str = ""


class PipelineResult(BaseModel):
    status: PipelineStatus
    ticket_id: str = ""
    stages: List[StageRecord] = Field(default_factory=list)
    halted_at: Optional[Stage] = None

    module_path: Optional[str] = None
    module_ref: Optional[str] = None
    git_source_url: Optional[str] = None

    hitl_request: Optional[HitlRequest] = None

    unit_hcl: Optional[str] = None
    pr_body: Optional[str] = None
    rendered_count: int = 0
    omitted_count: int = 0

    proposed_unit_path: Optional[str] = None
    # False once placement is settled -- the name observed from the repo and
    # the group answered. True only while the fallback guess is in use.
    placement_is_provisional: bool = True
    # Where a unit would go, and the evidence for it. Present on a completed
    # Section A run and on a run waiting for a placement answer.
    placement: Optional[PlacementProposal] = None
    placement_question: Optional[PlacementQuestion] = None

    # -- Section C ------------------------------------------------------
    # One envelope for both sections, so a caller reads `status` and
    # `classification` and never needs to know which one ran. Section C asks
    # one question at a time rather than a set (see `section_c_cfg/flow.py`),
    # so it fills `cfg_question` where Section A fills `hitl_request`.
    classification: str = ""
    cfg_question: Optional[CfgQuestion] = None
    cfg_outcome: Optional[Dict[str, Any]] = None
    cfg_round: int = 0
    # What Section C's engine printed, captured instead of emitted. Shown only
    # when the caller asks for it.
    cfg_trace: str = ""

    message: str = ""

    @property
    def pr_plan(self) -> Optional[Dict[str, Any]]:
        """What would have been raised, never what was.

        Section A has no write path and Section C's was removed, so nothing
        reachable from here can open a pull request.
        """
        results = ((self.cfg_outcome or {}).get("github") or {}).get("results") or []
        return results[0].get("plan") if results else None


class DAPipeline(object):
    """Holds everything that survives a HITL round trip."""

    def __init__(self,
                 git_client: Optional[GitClient] = None,
                 materialiser: Optional[Callable[[str, str], MaterialisedRepo]] = None,
                 completer: Optional[Callable[[str, str], str]] = None,
                 intake_parser: Optional[Any] = None,
                 cfg_runner: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None):
        self.git = git_client or GitClient()
        self.materialiser = materialiser or (
            lambda repo, ref: materialise(repo, ref, settings=self.git.settings))
        self.completer = completer
        self.intake_parser = intake_parser

        self.stages: List[StageRecord] = []
        self.intake: Optional[GateAIntakeOutput] = None
        self.discovery = None
        self.contract = None
        self.template = None
        self.platform = None
        self.loop: Optional[Step4A] = None
        self.app_root = ""
        # Placement answers accumulate the same way HITL answers do; the
        # proposal is recomputed from them rather than mutated.
        self.placement_answers: Dict[str, str] = {}
        self.render_result = None
        self.cfg: Optional[ConfigChangeFlow] = None
        self.cfg_runner = cfg_runner
        self.classification = ""
        self.ticket_text = ""
        self.ticket_id = ""

    # -- stage bookkeeping --------------------------------------------------

    def _ok(self, stage: Stage, message: str = "") -> None:
        self.stages.append(StageRecord(stage=stage, ok=True, message=message))

    def _halt(self, stage: Stage, message: str) -> PipelineResult:
        self.stages.append(StageRecord(stage=stage, ok=False, message=message))
        return PipelineResult(
            status=PipelineStatus.HALTED, ticket_id=self.ticket_id,
            stages=list(self.stages), halted_at=stage, message=message)

    # -- entry points -------------------------------------------------------

    def start(self, raw_ticket: Dict[str, Any]) -> PipelineResult:
        """Run every stage that needs no human, then say what is needed next."""
        self.stages = []

        early = self._intake(raw_ticket)
        if early is not None:
            return early

        # Section C runs its own state machine and shares nothing downstream of
        # here: no module contract, no closure proof, no HCL.
        if self.classification == "config_changes":
            return self._start_config_changes()

        halted = self._discover()
        if halted is not None:
            return halted

        halted = self._seed_facts_from_ticket()
        if halted is not None:
            return halted

        return self._advance()

    def submit(self, answers: Dict[str, Any]) -> PipelineResult:
        """Feed HITL answers back in and continue."""
        if self.cfg is not None:
            return self._after_cfg(self.cfg.submit(answers))

        # A placement answer arrives after the closure proof, so the loop is
        # done and re-entering it would resolve an already-resolved module.
        if self.render_result is not None:
            self.placement_answers.update(
                {k: v for k, v in answers.items()
                 if k in ("unit", "group", "variant")})
            return self._render(self.render_result)

        if self.loop is None:
            return self._halt(Stage.RESOLVE,
                              "submit() called before start(); there is no loop to "
                              "answer. The supervisor must call start() first.")
        result = self.loop.submit(answers)
        return self._after_loop(result)

    # -- Section C ----------------------------------------------------------

    def _cfg_request_from_intake(self) -> Dict[str, Any]:
        """Gate A output as a config-change request.

        A partial mapping on purpose. Section C resolves `configuration_change`
        fuzzily against the 28 catalogue entries and asks for anything else it
        needs, so a field this cannot fill becomes a question rather than a
        guess. That is why no default is invented for any of them here.
        """
        request: Dict[str, Any] = dict(self.intake.extracted_inputs or {})
        for key, value in (("configuration_change", self.intake.resource_type),
                           ("service", self.intake.target_service_repo),
                           ("environment", self.intake.environment)):
            if value and not request.get(key):
                request[key] = value
        return request

    def _start_config_changes(self) -> PipelineResult:
        self.cfg = ConfigChangeFlow(self._cfg_request_from_intake(),
                                    runner=self.cfg_runner)
        given = [k for k in ("configuration_change", "service", "environment")
                 if self.cfg.request.get(k)]
        self._ok(Stage.CONFIG_CHANGE,
                 "request built; %s from the ticket"
                 % (", ".join(given) or "nothing"))
        return self._after_cfg(self.cfg.advance())

    def _after_cfg(self, result: CfgResult) -> PipelineResult:
        if result.status is CfgStatus.HALT:
            # `_halt` is the shared builder and knows nothing about Section C,
            # so the trace has to be attached here -- and a failed run is
            # exactly when someone wants to read it.
            halted = self._halt(Stage.CONFIG_CHANGE, result.message)
            halted.classification = self.classification
            halted.cfg_trace = result.trace
            return halted

        if result.status is CfgStatus.NEEDS_HITL:
            return PipelineResult(
                status=PipelineStatus.NEEDS_HITL,
                ticket_id=self.ticket_id,
                classification=self.classification,
                stages=list(self.stages),
                cfg_question=result.question,
                cfg_round=result.round,
                cfg_trace=result.trace,
                message=result.message)

        self._ok(Stage.CONFIG_CHANGE, result.message)
        return PipelineResult(
            status=PipelineStatus.COMPLETE,
            ticket_id=self.ticket_id,
            classification=self.classification,
            stages=list(self.stages),
            cfg_outcome=result.outcome,
            cfg_round=result.round,
            cfg_trace=result.trace,
            message=result.message)

    # -- stages -------------------------------------------------------------

    def _intake(self, raw_ticket: Dict[str, Any]) -> Optional[PipelineResult]:
        from .classify.router import ClassificationRouter
        from .section_a_iac.gate_b import GateBValidator

        parser = self.intake_parser
        if parser is None:
            from .intake.parser import JiraIntakeParser
            parser = JiraIntakeParser()

        try:
            self.intake = parser.parse_raw_jira_ticket(raw_ticket)
        except Exception as exc:                    # noqa: BLE001 - reported, not raised
            return self._halt(Stage.INTAKE, "Could not parse the ticket: %s" % exc)

        ticket = raw_ticket.get("jira", raw_ticket)
        self.ticket_text = "%s\n\n%s" % (ticket.get("summary", ""),
                                         ticket.get("description", ""))
        self.ticket_id = (self.intake.ticket_metadata.ticket_id
                          if self.intake.ticket_metadata else "UNKNOWN")
        self._ok(Stage.INTAKE, "parsed %s" % self.ticket_id)

        # Gate A -- is this DA's work at all?
        if not self.intake.is_da_task:
            return self._halt(
                Stage.GATE_A,
                "Not a Deployment Agent task: %s Hand back to the supervisor."
                % (self.intake.rejection_reason or ""))
        self._ok(Stage.GATE_A, "DA task")

        try:
            classification = ClassificationRouter.route_request(self.intake)
        except ValueError as exc:
            return self._halt(Stage.CLASSIFY, str(exc))
        self._ok(Stage.CLASSIFY, classification)
        self.classification = classification

        if classification == "config_changes":
            return None                       # Section C, dispatched by start()

        if classification != "cloud_service_iac":
            return self._halt(
                Stage.CLASSIFY,
                "Classified '%s'. Sections A (cloud_service_iac) and C "
                "(config_changes) are implemented; microservice_onboarding is "
                "not built yet." % classification)

        # Gate B -- the baseline fields. Note this HALTs rather than prompting:
        # `hitl2_basic_iac` blocks on input(), which suits a console and not a
        # supervisor. The supervisor amends the ticket and calls start() again.
        gate_b = GateBValidator.validate_iac_request(self.intake)
        if not gate_b.is_valid:
            return self._halt(
                Stage.GATE_B,
                "Gate B failed. Missing: %s" % ", ".join(gate_b.missing_fields))
        self._ok(Stage.GATE_B, "baseline fields present")
        return None

    def _discover(self) -> Optional[PipelineResult]:
        from .section_a_iac.module_discovery import ModuleDiscoveryEngine

        engine = ModuleDiscoveryEngine(git_client=self.git,
                                       materialiser=self.materialiser)
        self.discovery = engine.discover_module_in_central_registry(self.intake)
        if not self.discovery.found:
            return self._halt(Stage.DISCOVERY, self.discovery.message)
        self._ok(Stage.DISCOVERY, "%s at %s" % (self.discovery.module_rel_path,
                                                self.discovery.module_ref))

        # The platform context comes from the app repo, materialised the same way.
        try:
            app = self.materialiser(self.git.settings.app_repo,
                                    self.git.settings.default_branch)
        except MaterialiseError as exc:
            return self._halt(Stage.PLATFORM,
                              "Could not materialise '%s': %s"
                              % (self.git.settings.app_repo, exc))
        self.app_root = app.root
        self.platform = build_platform_context(app.root)
        self._ok(Stage.PLATFORM, self.platform.message)

        module_path = self.discovery.module_rel_path
        try:
            self.contract = build_contract(self.discovery.module_dir, module_path)
        except Exception as exc:                    # noqa: BLE001 - reported, not raised
            return self._halt(Stage.CONTRACT,
                              "Could not build the contract for %s: %s"
                              % (module_path, exc))
        if not self.contract.variables:
            return self._halt(Stage.CONTRACT,
                              "%s declares no variables; there is nothing to resolve."
                              % module_path)

        # Templates pair by declared `source`, so every template is read (ADR 0013).
        templates = discover_templates(self.discovery.repo_root)
        paired = pair_modules_with_templates([module_path], templates).get(module_path)
        self.template = build_template_map(paired, os.path.basename(
            os.path.dirname(paired))) if paired else None
        self._ok(Stage.CONTRACT, "%s; template: %s"
                 % (self.contract.message, "yes" if self.template else "none"))
        return None

    def _seed_facts_from_ticket(self) -> Optional[PipelineResult]:
        """Round 0 -- read the ticket into slots (Stage 2 of the LLM boundary)."""
        from .section_a_iac.resolve.engine import resolve_module
        from .section_a_iac.resolve.models import FactSource
        from .section_a_iac.slots.filler import SlotFiller, apply_to_facts
        from .section_a_iac.slots.schema import build_slot_schema

        facts = FactsStore(self.contract)
        self.loop = Step4A(self.contract, self.template, self.platform, facts=facts)

        completer = self.completer
        if completer is None:
            from .section_a_iac.slots.filler import groq_completer
            completer = groq_completer()

        proof = resolve_module(self.contract, self.template, self.platform, facts)
        schema = build_slot_schema(self.contract, proof, self.template, self.platform)
        try:
            filled = SlotFiller(completer).fill(self.ticket_text, schema)
        except Exception as exc:                    # noqa: BLE001 - reported, not raised
            return self._halt(Stage.RESOLVE,
                              "Slot filling failed: %s" % exc)

        rejected = apply_to_facts(filled, facts, FactSource.JIRA, round=0)
        note = "%d value(s) from the ticket" % len(filled.accepted)
        if filled.invented_keys:
            note += "; %d invented key(s) dropped" % len(filled.invented_keys)
        if rejected:
            note += "; %d rejected by validation" % len(rejected)
        self._ok(Stage.RESOLVE, note)
        return None

    # -- the loop, and what follows it --------------------------------------

    def _advance(self) -> PipelineResult:
        return self._after_loop(self.loop.advance())

    def _after_loop(self, result) -> PipelineResult:
        if result.status is Step4AStatus.HALT:
            return self._halt(Stage.RESOLVE, result.message)

        if result.status is Step4AStatus.NEEDS_HITL:
            return PipelineResult(
                status=PipelineStatus.NEEDS_HITL,
                ticket_id=self.ticket_id,
                classification=self.classification,
                stages=list(self.stages),
                module_path=self.contract.module_path,
                module_ref=self.discovery.module_ref,
                git_source_url=self.discovery.git_source_url,
                hitl_request=result.hitl_request,
                message=result.message or "Waiting on %d answer(s)."
                        % len(result.hitl_request.questions))

        return self._render(result)

    def _placement(self) -> PlacementProposal:
        """Where the unit goes, read from the app repo (ADR 0026).

        The repo is already materialised -- `_discover()` walked it to build
        the platform context -- so this costs a filesystem read and no token.
        """
        repo = survey_local(self.app_root)
        return propose_placement(
            self.contract.module_path, repo,
            preferred_group=self.placement_answers.get("group"),
            variant=self.placement_answers.get("variant"))

    def _render(self, result) -> PipelineResult:
        """Only ever reached once the closure proof permits it (rule 12)."""
        from .section_a_iac.render.hcl_writer import render_unit
        from .section_a_iac.render.pr_body import render_pr_body
        from .section_a_iac.resolve.models import Action

        # Remembered so a placement answer can re-enter here without re-running
        # a resolution that is already closed.
        self.render_result = result

        placement = None
        if self.app_root:
            placement = self._placement()
            if placement.needs_ask:
                return PipelineResult(
                    status=PipelineStatus.NEEDS_HITL,
                    ticket_id=self.ticket_id,
                    classification=self.classification,
                    stages=list(self.stages),
                    module_path=self.contract.module_path,
                    module_ref=self.discovery.module_ref,
                    git_source_url=self.discovery.git_source_url,
                    placement=placement,
                    placement_question=placement.questions[0],
                    message=placement.reason)

        proof = result.proof
        rendered = render_unit(proof, self.contract, self.template, self.platform)
        unit_path = (placement.proposed_path if placement
                     and placement.proposed_path else self._propose_unit_path())
        body = render_pr_body(proof, self.contract, rendered, self.loop.facts,
                              ticket_key=self.ticket_id, unit_path=unit_path or "")
        omitted = sum(1 for r in proof.resolutions.values()
                      if r.action is Action.OMIT)
        self._ok(Stage.RENDER, "%d inputs, %d omitted"
                 % (len(rendered.rendered), omitted))

        return PipelineResult(
            status=PipelineStatus.COMPLETE,
            ticket_id=self.ticket_id,
            classification=self.classification,
            stages=list(self.stages),
            module_path=self.contract.module_path,
            module_ref=self.discovery.module_ref,
            git_source_url=self.discovery.git_source_url,
            unit_hcl=rendered.unit_hcl,
            pr_body=body,
            rendered_count=len(rendered.rendered),
            omitted_count=omitted,
            proposed_unit_path=unit_path,
            placement=placement,
            placement_is_provisional=not (placement and placement.proposed_path),
            message="Rendered %s at %s." % (self.contract.module_path,
                                            self.discovery.module_ref))

    def _propose_unit_path(self) -> Optional[str]:
        """The fallback, used only when the app repo could not be surveyed.

        `_placement()` reads the real convention out of the repo (ADR 0026).
        This remains for the case where there is no checkout to read -- the
        module name alone, with `placement_is_provisional` left True so no
        caller mistakes it for a decided location.
        """
        if self.contract is None:
            return None
        return self.contract.module_path.rsplit("/", 1)[-1]
