import re
import base64
from typing import List, Dict, Any, Optional
import httpx
from pydantic import BaseModel
from src.da.config import GitSettings, git_settings


def match_module_path(candidates: List[str], requested: str) -> "ModulePathMatch":
    """The case-sensitive matching rule, over an already-filtered candidate list.

    Pure, so the same rule serves both resolution routes -- the GitHub tree and a
    materialised checkout (ADR 0020). Two implementations of ADR 0004 would be
    two chances to get it wrong.

    Punctuation is ignored (`Function-App` == `Function_App`); case is not. A
    candidate matching only when lowercased is a near-miss, never a match.
    """
    clean_target = re.sub(r"[^a-zA-Z0-9]", "", requested)
    exact, near = [], []
    for path in candidates:
        folder = path.split("/")[-1]
        clean = re.sub(r"[^a-zA-Z0-9]", "", folder)
        if clean == clean_target:
            exact.append(path)
        elif clean.lower() == clean_target.lower():
            near.append(path)

    if exact:
        best = sorted(exact, key=lambda p: (len(p.split("/")), p))[0]
        return ModulePathMatch(path=best, message="Matched module directory '%s'." % best)

    if near:
        listed = ", ".join(sorted(near))
        return ModulePathMatch(
            case_mismatches=sorted(near),
            message=(
                "No module directory named '%s' exists. %d director(y/ies) differ "
                "only by case: %s. This is not treated as a match -- Terragrunt "
                "resolves this path on case-sensitive Linux CI runners, where the "
                "wrong casing fails." % (requested, len(near), listed)),
        )

    return ModulePathMatch(message="No module directory named '%s'." % requested)


class ModulePathMatch(BaseModel):
    """Outcome of resolving a resource type to a module directory.

    `path` is set only on an exact, case-correct match. A directory that differs
    from the requested name *only by case* is never returned as a match -- it is
    listed in `case_mismatches` instead, because Terragrunt resolves these paths
    on case-sensitive Linux CI runners where the wrong casing fails (ADR 0004).
    """

    path: Optional[str] = None
    case_mismatches: List[str] = []
    message: str = ""


class GitClient:
    def __init__(self, settings: Optional[GitSettings] = None):
        """Everything endpoint-specific comes from settings, nothing is baked in.

        The owner, the API base URL and the default branch were previously
        hardcoded here; each would have forced a code edit when this moves to
        Honeywell's GitHub Enterprise Server. See `da.config`.
        """
        self.settings = settings or git_settings()
        self.owner = self.settings.org
        self.base_api_url = self.settings.api_base_url.rstrip("/")
        self.azure_modules_pat = self.settings.modules_pat
        self.service_repo_pat = self.settings.app_pat

    @property
    def default_branch(self) -> str:
        return self.settings.default_branch

    def _get_headers(self, token: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "IPS-Deployment-Agent",
        }
        if token:
            headers["Authorization"] = f"token {token}"
        return headers

    def check_repo_exists(self, repo_name: str, use_service_pat: bool = False) -> bool:
        token = self.service_repo_pat if use_service_pat else self.azure_modules_pat
        url = f"{self.base_api_url}/repos/{self.owner}/{repo_name}"
        
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(url, headers=self._get_headers(token))
                return resp.status_code == 200
        except Exception:
            return False

    def list_repo_tree(self, repo_name: str, ref: Optional[str] = None, use_service_pat: bool = False) -> List[Dict[str, Any]]:
        ref = ref or self.default_branch
        token = self.service_repo_pat if use_service_pat else self.azure_modules_pat
        url = f"{self.base_api_url}/repos/{self.owner}/{repo_name}/git/trees/{ref}?recursive=1"

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(url, headers=self._get_headers(token))
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("tree", [])
                # Try fallback with service_repo_pat if failed
                if not use_service_pat and self.service_repo_pat:
                    resp_fb = client.get(url, headers=self._get_headers(self.service_repo_pat))
                    if resp_fb.status_code == 200:
                        return resp_fb.json().get("tree", [])
                return []
        except Exception:
            return []

    def ref_exists(self, repo_name: str, ref: str, use_service_pat: bool = False) -> bool:
        """Does this branch/tag/sha resolve in the repo?

        Distinguishing "the ref is absent" from "the module is absent" matters:
        the first is a pinning problem, the second is a catalogue gap, and only
        the second belongs in the Gap Register.
        """
        token = self.service_repo_pat if use_service_pat else self.azure_modules_pat
        url = f"{self.base_api_url}/repos/{self.owner}/{repo_name}/commits/{ref}"
        try:
            with httpx.Client(timeout=10.0) as client:
                return client.get(url, headers=self._get_headers(token)).status_code == 200
        except Exception:
            return False

    def find_nested_module_path(self, repo_name: str, normalized_resource: str, ref: Optional[str] = None, use_service_pat: bool = False) -> ModulePathMatch:
        """Resolve a resource type to a module directory, case-sensitively.

        Punctuation is still ignored (`Function-App` and `Function_App` are the
        same name), but case is not: a folder that matches only when lowercased
        is a **case-only near-miss** and is reported as its own error rather than
        silently accepted. Matching case-insensitively here would paper over the
        three real casing bugs that break Linux CI (ADR 0004).
        """
        tree = self.list_repo_tree(repo_name, ref=ref, use_service_pat=use_service_pat)
        blob_paths = {item.get("path") for item in tree if item.get("type") == "blob"}

        candidates = []
        for item in tree:
            if item.get("type") != "tree":
                continue
            path = item.get("path", "")
            # The ignore-list is a fixed set of our own literals, so testing it
            # case-insensitively resolves no module path and is benign.
            if any(ig in path.lower() for ig in
                   ["docs/", "examples/", "test/", "tests/", ".github/", "scratch/"]):
                continue
            if (f"{path}/variables.tf" in blob_paths
                    or f"{path}/main.tf" in blob_paths):
                candidates.append(path)

        match = match_module_path(candidates, normalized_resource)
        if not match.path and not match.case_mismatches:
            match.message = ("No module directory named '%s' in '%s'."
                             % (normalized_resource, repo_name))
        return match

    def get_file_content(self, repo_name: str, file_path: str, ref: Optional[str] = None, use_service_pat: bool = False) -> Optional[str]:
        ref = ref or self.default_branch
        token = self.service_repo_pat if use_service_pat else self.azure_modules_pat
        url = f"{self.base_api_url}/repos/{self.owner}/{repo_name}/contents/{file_path}?ref={ref}"

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(url, headers=self._get_headers(token))
                if resp.status_code != 200 and not use_service_pat and self.service_repo_pat:
                    resp = client.get(url, headers=self._get_headers(self.service_repo_pat))
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("encoding") == "base64":
                        content_bytes = base64.b64decode(data.get("content", ""))
                        return content_bytes.decode("utf-8")
        except Exception:
            pass

        return None
