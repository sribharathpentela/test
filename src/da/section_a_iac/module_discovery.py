"""Step 3a -- resolve the requested resource to a module, at the pinned ref.

The order matters and each step guards the next:

    common.hcl  ->  ref exists?  ->  materialise  ->  resolve path  ->  contract

1. The pin is read from the **service repo's** `common.hcl`; DA verifies it and
   never selects a version (ADR 0006).
2. A ref that does not resolve is a *pinning* failure, not a catalogue gap, and
   must not reach the Gap Register (ADR 0018).
3. The repo is materialised from Git at that ref (ADR 0020/0021); the contract
   builders read the cache, never a hand-maintained copy.
4. The module directory is matched case-sensitively (ADR 0004).
"""

import os
from typing import Callable, List, Optional

from pydantic import BaseModel

from src.da.clients.git_client import GitClient, match_module_path
from src.da.section_a_iac.contracts import hcl_loader as H
from src.da.section_a_iac.contracts.materialise import (MaterialiseError, MaterialisedRepo,
                                          materialise)
from src.da.gap_register.gap_logger import GapEntry, GapRegisterLogger
from src.da.schemas.base_intake import GateAIntakeOutput

COMMON_HCL = "common.hcl"


class ModulePin(BaseModel):
    """What the service repo's common.hcl says about module sourcing."""

    module_ref: str = ""
    iac_modules_repo: str = ""


class ModuleDiscoveryResult(BaseModel):
    found: bool
    resource_type: str
    central_repo_name: str
    module_rel_path: Optional[str] = None
    module_ref: Optional[str] = None
    git_source_url: Optional[str] = None
    module_dir: Optional[str] = None
    repo_root: Optional[str] = None
    case_mismatches: List[str] = []
    gap_entry: Optional[GapEntry] = None
    message: str


