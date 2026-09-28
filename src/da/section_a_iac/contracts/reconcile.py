"""Reconcile a module contract against its template: OK | WARN | HALT.

01-algorithm.md section 9:

    template key not declared in variables.tf     -> HALT
    variable referenced in .tf but never declared -> HALT
    template source path != discovered path       -> WARN
    path differs only by case                     -> distinct error (Linux CI)
    no template exists for the module             -> HALT

The case-only mismatch is reported under its own code and never folded into the
generic path warning. Three real paths differ only by case; they work on Windows
and macOS and fail on Linux CI, and a forgiving comparison hides all three
(ADR 0004).
"""

import os
import re
from typing import Dict, List, Optional

from .models import (Finding, ModuleContract, ReconcileResult, ReconcileStatus,
                     TemplateMap)

NO_TEMPLATE_MESSAGE = ("no template exists for this service module; "
                       "please check with DevOps or create one")

# Codes are stable identifiers; the detail carries the specifics.
CODE_NO_TEMPLATE = "no_template"
CODE_UNDECLARED_TEMPLATE_KEY = "template_key_not_declared"
CODE_UNDECLARED_REFERENCE = "referenced_but_never_declared"
CODE_PATH_MISMATCH = "source_path_mismatch"
CODE_PATH_CASE_MISMATCH = "source_path_case_mismatch"
CODE_UNPARSEABLE = "contract_unparseable"
CODE_ORPHAN = "orphan_variable"


def reconcile(contract: ModuleContract,
              template: Optional[TemplateMap]) -> ReconcileResult:
    """Compare one contract with one template."""
    result = ReconcileResult(module_path=contract.module_path)
    findings: List[Finding] = []

    if not contract.variables and contract.message:
        findings.append(Finding(
            status=ReconcileStatus.HALT,
            code=CODE_UNPARSEABLE,
            detail=contract.message,
        ))

    if template is None or not template.template_path:
        findings.append(Finding(
            status=ReconcileStatus.HALT,
            code=CODE_NO_TEMPLATE,
            detail=NO_TEMPLATE_MESSAGE,
        ))
    else:
        declared = set(contract.variables)
        for key in sorted(template.entries):
            if key not in declared:
                findings.append(Finding(
                    status=ReconcileStatus.HALT,
                    code=CODE_UNDECLARED_TEMPLATE_KEY,
                    detail="template sets %r, which %s/variables.tf does not declare"
                           % (key, contract.module_path),
                    key=key,
                ))
        findings.extend(_compare_source_path(contract, template))

    for name in contract.referenced_undeclared:
        findings.append(Finding(
            status=ReconcileStatus.HALT,
            code=CODE_UNDECLARED_REFERENCE,
            detail="var.%s is used in the module but never declared" % name,
            key=name,
        ))

    for name in contract.orphan_variables:
        findings.append(Finding(
            status=ReconcileStatus.WARN,
            code=CODE_ORPHAN,
            detail="%r is declared but never referenced" % name,
            key=name,
        ))

    result.findings = findings
    if any(f.status == ReconcileStatus.HALT for f in findings):
        result.status = ReconcileStatus.HALT
    elif any(f.status == ReconcileStatus.WARN for f in findings):
        result.status = ReconcileStatus.WARN
    else:
        result.status = ReconcileStatus.OK

    result.message = "%s: %s (%d halt, %d warn)" % (
        contract.module_path, result.status.value,
        len(result.halts), len(result.warns))
    return result


def _compare_source_path(contract: ModuleContract,
                         template: TemplateMap) -> List[Finding]:
    """Compare the template's declared source path with the real module path."""
    declared = (template.declared_module_path or "").strip()
    actual = contract.module_path.strip()
    if not declared:
        return []
    if declared == actual:
        return []

    # Case-only differences get their own code. Do not normalise to decide the
    # outcome -- only to decide *which* error this is.
    if declared.lower() == actual.lower():
        return [Finding(
            status=ReconcileStatus.WARN,
            code=CODE_PATH_CASE_MISMATCH,
            detail="template source says %r but the module directory is %r; "
                   "identical except for case, so this resolves on Windows and "
                   "macOS and fails on Linux CI" % (declared, actual),
            key=declared,
        )]

    return [Finding(
        status=ReconcileStatus.WARN,
        code=CODE_PATH_MISMATCH,
        detail="template source says %r but the module directory is %r"
               % (declared, actual),
        key=declared,
    )]


# --------------------------------------------------------------------------
# Module <-> template pairing
# --------------------------------------------------------------------------
# Module directories are `<Category>/<Module-Name>`; templates live at
# `docs/examples/<slug>/`. The slug is lowercase and hyphenated, so pairing them
# needs a fold. This is NOT the ADR 0004 case-sensitive comparison: that one
# compares the template's declared `source` path against the real directory and
# is done above, exactly. This is only how we locate a template file.

def slug_for_module(module_path: str) -> str:
    """`Databases/Managed-Redis` -> `managed-redis`."""
    name = module_path.rsplit("/", 1)[-1]
    return re.sub(r"[_\s]+", "-", name).lower()


def pair_modules_with_templates(module_paths: List[str],
                                templates: Dict[str, str]) -> Dict[str, Optional[str]]:
    """{module_path: template_path or None}.

    The template's declared `source` path is authoritative and is tried first;
    the directory slug is only a fallback, because the two naming schemes do not
    always agree (`Backup/Recovery-Services-Vault` lives under the template slug
    `recovery-service-vault`, singular).

    A case-only difference still *locates* the template -- otherwise the three
    known casing bugs would be reported as "no template exists", which is false
    and would bury them. Locating is a lookup; the verdict stays exact, and
    `_compare_source_path` raises CODE_PATH_CASE_MISMATCH on the same pair.
    """
    from .template_map import build_template_map

    by_declared: Dict[str, str] = {}
    for slug, template_path in templates.items():
        declared = build_template_map(template_path, slug).declared_module_path
        if declared:
            by_declared.setdefault(declared, template_path)
    by_declared_folded = {k.lower(): v for k, v in by_declared.items()}

    paired: Dict[str, Optional[str]] = {}
    for module_path in module_paths:
        found = by_declared.get(module_path)
        if found is None:
            found = by_declared_folded.get(module_path.lower())
        if found is None:
            found = templates.get(slug_for_module(module_path))
        paired[module_path] = found
    return paired


def unmatched_templates(module_paths: List[str],
                        templates: Dict[str, str]) -> List[str]:
    """Template slugs that no module directory claims."""
    claimed = {slug_for_module(m) for m in module_paths}
    return sorted(s for s in templates if s not in claimed)
