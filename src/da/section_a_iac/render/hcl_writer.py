"""Step 4c -- render the unit's terragrunt.hcl.

Renders only after the closure proof. Every declared variable must have landed
in exactly one branch and the ASK set must be empty; an unclassified variable is
a bug in the algorithm, not a value to guess (`01-algorithm.md` section 8).

The output follows the conventions of the units already in the repo, because
those units are reviewed by the people who wrote them:

* `include "root"` and `include "common"` first, `common` with `expose = true`;
* a `terraform` block whose `source` carries the module path and
  `${include.common.locals.module_ref}` -- the ref is *pointed at*, never
  written as a literal tag (ADR 0006);
* `inputs` grouped in template order with the template's own group comments,
  and `=` aligned within each group.

Key order follows the template, so a reviewer diffing against the template they
know sees the values change and nothing else. Variables the template never
mentions -- storage_account has 13 -- come last under their own heading, since
there is no template position to give them.
"""

from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from ..contracts.models import ModuleContract, PlatformContext, TemplateMap
from ..resolve.models import Action, ClosureProof, FactSource, Resolution

INDENT = "  "


class RenderResult(BaseModel):
    module_path: str
    unit_hcl: str = ""
    rendered: List[str] = Field(default_factory=list)
    omitted: List[str] = Field(default_factory=list)
    secrets: List[str] = Field(default_factory=list)
    blocked_reason: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.unit_hcl) and not self.blocked_reason


def render_unit(proof: ClosureProof,
                contract: ModuleContract,
                template: Optional[TemplateMap],
                platform: PlatformContext) -> RenderResult:
    """Render the unit file, or refuse and say why."""
    result = RenderResult(module_path=contract.module_path)

    if not proof.may_render:
        reasons = []
        if proof.unclassified:
            reasons.append("unclassified: %s" % ", ".join(proof.unclassified))
        if proof.ask_set:
            reasons.append("unanswered: %s" % ", ".join(proof.ask_set))
        if proof.halts:
            reasons.append("halts: %s" % "; ".join(proof.halts[:3]))
        result.blocked_reason = "; ".join(reasons) or "closure not proven"
        result.message = "refused to render -- %s" % result.blocked_reason
        return result

    groups = _group_in_template_order(proof, contract, template)

    lines = [
        'include "root" {',
        '%spath = find_in_parent_folders("root.hcl")' % INDENT,
        "}",
        "",
        'include "common" {',
        '%spath   = find_in_parent_folders("common.hcl")' % INDENT,
        "%sexpose = true" % INDENT,
        "}",
        "",
        "terraform {",
        "%s# Module ref is controlled centrally in common.hcl (module_ref)" % INDENT,
        '%ssource = "${include.common.locals.iac_modules_repo}//%s'
        '?ref=${include.common.locals.module_ref}"' % (INDENT, contract.module_path),
        "}",
        "",
        "inputs = {",
    ]

    standalone = list(template.standalone_comments) if template else []
    first_group = True
    for heading, entries, anchor in groups:
        if not entries:
            continue
        # Prose the template placed before this section, kept in position.
        while standalone and standalone[0][0] < anchor:
            _, text = standalone.pop(0)
            if not first_group:
                lines.append("")
            first_group = False
            lines.append("%s# %s" % (INDENT, text))
        if not first_group:
            lines.append("")
        first_group = False
        if heading:
            lines.append("%s# %s" % (INDENT, heading))
        lines.extend(_render_group(entries, result, template))
    for _, text in standalone:
        lines.append("")
        lines.append("%s# %s" % (INDENT, text))

    lines.append("}")
    result.unit_hcl = "\n".join(lines) + "\n"
    result.message = "%s: %d inputs rendered, %d omitted" % (
        contract.module_path, len(result.rendered), len(result.omitted))
    return result


# --------------------------------------------------------------------------
# Grouping and ordering
# --------------------------------------------------------------------------

