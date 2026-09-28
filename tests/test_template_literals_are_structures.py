"""A template literal renders as the structure hcl2 parsed, never as its text.

[ADR 0005](../docs/decisions/0005-parse-hcl-with-hcl2.md) says structure comes
from hcl2 and the raw pass is comments and line numbers only. The raw pass
records a **single line**, so a live `{}` arrives there as the *string* `"{}"`
and `null` as `"null"`.

Branch 3's `EntryState.BOTH` path used `raw_value` as a value. It was the only
place in the branch that did, and it put a quoted string into a module
declaring `map(object({...}))` — HCL Terraform cannot convert, so the generated
unit did not apply. 22 entries across 9 of the 27 templates reached it.

The suite had 403 tests and none of them caught it, because every existing
assertion checked which *branch* fired and what was *disclosed*, not the type
of the value that came out. These check the type.

Offline: templates are read from the materialised cache.
"""

import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(ROOT))

from src.da.section_a_iac.contracts.models import EntryState
from src.da.section_a_iac.contracts.template_map import (build_template_map,
                                                         discover_templates)
from src.da.section_a_iac.render.hcl_writer import to_hcl

REPO = os.path.join(ROOT, ".cache", "repos",
                    "pss_fabric_azure_modules_iac_sample", "main")

pytestmark = pytest.mark.skipif(
    not os.path.isdir(REPO),
    reason="modules repo not materialised; run the pipeline once to populate "
           ".cache/repos (ADR 0020)")


def structural_entries():
    """Every template entry hcl2 parsed as something other than a string."""
    found = []
    for slug, path in sorted(discover_templates(REPO).items()):
        template = build_template_map(path, slug)
        for key, entry in template.entries.items():
            if entry.has_parsed_value and not isinstance(entry.parsed_value, str):
                found.append((slug, key, entry))
    return found


def ids(rows):
    return ["%s:%s" % (slug, key) for slug, key, _ in rows]


ROWS = structural_entries()


def test_the_templates_are_reachable():
    """If this fails the rest are skipped for the wrong reason."""
    assert ROWS, "no structural template entries found at all"


@pytest.mark.parametrize("slug,key,entry", ROWS, ids=ids(ROWS))
def test_a_parsed_structure_never_renders_as_a_quoted_string(slug, key, entry):
    rendered = to_hcl(entry.parsed_value)
    assert not rendered.startswith('"'), (
        "%s.%s renders as %s -- a string where the module declares a structure"
        % (slug, key, rendered))


@pytest.mark.parametrize("slug,key,entry", ROWS, ids=ids(ROWS))
def test_the_raw_pass_is_never_the_value(slug, key, entry):
    """The raw text and the parsed structure are different things. Where they
    render differently, the parsed one is the correct one."""
    if to_hcl(entry.raw_value) != to_hcl(entry.parsed_value):
        assert to_hcl(entry.parsed_value) == _expected(entry.parsed_value)


def _expected(value):
    if value is None:
        return "null"
    if value == {}:
        return "{}"
    if value == []:
        return "[]"
    return to_hcl(value)


# --- the specific case that surfaced it ---------------------------------

def test_an_empty_map_beside_a_blueprint_resolves_to_an_empty_map():
    """`postgresql_flexible_servers = {}` with commented lines showing what
    could go in it. `EntryState.BOTH` fires because both forms are present, but
    the live value is an empty map and that is what must reach the renderer.

    The module declares `type = map(object({...}))` with `default = {}`, so the
    string `"{}"` is not a value it can take.
    """
    from src.da.section_a_iac.contracts.template_map import build_template_map

    path = discover_templates(REPO)["postgresql-flexible-server"]
    entry = build_template_map(path, "postgresql-flexible-server") \
        .entries["postgresql_flexible_servers"]

    assert entry.state is EntryState.BOTH
    assert entry.raw_value == "{}"          # the raw pass, a string
    assert entry.parsed_value == {}         # hcl2, a dict
    assert to_hcl(entry.parsed_value) == "{}"
    assert to_hcl(entry.raw_value) == '"{}"'   # what used to be rendered


def test_a_null_beside_a_blueprint_resolves_to_null():
    """The same defect with a different wrong type: `"null"` is a string, and
    `null` is the absence of a value. `01-algorithm.md` section 6 keeps those
    three states distinct on purpose."""
    path = discover_templates(REPO)["stream-analytics-job"]
    entry = build_template_map(path, "stream-analytics-job") \
        .entries["input_eh_config"]

    assert entry.parsed_value is None
    assert to_hcl(entry.parsed_value) == "null"
    assert to_hcl(entry.raw_value) == '"null"'


# --- the branch itself, not just the renderer ---------------------------

def _resolve_both(parsed, raw):
    """Drive Branch 3's `EntryState.BOTH` path directly."""
    from src.da.section_a_iac.contracts.models import (PlatformContext,
                                                       TemplateEntry,
                                                       VariableSpec)
    from src.da.section_a_iac.resolve.branches import _from_template

    entry = TemplateEntry(
        key="servers", state=EntryState.BOTH, raw_value=raw,
        parsed_value=parsed, has_parsed_value=True, line=27,
        blueprint_text="#   primary = { sku_name = ... }")
    spec = VariableSpec(name="servers",
                        type_expr="map(object({sku_name = string}))",
                        has_default=True, default={})
    return _from_template(spec, entry, PlatformContext())


@pytest.mark.parametrize("parsed,raw", [
    ({}, "{}"),
    ([], "[]"),
    (None, "null"),
    ({"primary": {"sku_name": "GP_Standard_D2s_v3"}}, "{"),
])
def test_the_branch_carries_the_structure_not_the_raw_text(parsed, raw):
    """The regression proper.

    Before the fix this returned `raw`, so the Resolution carried a string and
    the renderer quoted it. The last case is the multi-line one the raw pass
    truncates to `{` -- the same defect that produced `network_profile = "{"`
    and was supposed to have been closed by ADR 0005's note.
    """
    resolution = _resolve_both(parsed, raw)
    assert resolution.value == parsed
    assert resolution.value != raw
    assert to_hcl(resolution.value) != to_hcl(raw)


def test_the_branch_still_falls_back_when_hcl2_parsed_nothing():
    """`has_parsed_value=False` is a real state -- a value hcl2 could not read.
    Coercing the raw text is the only thing left, and it must still happen."""
    from src.da.section_a_iac.contracts.models import (PlatformContext,
                                                       TemplateEntry,
                                                       VariableSpec)
    from src.da.section_a_iac.resolve.branches import _from_template

    entry = TemplateEntry(key="n", state=EntryState.BOTH, raw_value="3",
                          has_parsed_value=False, line=9)
    resolution = _from_template(VariableSpec(name="n", type_expr="number"),
                                entry, PlatformContext())
    assert resolution.value == 3
