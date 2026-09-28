# Section C — configuration changes

Takes a plain-language config-change request, validates it deterministically
against a catalogue of **28 change types**, and — only if valid — edits the one
target field in a GitOps Kubernetes/Kustomize repo and opens a PR for it.

`engine/` arrived whole, on **2026-09-26**, from a separate build
(`HON-Config_changes-main`). It is now routed, driven by `DAPipeline`, and
read-only — see *Integrated* below. Its 3,289 lines have **not been reviewed**
and its own checks are still largely untested; what covers it today is the
seam, the state machine and the config, not `validation.py`.

## Why it was not rebuilt to match Section A

The obvious move on intake was to refactor it into `contracts / resolve / slots
/ render` so both sections look alike. That would have been renaming for its own
sake. The architectures already agree where it counts:

| Section A (IaC) | Section C (config) |
|---|---|
| module contracts built from `variables.tf` | `config_changes.json` — 28 types, their required fields, pre-checks and exact target paths |
| schema inversion; the LLM never names a variable (ADR 0007) | the LLM only picks from a fixed option list; it never reads repo content or chooses a mutation target |
| outside text is data, never instruction (ADR 0011) | repo reads, YAML parsing, cross-file checks and mutation are all plain Python |
| the closure proof — render only when every variable is resolved | `VALID / INVALID / INCOMPLETE / CONFLICT` — mutate only on `VALID` |
| HITL asks for what the ticket does not say | HITL asks for what `input.json` does not say, and never re-asks or overwrites |

What it does *not* share is Section A's test discipline — 49 tests against
Section A's 177, and none of them exercise the 28 change types. That is the gap
worth closing next.

## What changed on intake

Structure only — no behaviour was touched.

- Flattened `HON-Config_changes-main/config_change_modules/` to `engine/`. Two
  levels of nesting that carried no meaning, one of them a zip artifact whose
  hyphen made the whole tree unimportable.
- The 17 cross-package imports went from absolute (`from core.config import …`)
  to relative. It was a standalone CLI that required `cd config_change_modules`
  first; it is now an ordinary package that imports from anywhere in the repo.
- `requests` and `ruamel.yaml` merged into the root `requirements.txt`, and the
  `groq` floor raised to the higher of the two (`>=0.11.0`).

`BASE_DIR` still resolves to the folder holding `config_changes.json`, so the
data files travel with the code and no path was rewritten.

## Layout

```
engine/
├── core/            shared building blocks, no business logic
│   ├── config.py        env, paths, constants, quantity math
│   ├── exceptions.py    the 5 error types
│   ├── github.py        read-only GitHub access
│   └── llm_hitl.py      every Groq call and every human prompt
├── processing/
│   ├── validation.py    steps 1-3 — match, pre-check, decide
│   └── mutation.py      step 4a — edit one field, stage locally
├── workflow/
│   └── run.py           validate, stage, and report the PR it would have opened
└── config_changes.json  the catalogue. Nothing service-specific is in the Python.
```

Nothing else. The `.env.example`, `.gitignore` and `requirements.txt` this
folder used to carry were duplicates of the repo's own, and `input.json` was a
fixture; it now sits with the others in `src/da/samples/`. Staged edits go to
`output/config_changes/` at the repo root, not into the package.

## Running it

Normally you do not run this directly -- `run_da.py` at the repo root takes a
Jira ticket, routes it here, and drives the questions. To run the engine alone
against an `input.json`:

```bash
python -m src.da.section_c_cfg.engine.workflow.run --verbose
python -m src.da.section_c_cfg.engine.workflow.run --verbose --execute
```

`--execute` reports the simulated PR result. It is named for the flag that used
to open a real one; it no longer can.

Needs `GIT_GITOPS_REPO`, `GIT_GITOPS_PAT` and `GROQ_API_KEY` in the repo-root
`.env`; see `.env.example` there. Read-only access is sufficient.

## Integrated 2026-09-26

Section C now runs through `DAPipeline` exactly as Section A does, and the two
share one result envelope. A caller reads `status` and `classification`; it
never needs to know which section ran.

- **PR writing removed.** The six functions that could reach GitHub are gone,
  and with them this module's only HTTP client. `--execute` now returns
  `"Raised PR with these details — In development Phase"` carrying the real
  plan — branch, per-file commit messages, PR title and body. The removal is
  structural: there is no transport left to write with, so restoring the
  behaviour means restoring the transport, not flipping a flag.
- **HITL inverted.** The six blocking `input()` calls are gone, satisfying
  [ADR 0022](../../../docs/decisions/0022-da-never-prompts.md). `questions.py`
  holds the seam, `flow.py` the state machine.
- **Routed.** `classify/router.py`'s `config_changes` label now dispatches.
  Gate B is skipped for it, since Gate B validates an *IaC* request.
- **One entry point.** `main.py` and `github_pr_flow.py` merged into `run.py`.
  The split was a safety line -- one could write to GitHub, one could not --
  and removing the writes removed the reason for it. `--help` works now too;
  the old hand-rolled `sys.argv` parse read it as a filename.
- **Silent under the pipeline.** The engine's 139 `print()` calls are captured
  rather than emitted, and land on the result as `cfg_trace`. They used to
  interleave with the caller's stage report -- the caller prints stages only
  after the call returns, so the engine's output arrived first -- and because
  every round replays the run, round two reprinted round one. Section A has
  never printed from inside the pipeline; this is the cheap way to the same
  shape. The proper fix is an event seam like `questions.py`'s asker, designed
  against a real UI rather than guessed at now.
- **No tool attribution.** The commit trailer and PR footer naming the editor
  that produced the agent are gone. A PR raised against a Honeywell repository
  is a Honeywell artifact; the body now says what changed and what checked it.

### How the inversion works

Not a suspended call stack. `ConfigChangeFlow` **replays the whole run each
round** against an append-only `AnswerStore`, stopping at the first question
without an answer — which is precisely what `Step4A` does, for the reason
[ADR 0010](../../../docs/decisions/0010-validation-on-facts-store-write-path.md)
gives: no partial state for a late answer to corrupt, no ordering dependency
between rounds. That is what let the 686 lines of interpretation logic in
`engine/core/llm_hitl.py` stay untouched; only the six prompt sites changed.

Three askers share one seam. `ConsoleAsker` is the default, so the CLI still
prompts exactly as before; `SuspendingAsker` is what the pipeline installs;
`ScriptedAsker` is what tests use.

One difference from Section A worth knowing: Section C asks **one question per
round**, not a set. `validation.py` discovers what it needs only after
resolving the last thing, so a question set cannot be computed up front the way
a slot schema can.

## Known gaps

1. **Thin tests.** 55 now — 17 on the removed write path, 24 on the pipeline
   integration, 14 on configuration — against Section A's 177. They cover the
   seam, the state machine, the output capture and the config; they do not
   cover `validation.py`'s pre-checks, which is the largest untested surface
   in the repo.
2. **Duplicate clients.** `engine/core/github.py` against
   `src/da/clients/git_client.py`, and `engine/core/llm_hitl.py`'s Groq calls
   against `slots/filler.py`'s. Two GitHub clients and two Groq clients in one
   repo is real debt. Merging them is the change with the most risk and the
   least demo value, and it gets cheaper once (1) is closed. The Groq half is
   worth doing first regardless — it is the same change the Databricks
   foundation-model swap needs.
3. **The catalogue is not exercised.** `config_changes.json` declares 28
   change types and the tests drive one. Nothing proves the other 27 resolve
   to a real file and field in the GitOps repo, so the first time most of them
   run will be in front of someone.
