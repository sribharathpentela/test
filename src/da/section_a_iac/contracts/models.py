"""Pydantic models for the four Phase 1 artifacts.

Data only. Construction lives in the builder modules:
    module_contract.py   -> ModuleContract
    template_map.py      -> TemplateMap
    platform_context.py  -> PlatformContext
    reconcile.py         -> ReconcileResult
"""

from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Type tree  (01-algorithm.md section 5)
# --------------------------------------------------------------------------

class TypeKind(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOL = "bool"
    ANY = "any"
    LIST = "list"
    SET = "set"
    MAP = "map"
    OBJECT = "object"
    TUPLE = "tuple"

    @property
    def is_scalar(self) -> bool:
        return self in (TypeKind.STRING, TypeKind.NUMBER, TypeKind.BOOL, TypeKind.ANY)


class ObjectAttr(BaseModel):
    """One attribute of an object({...}) type."""

    name: str
    node: "TypeNode"
    # optional() means the caller may leave it out -- so it is NOT tier T4.
    optional: bool = False
    default: Optional[Any] = None


class TypeNode(BaseModel):
    """A parsed HCL type expression.

    map/list/set carry `of`; object carries `attributes`; tuple carries
    `elements`. Scalars carry neither. `raw` is kept so a caller can always fall
    back to the source text.
    """

    kind: TypeKind
    raw: str = ""
    of: Optional["TypeNode"] = None
    attributes: Dict[str, ObjectAttr] = Field(default_factory=dict)
    elements: List["TypeNode"] = Field(default_factory=list)

    @property
    def adds_key_level(self) -> bool:
        """map(T) adds a key level in the fact path; object({}) does not."""
        return self.kind == TypeKind.MAP


# --------------------------------------------------------------------------
# Module contract  (variables.tf + main.tf + data.tf + local.tf -- never README)
# --------------------------------------------------------------------------

class RequiredTier(str, Enum):
    """01-algorithm.md section 4. Only T1 is visible from variables.tf alone."""

    T1 = "T1"  # declared with no default
    T2 = "T2"  # has a default, but a precondition rejects it
    T3 = "T3"  # payload map defaults to {}, which creates nothing
    T4 = "T4"  # nested object attribute not wrapped in optional()


class Validation(BaseModel):
    """A `validation { condition, error_message }` block from variables.tf.

    Rung 2 of the ladder -- an *authoritative* enum, unlike the advisory prose in
    a description (rung 7, WARN only).
    """

    condition: str
    error_message: str = ""
    # Extracted from `contains([...], var.self)`, when that is the shape.
    allowed_values: List[Any] = Field(default_factory=list)
    # Enums constraining an attribute of each entry in a collection, e.g.
    # {"version": ["11", ..., "16"]} from an alltrue([for ...]) condition.
    # These belong to the leaf, never to the map.
    attribute_enums: Dict[str, List[Any]] = Field(default_factory=dict)


class VariableSpec(BaseModel):
    name: str
    type_expr: str = ""
    type_tree: Optional[TypeNode] = None
    has_default: bool = False
    default: Optional[Any] = None
    description: str = ""
    sensitive: bool = False
    validations: List[Validation] = Field(default_factory=list)
    # Tiers this variable satisfies. Empty means not required.
    tiers: List[RequiredTier] = Field(default_factory=list)
    # For T4: dotted paths under this variable that are required.
    required_paths: List[str] = Field(default_factory=list)
    declared_in: str = ""

    @property
    def is_required(self) -> bool:
        return bool(self.tiers)

    @property
    def allowed_values(self) -> List[Any]:
        """Union of every authoritative enum declared on this variable."""
        values: List[Any] = []
        for validation in self.validations:
            for value in validation.allowed_values:
                if value not in values:
                    values.append(value)
        return values


class Precondition(BaseModel):
    """A lifecycle precondition found in main.tf, attributed to a variable."""

    variable: str
    condition: str
    error_message: str = ""
    found_in: str = ""


class ModuleContract(BaseModel):
    """Everything the algorithm needs about a module, from its .tf files."""

    module_path: str          # case-sensitive, e.g. "Databases/Managed-Redis"
    category: str = ""
    name: str = ""
    variables: Dict[str, VariableSpec] = Field(default_factory=dict)
    # The map that module "naming" iterates. None for flat modules.
    primary_payload_map: Optional[str] = None
    # Variables passed into module "naming" -- these decide the resource's name.
    naming_inputs: List[str] = Field(default_factory=list)
    preconditions: List[Precondition] = Field(default_factory=list)
    # Referenced as var.X somewhere but never declared -> HALT (Databases/Redis).
    referenced_undeclared: List[str] = Field(default_factory=list)
    # Declared but never referenced -> orphan (Recovery-Services-Vault).
    orphan_variables: List[str] = Field(default_factory=list)
    files_parsed: List[str] = Field(default_factory=list)
    message: str = ""

    @property
    def required(self) -> Dict[str, VariableSpec]:
        return {n: v for n, v in self.variables.items() if v.is_required}


# --------------------------------------------------------------------------
# Template map  (two-pass parse of terragrunt.template.hcl)
# --------------------------------------------------------------------------

class EntryState(str, Enum):
    LIVE = "live"              # appears uncommented
    COMMENTED = "commented"    # appears only behind a #
    BOTH = "both"              # both -> blueprint, UNWRAP


class EntryKind(str, Enum):
    """What the template value looks like. Branch 3 resolution table."""

    POINTER = "pointer"                # include.common.locals.X
    UPDATE_VALUE = "update_value"      # "<update-value>"
    LITERAL = "literal"                # a concrete value
    NULL = "null"
    EMPTY_MAP = "empty_map"            # {}
    EMPTY_LIST = "empty_list"          # []
    INTERPOLATION = "interpolation"    # some other ${...}
    UNKNOWN = "unknown"


class TemplateEntry(BaseModel):
    key: str
    state: EntryState
    kind: EntryKind = EntryKind.UNKNOWN
    raw_value: str = ""
    # The value as hcl2 parsed it. The raw pass only ever sees the first line,
    # so a multi-line block arrives there as "{" -- AKS's network_profile and
    # default_node_pool are both blocks. Structure comes from hcl2; the raw pass
    # is for comments and line numbers.
    parsed_value: Any = None
    has_parsed_value: bool = False
    line: int = 0               # live occurrence, else first commented
    pointer_target: str = ""    # for POINTER: the local name, e.g. "location"
    # Trailing-comment metadata the raw pass recovers.
    inline_comment: str = ""
    documented_default: Optional[str] = None   # from "# default: X"
    example: Optional[str] = None              # from "# e.g. X"
    # For BOTH: the commented lines that show the shape.
    blueprint_lines: List[int] = Field(default_factory=list)
    blueprint_text: str = ""
    # The section comment this key sat under in the template, e.g.
    # "Networking (private endpoint, from common.hcl)". The renderer reuses it
    # so a generated unit groups the way the hand-written ones do.
    group_comment: str = ""


class TemplateMap(BaseModel):
    module_slug: str                   # docs/examples/<slug>
    template_path: str = ""
    # From terraform { source = "...//<module_path>?ref=..." }
    declared_module_path: str = ""
    declared_ref_expr: str = ""
    entries: Dict[str, TemplateEntry] = Field(default_factory=dict)
    # Section comments inside `inputs` that have no keys under them at all.
    # "Tags inherited from common.hcl (app/owner/...)" explains why there is no
    # tags line; dropping it from a generated unit loses the explanation and
    # leaves a reviewer wondering. (line, text)
    standalone_comments: List[Tuple[int, str]] = Field(default_factory=list)
    message: str = ""

    @property
    def live_keys(self) -> List[str]:
        return [k for k, e in self.entries.items()
                if e.state in (EntryState.LIVE, EntryState.BOTH)]

    @property
    def blueprint_keys(self) -> List[str]:
        return [k for k, e in self.entries.items() if e.state == EntryState.BOTH]


# --------------------------------------------------------------------------
# Platform context  (common.hcl / env.hcl / project.hcl / context.hcl)
# --------------------------------------------------------------------------

class PlatformContext(BaseModel):
    """Read from the service repo. Never hardcode -- see ADR 0002 / 0006."""

    repo_root: str = ""
    # common.hcl inputs {} -- the 8 keys injected into every unit.
    auto_injected: Dict[str, str] = Field(default_factory=dict)
    # common.hcl locals that are valid pointer targets (the 7).
    pointer_locals: List[str] = Field(default_factory=list)
    # Pairs that are the same value under two names, e.g. region <-> location.
    alias_pairs: List[List[str]] = Field(default_factory=list)
    module_ref: str = ""
    iac_modules_repo: str = ""
    files_parsed: List[str] = Field(default_factory=list)
    message: str = ""

    @property
    def auto_injected_keys(self) -> List[str]:
        return sorted(self.auto_injected.keys())


# --------------------------------------------------------------------------
# Reconciliation  (01-algorithm.md section 9)
# --------------------------------------------------------------------------

class ReconcileStatus(str, Enum):
    OK = "OK"
    WARN = "WARN"
    HALT = "HALT"


class Finding(BaseModel):
    status: ReconcileStatus
    code: str
    detail: str
    key: str = ""


class ReconcileResult(BaseModel):
    module_path: str
    status: ReconcileStatus = ReconcileStatus.OK
    findings: List[Finding] = Field(default_factory=list)
    message: str = ""

    @property
    def halts(self) -> List[Finding]:
        return [f for f in self.findings if f.status == ReconcileStatus.HALT]

    @property
    def warns(self) -> List[Finding]:
        return [f for f in self.findings if f.status == ReconcileStatus.WARN]


ObjectAttr.model_rebuild()
TypeNode.model_rebuild()