def _group_in_template_order(proof: ClosureProof,
                             contract: ModuleContract,
                             template: Optional[TemplateMap]
                             ) -> List[Tuple[str, List[Resolution]]]:
    """Walk the template top to bottom, keeping its grouping comments."""
    emitted = {name: resolution
               for name, resolution in proof.resolutions.items()
               if resolution.renders}
    groups: List[Tuple[str, List[Resolution]]] = []
    placed = set()

    if template is not None:
        ordered = sorted((e for e in template.entries.values() if e.line),
                         key=lambda e: e.line)
        current_heading = ""
        current_line = 0
        current: List[Resolution] = []
        for entry in ordered:
            resolution = emitted.get(entry.key)
            if resolution is None:
                continue
            heading = _heading_for(entry, template)
            if heading != current_heading and current:
                groups.append((current_heading, current, current_line))
                current = []
            if not current:
                current_line = entry.line
            current_heading = heading
            current.append(resolution)
            placed.add(entry.key)
        if current:
            groups.append((current_heading, current, current_line))

    # Declared but absent from the template -- the invisible variables.
    leftovers = [emitted[n] for n in sorted(emitted) if n not in placed]
    if leftovers:
        groups.append(("Declared by the module but not present in the template",
                       leftovers, 10 ** 9))
    return groups


def _heading_for(entry, template: TemplateMap) -> str:
    """The template's own group comment, when it has one."""
    return getattr(entry, "group_comment", "") or ""


def _render_group(entries: List[Resolution], result: RenderResult,
                  template=None) -> List[str]:
    """Render one group with `=` aligned to its widest key."""
    width = max(len(r.variable) for r in entries)
    lines = []
    for resolution in entries:
        rendered = _value_for(resolution)
        if rendered is None:
            continue
        comment = _comment_for(resolution) or _template_comment(resolution,
                                                                template)
        if comment and "\n" in rendered:
            # A trailing comment after a multi-line block dangles off the
            # closing brace, where it reads as belonging to nothing.
            lines.append("%s# %s" % (INDENT, comment))
            comment = ""
        line = "%s%-*s = %s" % (INDENT, width, resolution.variable, rendered)
        if comment:
            line += "  # %s" % comment
        lines.append(line)
        result.rendered.append(resolution.variable)
        if resolution.action is Action.SECRET:
            result.secrets.append(resolution.variable)
    return lines


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------

def _value_for(resolution: Resolution) -> Optional[str]:
    if resolution.action is Action.POINTER:
        return "include.common.locals.%s" % resolution.pointer_target
    if resolution.action is Action.SECRET:
        # Never the value. The slot is an object naming an environment
        # variable, and it renders as a lookup (section 7).
        name = resolution.value
        if isinstance(name, dict):
            name = name.get("env_var_name", "")
        return 'get_env("%s")' % name
    return to_hcl(resolution.value)


def to_hcl(value: Any, indent: int = 1) -> str:
    """Render a Python value as HCL.

    `null`, `""` and a value are three distinct states and are never collapsed
    (`01-algorithm.md` section 6).
    """
    pad = INDENT * indent
    inner_pad = INDENT * (indent + 1)

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        items = ", ".join(to_hcl(v, indent + 1) for v in value)
        if len(items) <= 90:
            return "[%s]" % items
        rendered = ["["]
        for item in value:
            rendered.append("%s%s," % (inner_pad, to_hcl(item, indent + 1)))
        rendered.append("%s]" % pad)
        return "\n".join(rendered)
    if isinstance(value, dict):
        if not value:
            return "{}"
        rendered = ["{"]
        width = max(len(str(k)) for k in value)
        for key in value:
            rendered.append("%s%-*s = %s" % (inner_pad, width, key,
                                             to_hcl(value[key], indent + 1)))
        rendered.append("%s}" % pad)
        return "\n".join(rendered)
    return '"%s"' % value


def _template_comment(resolution: Resolution, template) -> str:
    """The template's own inline note, when it still applies.

    `public_network_access_enabled = false # set true only if public access is
    required` is guidance worth carrying into the generated unit. It is carried
    only when the value still comes from the template: once the ticket supplies
    a value, `# REQUIRED, e.g. "wfi-core"` describes a state that no longer
    holds and would mislead the reviewer.
    """
    if template is None or resolution.source is not FactSource.TEMPLATE:
        return ""
    entry = template.entries.get(resolution.variable)
    if entry is None:
        return ""
    note = (entry.inline_comment or "").strip()
    if not note or note.lower().startswith(("required", "default:")):
        return ""
    return note


def _comment_for(resolution: Resolution) -> str:
    """A short note, only where it earns its place.

    The default is rendered as HCL, not as a Python repr -- a comment reading
    `module default is False` in a file where the literal is `false` invites a
    reader to wonder which one Terraform sees.
    """
    if not resolution.disclose:
        return ""
    if resolution.has_module_default:
        return "module default is %s" % to_hcl(resolution.module_default)
    if "confirm" in (resolution.disclosure or "").lower():
        return "from the ticket -- confirm"
    return ""
