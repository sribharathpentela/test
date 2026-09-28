import os
import re
from typing import Optional
from pydantic import BaseModel
from src.da.clients.git_client import GitClient
from src.da.schemas.base_intake import GateAIntakeOutput


class UsageDiscoveryResult(BaseModel):
    target_repo_exists: bool
    target_branch_exists: bool
    target_branch_name: str
    is_duplicate_conflict: bool
    existing_conflicting_folder: Optional[str] = None
    proposed_folder_name: Optional[str] = None
    message: str


class UsageDiscoveryEngine:
    def __init__(self, git_client: Optional[GitClient] = None):
        self.git_client = git_client or GitClient()

    def discover_usage_in_target_service_repo(
        self,
        intake_data: GateAIntakeOutput,
        target_repo_override: Optional[str] = None,
    ) -> UsageDiscoveryResult:
        target_repo = target_repo_override or intake_data.target_service_repo
        if not target_repo:
            return UsageDiscoveryResult(
                target_repo_exists=False,
                target_branch_exists=False,
                target_branch_name="unknown",
                is_duplicate_conflict=False,
                message="🛑 [STEP 3b ERROR] Target service repository name missing from intake data.",
            )

        # 1. Verify Target Service Repo Existence in Git
        repo_exists = self.git_client.check_repo_exists(
            target_repo, use_service_pat=True
        )
        if not repo_exists:
            return UsageDiscoveryResult(
                target_repo_exists=False,
                target_branch_exists=False,
                target_branch_name="unknown",
                is_duplicate_conflict=False,
                message=f"🛑 [STEP 3b ERROR] Target service repository '{target_repo}' was not found in Git organization.",
            )

        # 2. Verify Target Branch Existence (env maps to branch)
        env = (intake_data.environment or "dev").lower()
        target_branch = env

        # Check branch existence via tree fetch
        tree = self.git_client.list_repo_tree(target_repo, ref=target_branch, use_service_pat=True)
        if not tree:
            # The repo default is configuration, not the literal "main" -- HON's
            # monorepo default branch differs from this mirror's.
            fallback = self.git_client.default_branch
            tree_main = self.git_client.list_repo_tree(target_repo, ref=fallback, use_service_pat=True)
            if tree_main:
                target_branch = fallback
                tree = tree_main
            else:
                return UsageDiscoveryResult(
                    target_repo_exists=True,
                    target_branch_exists=False,
                    target_branch_name=env,
                    is_duplicate_conflict=False,
                    message=f"🛑 [STEP 3b ERROR] Target branch '{env}' does not exist in repository '{target_repo}'.",
                )

        # 3. 2-Tier Duplication Inspection
        resource_type = intake_data.resource_type or "resource"
        requested_prefix = intake_data.resource_prefix or ""

        # Folder resolution is case-sensitive (ADR 0004) -- the same rule as
        # git_client.find_nested_module_path, which this scan duplicated.
        clean_requested_type = re.sub(r"[^a-zA-Z0-9]", "", resource_type)
        # The prefix comparison below is a *value* comparison, not a path
        # resolution, so it stays case-insensitive. Parked as X10.
        clean_requested_prefix = re.sub(r"[^a-zA-Z0-9]", "", requested_prefix).lower()

        conflicting_folder = None
        blob_paths = {item.get("path") for item in tree if item.get("type") == "blob"}

        for item in tree:
            if item.get("type") == "tree":
                path = item.get("path", "")
                # Fixed set of our own literals -- resolves no path, so testing
                # it case-insensitively is benign and stays.
                lower_path = path.lower()
                if any(ignored in lower_path for ignored in ["docs/", "examples/", "test/", "tests/", ".github/", "scratch/"]):
                    continue

                folder_name = path.split("/")[-1]
                clean_folder = re.sub(r"[^a-zA-Z0-9]", "", folder_name)

                if clean_requested_type in clean_folder or clean_folder in clean_requested_type:
                    hcl_path = f"{path}/terragrunt.hcl"
                    if hcl_path in blob_paths:
                        content = self.git_client.get_file_content(
                            target_repo, hcl_path, ref=target_branch, use_service_pat=True
                        )
                        if content:
                            prefix_match = re.search(r'resource_prefix\s*=\s*"([^"]+)"', content)
                            if prefix_match:
                                existing_prefix = prefix_match.group(1).strip()
                                clean_existing_prefix = re.sub(r"[^a-zA-Z0-9]", "", existing_prefix).lower()
                                if clean_existing_prefix == clean_requested_prefix:
                                    conflicting_folder = path
                                    break

        if conflicting_folder:
            return UsageDiscoveryResult(
                target_repo_exists=True,
                target_branch_exists=True,
                target_branch_name=target_branch,
                is_duplicate_conflict=True,
                existing_conflicting_folder=conflicting_folder,
                message=f"🚨 [STEP 3b RESOURCE CONFLICT] Resource '{resource_type}' with prefix '{requested_prefix}' ALREADY exists on branch '{target_branch}' inside folder '{conflicting_folder}'.",
            )

        # 4. Propose Unique Folder Name
        # resource_prefix is optional since M8, so it may be absent here. The
        # real folder convention is `shared/AKS`, `la/FunctionApp/android` --
        # replacing this `{type}-{prefix}` guess is M7.
        clean_type_slug = resource_type.replace(" ", "-")
        clean_prefix_slug = requested_prefix.replace(" ", "-")
        proposed_folder = (f"{clean_type_slug}-{clean_prefix_slug}"
                           if clean_prefix_slug else clean_type_slug)

        # Case-sensitive: two folders differing only by case are two folders on
        # the Linux CI runner that resolves them (ADR 0004).
        existing_folder_names = {item.get("path").split("/")[-1] for item in tree}
        if proposed_folder in existing_folder_names:
            proposed_folder = f"{proposed_folder}-02"

        return UsageDiscoveryResult(
            target_repo_exists=True,
            target_branch_exists=True,
            target_branch_name=target_branch,
            is_duplicate_conflict=False,
            proposed_folder_name=proposed_folder,
            message=f"✅ [STEP 3b USAGE DISCOVERY PASSED] Target repo '{target_repo}' and branch '{target_branch}' validated. Proposed folder: '{proposed_folder}'.",
        )
