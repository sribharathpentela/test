"""M5/M7 -- where a new unit goes in the app monorepo.

**Q10 asked the wrong question.** It concluded that the unit folder name
"cannot be an algorithm", and for *deriving* the name that is correct: `APIM`
and `Databricks` are human abbreviations of `API-Management/API-Management-service`
and `Databricks/Azure-Databricks`, and no transformation produces them.

But the name never had to be derived. Every existing unit declares the module
it wraps, in its own `terragrunt.hcl`:

    source = "${include.common.locals.iac_modules_repo}//Containers/AKS?ref=..."

So the folder name is **observed, not computed** -- by the same rule ADR 0013
already uses to pair templates: match on the declared `source` path. All of
Q10's hard cases resolve this way (`APIM`, `Databricks`, `KeyVault`,
`FunctionApp`, `StorageAccount`, `EventHub`).

Groups are read the same way, from their own marker. **A group is a folder with
a `project.hcl`, not a folder that happens to contain units** -- `wfi/` holds a
`project.hcl` and no units at all, so inferring groups from unit paths would
hide a legitimate and currently empty placement target. `project.hcl` also
carries `project_name`, which is M5's app dimension and what makes the group
question readable to a human.

What the repo does *not* record is which group a new unit belongs to. `la/` and
`core/` both hold a `FunctionApp` and neither file says why. Nor does anything
supply a new variant name. Those are ownership judgements, so placement asks --
with the candidates and the evidence attached, and without prompting for them
itself (ADR 0022).

The guess this replaces produced `AKS-wfi-core`, which is not a path this repo
has ever contained.

Folder matching is case-sensitive throughout (ADR 0004).
"""

import re
from typing import Callable, Dict, List, Optional

from pydantic import BaseModel, Field

# `source = "<anything>//<module path>?ref=<anything>"`. The leading `[^"]*` is
# greedy on purpose: it binds to the *last* `//` inside the quoted string, so a
# source written as a full `git::https://...` URL yields the module path rather
# than everything after `https:`.
_SOURCE_RE = re.compile(r'source\s*=\s*"[^"]*//([^"?]+)')

_PROJECT_NAME_RE = re.compile(r'project_name\s*=\s*"([^"]*)"')

# A group is marked by this file. Nothing else qualifies a top-level folder.
GROUP_MARKER = "project.hcl"

UNIT_MARKER = "terragrunt.hcl"


class ObservedGroup(BaseModel):
    """A top-level folder carrying a `project.hcl`."""

    name: str                          # "wfi"
    project_name: Optional[str] = None # "WFI" -- the `app` tag (M5)


class ObservedUnit(BaseModel):
    """One `terragrunt.hcl` already in the app repo, and the module it wraps."""

    path: str                          # "la/FunctionApp/android"
    group: str                         # "la"
    unit: str                          # "FunctionApp"
    variant: Optional[str] = None      # "android"
    module_path: str                   # "Compute/Function-app"


class RepoSurvey(BaseModel):
    """What the app repo currently contains. Facts only, no proposals."""

    groups: List[ObservedGroup] = Field(default_factory=list)
    units: List[ObservedUnit] = Field(default_factory=list)

    @property
    def group_names(self) -> List[str]:
        return [g.name for g in self.groups]

    def label(self, group: str) -> str:
        """`shared (Shared)` -- what a human should see in the question."""
        for g in self.groups:
            if g.name == group:
                return ("%s (%s)" % (g.name, g.project_name)
                        if g.project_name else g.name)
        return group


class PlacementQuestion(BaseModel):
    """A placement decision DA will not make for itself.

    Deliberately not a `HitlQuestion`: that one carries `variable` and a JSON
    type because it answers "what value does this `variables.tf` entry take".
    Placement answers "where does this unit live", which is not a variable and
    has no module schema behind it.
    """

    field: str                                          # "group" | "variant" | "unit"
    prompt: str
    allowed_values: List[str] = Field(default_factory=list)
    # What to show beside each allowed value, where a bare folder name is not
    # self-explanatory.
    labels: Dict[str, str] = Field(default_factory=dict)
    # Free text is accepted only where the tree cannot enumerate the answers --
    # a variant name for a service that does not exist yet.
    open_ended: bool = False
    why: str = ""


