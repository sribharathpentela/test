# ips_agent_da — the Deployment Agent

Takes a Jira ticket, works out what it is asking for, and produces the change —
Terragrunt HCL for infrastructure, or an edited Kubernetes manifest for a
configuration change. It asks a human whenever the ticket does not say, and it
never guesses.

**The agent has no write path to any repository.** Both sections read from Git
and stop at a rendered artifact plus the pull request they *would* have opened.
That is deliberate while the agent is in development, and it is structural
rather than flag-guarded: the code that could branch, commit and open a PR was
removed, along with the HTTP client it used.

## Running it

One entry point, a Jira ticket in:

```bash
python run_da.py DEVOPS-888                  # Section A - IaC
python run_da.py DEVOPS-950                  # Section C - config change
python run_da.py DEVOPS-888 --live           # real model for intake and slot filling
python run_da.py DEVOPS-888 --out artifacts/ # write the .hcl and PR body out
python run_da.py DEVOPS-950 --trace          # also show Section C's engine trace
```

**You answer the questions by default.** `--simulate` fills every one with a
made-up value; it exists for CI and should never be used to show the agent to
anyone, because the rendered unit then contains strings like
`admin_username = "simulated-admin_username"` in output that otherwise looks
ready to apply.

The ticket is a key from `src/da/samples/`, or a path to any Jira payload JSON.
The router decides which section runs; the caller never picks.

As a library — this is what a supervisor or a Databricks endpoint calls:

```python
from src.da.pipeline import DAPipeline, PipelineStatus

pipeline = DAPipeline()
result = pipeline.start(jira_payload)
while result.status is PipelineStatus.NEEDS_HITL:
    result = pipeline.submit(answers)        # see below
```

`DAPipeline` is the single integration point for both sections. Every runnable
file in the repo is a thin wrapper around it.

### Answering questions

Both sections return questions rather than blocking on stdin
([ADR 0022](docs/decisions/0022-da-never-prompts.md)), but they ask differently,
because Section A can compute the whole set up front from a slot schema and
Section C discovers what it needs as it resolves:

| | field to read | answer with |
|---|---|---|
| Section A | `result.hitl_request.questions` (a set) | `{question.path: value, ...}` |
| Section C | `result.cfg_question` (one at a time) | `{question.key: value}` |

Answer Section C by **name, not by list position** — the candidate order is
ranked with the model's help and is not stable between runs.

## Configuration

Everything environment-specific is in `.env`; see `.env.example`. Moving from
this GitHub.com mirror to Honeywell's GHES in VDI is a change to `.env` and the
tokens, never a change to code — `GIT_API_BASE_URL` carries the `/api/v3` host,
`GIT_ORG` the owner, and nothing is hardcoded with a working default.

## Layout

```
run_da.py            the entry point: a Jira ticket in, an artifact out
src/da/
  pipeline.py        both sections, one envelope, three terminal states
  config.py          every environment-specific value, in one place
  intake/            Jira parsing
  classify/          which section a ticket belongs to
  schemas/           the shared Pydantic models
  llm/               the one model client. Swapping models is this file.
  section_a_iac/     IaC
    contracts/         module contracts built from variables.tf
    resolve/           the 6-branch precedence engine, the HITL state machine
    slots/             schema inversion and LLM extraction
    render/            terragrunt.hcl and the PR body
    placement.py       where a new unit goes in the monorepo
  section_b_ms/      microservices - not built
  section_c_cfg/     config changes
    questions.py       the asker seam
    flow.py            the state machine
    engine/            the 28 change types, validation, mutation, the PR plan
  clients/           GitHub REST client (read-only)
  gap_register/      modules the catalogue does not cover
docs/                architecture and ADRs - start at docs/architecture/00-INDEX.md
output/              generated artifacts. Runs write here, never into src/.
tests/               403 passing
scripts/             offline verification; smoke_*.py need a live model
.agent/              session journal and link checker
```

## The two sections

|  | Section A — IaC | Section C — config changes |
|---|---|---|
| in | a provisioning ticket | a config-change ticket |
| driven by | module contracts read from `variables.tf` | `config_changes.json`, 28 change types |
| the LLM | never names a variable ([ADR 0007](docs/decisions/0007-schema-inversion-at-llm-boundary.md)) | only picks from a fixed option list; never reads repo content |
| proof | the closure proof — render only when every variable resolves | `VALID / INVALID / INCOMPLETE / CONFLICT` |
| out | `terragrunt.hcl` + a PR body | an edited manifest + a simulated PR |

Section B is a placeholder. Section A's placement proposal is evidence-backed
but still asks: see `section_a_iac/placement.py`.

The two sections are an aid to reading, not units that run on their own — only
the agent runs. Anything a section needs *in order to* run belongs to the
pipeline; only domain logic stays local. That is why there is one config, one
model client and one GitHub client rather than one of each per section.

One place still breaks the rule: Section C keeps its own
`engine/core/processing/workflow/` tree, left over from when it was a
standalone tool. Flattening it is the last structural item.

## Working on it

```bash
python -m pytest -q             # 403 passed, 1 skipped
python scripts/validate_phases.py   # 29 offline phase checks, exit 0/1
python .agent/check_links.py        # every relative doc link, exact case
```

`CLAUDE.md` holds the rules an agent working here must follow, and
`.agent/journal.md`'s top entry says where the work stands.