class ModuleDiscoveryEngine:
    def __init__(self,
                 git_client: Optional[GitClient] = None,
                 materialiser: Optional[Callable[[str, str], MaterialisedRepo]] = None):
        self.git_client = git_client or GitClient()
        # Injectable so tests never reach the network; the default is the real
        # Git fetch, because Git is the only source of truth (ADR 0021).
        self.materialiser = materialiser or (
            lambda repo, ref: materialise(repo, ref, settings=self.git_client.settings))

    @property
    def DEFAULT_CENTRAL_MODULE_REPO(self) -> str:
        return self.git_client.settings.modules_repo

    def read_module_pin(self, service_repo: str) -> Optional[ModulePin]:
        """Read `module_ref` and `iac_modules_repo` from the service repo.

        Both are platform decisions pinned centrally. Returns None if the file is
        unreachable, unparseable or declares no `module_ref` -- each a HALT for
        the caller, never a fallback.
        """
        content = self.git_client.get_file_content(
            repo_name=service_repo, file_path=COMMON_HCL, use_service_pat=True)
        if not content:
            return None
        try:
            data = H.load_hcl_text(content, label="%s/%s" % (service_repo, COMMON_HCL))
        except H.HclParseError:
            return None

        pin = ModulePin()
        for block in data.get("locals", []) or []:
            if not isinstance(block, dict):
                continue
            if "module_ref" in block and not pin.module_ref:
                value = H.unquote(block["module_ref"])
                if isinstance(value, str):
                    pin.module_ref = value.strip()
            if "iac_modules_repo" in block and not pin.iac_modules_repo:
                value = H.unquote(block["iac_modules_repo"])
                if isinstance(value, str):
                    pin.iac_modules_repo = value.strip()
        return pin if pin.module_ref else None

    def discover_module_in_central_registry(
        self,
        intake_data: GateAIntakeOutput,
        central_repo: Optional[str] = None,
    ) -> ModuleDiscoveryResult:
        central_repo_name = central_repo or self.DEFAULT_CENTRAL_MODULE_REPO
        resource_type = intake_data.resource_type or "unknown"
        ticket_id = (intake_data.ticket_metadata.ticket_id
                     if intake_data.ticket_metadata else "UNKNOWN")
        env = intake_data.environment or "dev"

        def halt(message, **kw):
            return ModuleDiscoveryResult(
                found=False, resource_type=resource_type,
                central_repo_name=central_repo_name, message=message, **kw)

        # --- 1. the pin ------------------------------------------------------
        service_repo = intake_data.target_service_repo
        if not service_repo or not service_repo.strip():
            return halt(
                "\U0001f6d1 [STEP 3a HALT] Cannot read the pinned module_ref: intake "
                "carries no target_service_repo, and the ref lives in that repo's "
                "%s." % COMMON_HCL)

        pin = self.read_module_pin(service_repo)
        if pin is None:
            return halt(
                "\U0001f6d1 [STEP 3a HALT] No `module_ref` could be read from "
                "'%s/%s'. The module version is pinned centrally and DA verifies it "
                "rather than choosing one (ADR 0006), so there is no ref to fall back "
                "to." % (service_repo, COMMON_HCL))

        module_ref = pin.module_ref

        # --- 2. does the ref resolve? (ADR 0018) ------------------------------
        if not self.git_client.ref_exists(central_repo_name, module_ref):
            return halt(
                "\U0001f6d1 [STEP 3a REF NOT FOUND] '%s/%s' pins module_ref '%s', but "
                "that ref does not resolve in '%s'. The module catalogue was not "
                "consulted: DA verifies the pin and does not substitute another ref "
                "(ADR 0006). Either the pin is wrong or the tag is missing from that "
                "repo." % (service_repo, COMMON_HCL, module_ref, central_repo_name),
                module_ref=module_ref)

        # --- 3. materialise from Git (ADR 0020/0021) --------------------------
        try:
            repo = self.materialiser(central_repo_name, module_ref)
        except MaterialiseError as exc:
            return halt(
                "\U0001f6d1 [STEP 3a FETCH FAILED] Could not materialise '%s' at "
                "'%s': %s" % (central_repo_name, module_ref, exc),
                module_ref=module_ref)

        # --- 4. resolve the module directory, case-sensitively (ADR 0004) -----
        match = match_module_path(_module_dirs(repo.root), resource_type)

        if not match.path:
            if match.case_mismatches:
                return halt("\U0001f6d1 [STEP 3a CASE MISMATCH] %s" % match.message,
                            module_ref=module_ref,
                            case_mismatches=match.case_mismatches,
                            repo_root=repo.root)

            reason = ("Requested resource_type '%s' was not found in Central Template "
                      "Registry '%s' at pinned ref '%s'."
                      % (resource_type, central_repo_name, module_ref))
            return halt(
                "\U0001f6d1 [STEP 3a MODULE NOT FOUND] %s Logged entry to Gap "
                "Register." % reason,
                module_ref=module_ref, repo_root=repo.root,
                gap_entry=GapRegisterLogger.log_unsupported_resource(
                    ticket_id=ticket_id, requested_resource_type=resource_type,
                    environment=env, reason=reason))

        module_path = match.path
        # X13: the module repo URL is declared in common.hcl, not assembled from a
        # hardcoded host -- the same value the rendered unit points at.
        settings = self.git_client.settings
        base = pin.iac_modules_repo or "git::%s/%s/%s.git" % (
            settings.api_base_url.rstrip("/"), settings.org, central_repo_name)

        return ModuleDiscoveryResult(
            found=True,
            resource_type=resource_type,
            central_repo_name=central_repo_name,
            module_rel_path=module_path,
            module_ref=module_ref,
            git_source_url="%s//%s?ref=%s" % (base, module_path, module_ref),
            module_dir=repo.module_dir(module_path),
            repo_root=repo.root,
            message=("✅ [STEP 3a MODULE FOUND] Verified '%s' at pinned ref '%s' "
                     "(from %s/%s); %s."
                     % (module_path, module_ref, service_repo, COMMON_HCL,
                        repo.message)),
        )


def _module_dirs(repo_root: str) -> List[str]:
    """Every `<Category>/<Module>` holding a variables.tf, case preserved.

    Mirrors `contracts.discover_modules` but tolerates a repo whose top level
    holds files as well as category directories.
    """
    found = []
    for category in sorted(os.listdir(repo_root)):
        category_dir = os.path.join(repo_root, category)
        if not os.path.isdir(category_dir) or category.startswith("."):
            continue
        for name in sorted(os.listdir(category_dir)):
            if os.path.isfile(os.path.join(category_dir, name, "variables.tf")):
                found.append("%s/%s" % (category, name))
    return found