class PlacementProposal(BaseModel):
    module_path: str
    unit: Optional[str] = None
    # True when `unit` is a mechanical guess because no existing unit wraps this
    # module. A guess is never rendered without confirmation.
    unit_is_guess: bool = False

    proposed_path: Optional[str] = None
    candidates: List[str] = Field(default_factory=list)
    groups: List[str] = Field(default_factory=list)

    questions: List[PlacementQuestion] = Field(default_factory=list)
    evidence: List[str] = Field(default_factory=list)
    reason: str = ""

    @property
    def needs_ask(self) -> bool:
        return bool(self.questions)


def parse_unit_source(text: str) -> Optional[str]:
    """The module path a unit's `terragrunt.hcl` declares, or None."""
    if not text:
        return None
    match = _SOURCE_RE.search(text)
    return match.group(1).strip() if match else None


def parse_project_name(text: str) -> Optional[str]:
    """The `project_name` local from a `project.hcl`, or None."""
    if not text:
        return None
    match = _PROJECT_NAME_RE.search(text)
    return match.group(1).strip() if match else None


def _unit_from_folder(folder: str) -> Optional[ObservedUnit]:
    parts = [p for p in folder.split("/") if p]
    if len(parts) < 2:
        return None
    return ObservedUnit(
        path=folder,
        group=parts[0],
        unit=parts[1],
        variant="/".join(parts[2:]) or None,
        module_path="",
    )


def survey(tree: List[dict],
           get_content: Callable[[str], Optional[str]]) -> RepoSurvey:
    """Read the app repo's groups and units.

    `tree` is a GitHub tree listing; `get_content` fetches one blob by path.
    Anything whose marker file cannot be parsed is **skipped, never guessed at**
    -- an unreadable folder must not become evidence for a placement.
    """
    groups: List[ObservedGroup] = []
    units: List[ObservedUnit] = []

    for item in tree:
        if item.get("type") != "blob":
            continue
        path = item.get("path", "")
        parts = path.split("/")

        if len(parts) == 2 and parts[1] == GROUP_MARKER:
            groups.append(ObservedGroup(
                name=parts[0],
                project_name=parse_project_name(get_content(path) or ""),
            ))
            continue

        if parts[-1] != UNIT_MARKER:
            continue
        unit = _unit_from_folder("/".join(parts[:-1]))
        if unit is None:
            continue
        module_path = parse_unit_source(get_content(path) or "")
        if not module_path:
            continue
        unit.module_path = module_path
        units.append(unit)

    # A unit under a folder with no `project.hcl` is not in a group this repo
    # recognises, so it cannot be evidence for placing anything.
    known = {g.name for g in groups}
    units = [u for u in units if u.group in known]

    groups.sort(key=lambda g: g.name)
    units.sort(key=lambda u: u.path)
    return RepoSurvey(groups=groups, units=units)


