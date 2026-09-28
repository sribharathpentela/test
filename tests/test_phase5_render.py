"""Phase 5 acceptance tests -- Step 4c, rendering the unit and the PR body."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from da.section_a_iac.contracts.module_contract import build_contract
from da.section_a_iac.contracts.platform_context import build_platform_context
from da.section_a_iac.contracts.template_map import build_template_map
from da.section_a_iac.render.hcl_writer import render_unit, to_hcl
from da.section_a_iac.render.pr_body import render_pr_body
from da.section_a_iac.resolve.engine import resolve_module
from da.section_a_iac.resolve.facts import FactsStore
from da.section_a_iac.resolve.models import FactSource

HON = os.path.join(os.path.expanduser("~"),
                   "OneDrive - KnackForge Soft Solutions Pvt Ltd",
                   "Desktop", "HON")
MODULES_REPO = os.path.join(HON, "pss_fabric_azure_modules_iac_sample")
APP_REPO = os.path.join(HON, "pss_fabric_app_iac_sample")

pytestmark = pytest.mark.skipif(
    not os.path.isdir(MODULES_REPO),
    reason="sample repos not present; see .agent/journal.md for expected paths")


@pytest.fixture(scope="module")
def redis():
    platform = build_platform_context(APP_REPO)
    contract = build_contract(os.path.join(MODULES_REPO, "Databases",
                                           "Managed-Redis"),
                              "Databases/Managed-Redis")
    template = build_template_map(
        os.path.join(MODULES_REPO, "docs", "examples", "managed-redis",
                     "terragrunt.template.hcl"), "managed-redis")
    return contract, template, platform


def _closed(redis):
    contract, template, platform = redis
    facts = FactsStore(contract)
    facts.append("resource_prefix", "subscription-redis", FactSource.JIRA,
                 evidence="Resource prefix: subscription-redis")
    facts.append("managed_redis_instances.primary.sku_name", "Balanced_B3",
                 FactSource.JIRA, evidence="SKU: Balanced_B3")
    facts.append("high_availability_enabled", True, FactSource.JIRA,
                 evidence="High Availability: true")
    proof = resolve_module(contract, template, platform, facts)
    return proof, facts


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------

def test_refuses_to_render_before_closure(redis):
    """Render only after the proof (section 8)."""
    contract, template, platform = redis
    proof = resolve_module(contract, template, platform)   # nothing answered
    result = render_unit(proof, contract, template, platform)

    assert not result.ok
    assert result.unit_hcl == ""
    assert "unanswered" in result.blocked_reason


def test_renders_once_closure_is_proven(redis):
    contract, template, platform = redis
    proof, _ = _closed(redis)
    result = render_unit(proof, contract, template, platform)
    assert result.ok
    assert result.rendered


# --------------------------------------------------------------------------
# The generated file
# --------------------------------------------------------------------------

def test_unit_matches_the_repo_conventions(redis):
    contract, template, platform = redis
    proof, _ = _closed(redis)
    hcl = render_unit(proof, contract, template, platform).unit_hcl

    assert 'include "root" {' in hcl
    assert 'include "common" {' in hcl
    assert "expose = true" in hcl
    assert "inputs = {" in hcl
    assert hcl.rstrip().endswith("}")


def test_module_ref_is_pointed_at_never_written_as_a_tag(redis):
    """ADR 0006 -- DA verifies the pinned ref; it does not choose one."""
    contract, template, platform = redis
    proof, _ = _closed(redis)
    hcl = render_unit(proof, contract, template, platform).unit_hcl

    assert "?ref=${include.common.locals.module_ref}" in hcl
    assert "v1.0.6" not in hcl, "the literal tag must not be baked in"
    assert "//Databases/Managed-Redis?" in hcl


def test_pointers_render_as_pointers_not_values(redis):
    contract, template, platform = redis
    proof, _ = _closed(redis)
    hcl = render_unit(proof, contract, template, platform).unit_hcl
    assert "location            = include.common.locals.location" in hcl
    assert "subscription_id     = include.common.locals.subscription_id" in hcl


def test_omitted_variables_emit_no_line(redis):
    """Branch 2 and Branch 6 leave the module default in charge."""
    contract, template, platform = redis
    proof, _ = _closed(redis)
    hcl = render_unit(proof, contract, template, platform).unit_hcl
    for name in ("env", "region", "owner", "provisioned_time"):
        assert ("\n  %s " % name) not in hcl, name


def test_payload_map_renders_as_a_nested_block(redis):
    contract, template, platform = redis
    proof, _ = _closed(redis)
    hcl = render_unit(proof, contract, template, platform).unit_hcl
    assert "managed_redis_instances = {" in hcl
    assert "primary = {" in hcl
    assert 'sku_name = "Balanced_B3"' in hcl


def test_group_comments_come_from_the_template(redis):
    contract, template, platform = redis
    proof, _ = _closed(redis)
    hcl = render_unit(proof, contract, template, platform).unit_hcl
    assert "# Subscription and resource placement" in hcl
    assert "# Cluster-level settings" in hcl


def test_divergent_defaults_are_commented_in_hcl_not_python(redis):
    """`module default is false`, never `module default is False`."""
    contract, template, platform = redis
    proof, _ = _closed(redis)
    hcl = render_unit(proof, contract, template, platform).unit_hcl

    assert "# module default is false" in hcl
    assert "# module default is null" in hcl
    assert "is False" not in hcl and "is None" not in hcl


# --------------------------------------------------------------------------
# HCL value formatting
# --------------------------------------------------------------------------

def test_to_hcl_keeps_null_empty_string_and_value_distinct():
    """Three states, never collapsed (section 6)."""
    assert to_hcl(None) == "null"
    assert to_hcl("") == '""'
    assert to_hcl("x") == '"x"'
    assert to_hcl(True) == "true"
    assert to_hcl(False) == "false"
    assert to_hcl({}) == "{}"
    assert to_hcl([]) == "[]"


def test_to_hcl_escapes_quotes():
    assert to_hcl('say "hi"') == '"say \\"hi\\""'


# --------------------------------------------------------------------------
# The PR body
# --------------------------------------------------------------------------

def test_pr_body_carries_the_closure_proof(redis):
    contract, template, platform = redis
    proof, facts = _closed(redis)
    render = render_unit(proof, contract, template, platform)
    body = render_pr_body(proof, contract, render, facts,
                          ticket_key="DEVOPS-777")

    assert "### Provenance" in body
    for name in proof.resolutions:
        assert "`%s`" % name in body, name


def test_pr_body_surfaces_the_three_divergences(redis):
    contract, template, platform = redis
    proof, facts = _closed(redis)
    render = render_unit(proof, contract, template, platform)
    body = render_pr_body(proof, contract, render, facts)

    assert "### Values that differ from the module default" in body
    assert "| `persistence_rdb_backup_frequency` | `\"12h\"` | `null` |" in body


def test_pr_body_asks_for_confirmation_with_the_quote(redis):
    contract, template, platform = redis
    proof, facts = _closed(redis)
    render = render_unit(proof, contract, template, platform)
    body = render_pr_body(proof, contract, render, facts)

    assert "please confirm" in body
    assert "Resource prefix: subscription-redis" in body


def test_pr_body_tables_are_not_broken_by_block_values(redis):
    """A rendered block contains newlines and would end the row early."""
    contract, template, platform = redis
    proof, facts = _closed(redis)
    render = render_unit(proof, contract, template, platform)
    body = render_pr_body(proof, contract, render, facts)

    for line in body.splitlines():
        if line.startswith("|"):
            assert line.endswith("|"), line
    assert "*(block -- see the file)*" in body


def test_pr_body_is_ascii(redis):
    """It passes through a lot of tooling on the way to GitHub."""
    contract, template, platform = redis
    proof, facts = _closed(redis)
    render = render_unit(proof, contract, template, platform)
    body = render_pr_body(proof, contract, render, facts)
    body.encode("ascii")


# --------------------------------------------------------------------------
# Comment preservation (deferred item X5)
# --------------------------------------------------------------------------

def test_standalone_template_prose_is_kept(redis):
    """A comment that explains an *absence* must survive into the unit.

    The template says "Tags inherited from common.hcl (app/owner/...)" and puts
    no tags line under it. Dropping the comment leaves a reviewer wondering
    where the tags went.
    """
    contract, template, platform = redis
    assert any("Tags inherited" in text
               for _, text in template.standalone_comments)

    proof, _ = _closed(redis)
    hcl = render_unit(proof, contract, template, platform).unit_hcl
    assert "# Tags inherited from common.hcl" in hcl


def test_blueprint_braces_are_not_mistaken_for_headings(redis):
    """`#   }` closing a commented blueprint is not a section comment."""
    _, template, _ = redis
    for _, text in template.standalone_comments:
        assert any(c.isalpha() for c in text), text


def test_template_inline_guidance_is_carried_over(redis):
    contract, template, platform = redis
    proof, _ = _closed(redis)
    hcl = render_unit(proof, contract, template, platform).unit_hcl
    assert "# set true only if public access is required" in hcl


def test_stale_inline_guidance_is_dropped(redis):
    """`# REQUIRED, e.g. "wfi-core"` describes a state that no longer holds."""
    contract, template, platform = redis
    proof, _ = _closed(redis)
    hcl = render_unit(proof, contract, template, platform).unit_hcl
    assert "REQUIRED" not in hcl
    assert 'resource_prefix = "subscription-redis"' in hcl


def test_every_live_template_key_is_accounted_for(redis):
    """No live template key may vanish without a reason.

    Either it renders, or its resolution says why not (auto-injected, module
    default, or still awaiting an answer).
    """
    from da.section_a_iac.contracts.models import EntryState
    contract, template, platform = redis
    proof, _ = _closed(redis)
    result = render_unit(proof, contract, template, platform)

    for key, entry in template.entries.items():
        if entry.state is EntryState.COMMENTED:
            continue                       # commented-only -> OMIT by design
        resolution = proof.resolutions.get(key)
        assert resolution is not None, key
        if key in result.rendered:
            continue
        assert int(resolution.branch) in (2, 6) or resolution.action.value == "ASK", (
            "%s disappeared with no reason: %s" % (key, resolution.action))