def propose_placement(module_path: str,
                      repo: RepoSurvey,
                      preferred_group: Optional[str] = None,
                      variant: Optional[str] = None) -> PlacementProposal:
    """Where a new unit wrapping `module_path` should go.

    Returns a proposal plus whatever questions remain. `preferred_group` and
    `variant` are answers from an earlier round -- supplying them is how the
    caller closes the questions this returns.
    """
    groups = repo.group_names
    labels = {g.name: repo.label(g.name) for g in repo.groups}
    # Case-sensitive (ADR 0004): `Security/Keyvault` and `Security/KeyVault`
    # are two different modules on the Linux runner that resolves them.
    matches = [u for u in repo.units if u.module_path == module_path]

    proposal = PlacementProposal(module_path=module_path, groups=groups)

    if not matches:
        # Nothing in the repo wraps this module, so the folder name is not
        # observable. Guess mechanically, mark it a guess, and ask.
        guess = module_path.split("/")[-1].replace("-", "").replace("_", "")
        proposal.unit = guess
        proposal.unit_is_guess = True
        proposal.reason = (
            "No existing unit wraps '%s', so neither the folder name nor the "
            "group can be read from the repo." % module_path)
        proposal.evidence.append(
            "searched %d units across %d groups; no source matches '%s'"
            % (len(repo.units), len(groups), module_path))
        proposal.candidates = ["%s/%s" % (g, guess) for g in groups]
        proposal.questions.append(PlacementQuestion(
            field="unit",
            prompt="What should the unit folder be called?",
            allowed_values=[guess],
            open_ended=True,
            why=("'%s' is a mechanical guess from the module path. This repo's "
                 "names are not derivable -- 'API-Management/"
                 "API-Management-service' is filed as 'APIM'." % guess),
        ))
        proposal.questions.append(PlacementQuestion(
            field="group",
            prompt="Which group should it live under?",
            allowed_values=groups,
            labels=labels,
            why="Group is an ownership decision; the repo does not record it.",
        ))
        return proposal

    # The folder name is observed. If existing units disagree, say so rather
    # than picking one.
    names = sorted({u.unit for u in matches})
    proposal.unit = names[0]
    for u in matches:
        proposal.evidence.append(
            "%s declares source '%s'" % (u.path, u.module_path))

    if len(names) > 1:
        proposal.reason = (
            "Existing units wrapping '%s' use more than one folder name."
            % module_path)
        proposal.questions.append(PlacementQuestion(
            field="unit",
            prompt="Which folder name applies here?",
            allowed_values=names,
            why="The repo is inconsistent for this module; both names are in use.",
        ))
        return proposal

    unit = names[0]
    hosting = sorted({u.group for u in matches})
    # Either kind of existing unit rules out a bare `<group>/<unit>`: a bare one
    # already holds the path, and a variant one puts the folder in variant mode
    # (`la/FunctionApp/` is a directory of units, not a unit). So a new unit in
    # a hosting group is a variant in both cases -- but only the bare case also
    # needs the existing unit moved, so the two are tracked apart.
    bare = {u.group for u in matches if not u.variant}
    occupied = set(hosting)

    proposal.candidates = [
        "%s/%s/<variant>" % (g, unit) if g in occupied else "%s/%s" % (g, unit)
        for g in groups
    ]

    group = preferred_group
    if group is None:
        free = [g for g in groups if g not in occupied]
        proposal.reason = (
            "'%s' appears under %s; %s"
            % (unit,
               ", ".join("%s/" % g for g in hosting),
               ("free groups: %s" % ", ".join(free)) if free
               else "every group already hosts one"))
        proposal.questions.append(PlacementQuestion(
            field="group",
            prompt="Which group should this unit live under?",
            allowed_values=groups,
            labels=labels,
            why="; ".join(proposal.evidence),
        ))
        return proposal

    if group in occupied and variant is None:
        siblings = sorted(u.variant for u in matches
                          if u.group == group and u.variant)
        if group in bare:
            # `<group>/<unit>/terragrunt.hcl` exists, so nesting a variant under
            # it would make a unit contain a unit. DA cannot resolve that on its
            # own -- it names the consequence and lets a human decide.
            proposal.reason = (
                "'%s/%s' is itself a unit, so adding a variant beside it means "
                "moving the existing one into a variant folder first."
                % (group, unit))
        else:
            proposal.reason = (
                "'%s/%s' is a folder of variants, so a new unit needs a variant "
                "name." % (group, unit))
        proposal.questions.append(PlacementQuestion(
            field="variant",
            prompt="What should the variant folder be called?",
            allowed_values=siblings,
            open_ended=True,
            why=("existing variants under %s/%s: %s"
                 % (group, unit, ", ".join(siblings) if siblings else "none")),
        ))
        return proposal

    parts = [group, unit] + ([variant] if variant else [])
    proposal.proposed_path = "/".join(parts)
    return proposal


def survey_local(root: str) -> RepoSurvey:
    """`survey()` against a materialised checkout rather than a tree listing.

    The app repo is already on disk by the time placement runs -- `_discover()`
    materialises it to build the platform context (ADR 0020) -- so walking it
    costs nothing and needs no token. The GitHub-shaped `survey()` stays for a
    caller that has a tree and no checkout.
    """
    import os

    items = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            items.append({"type": "blob", "path": rel.replace(os.sep, "/")})

    def read(path: str) -> Optional[str]:
        try:
            with open(os.path.join(root, path), encoding="utf-8") as handle:
                return handle.read()
        except (OSError, UnicodeDecodeError):
            return None

    return survey(items, read)
