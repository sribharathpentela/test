# configuration_changes

> ⚠️ **Superseded in part, 2026-09-26.** This tool now lives at
> `src/da/section_c_cfg/engine/` inside `ips_agent_da`, and three things below
> are no longer true:
>
> 1. **There is no `main.py`.** It and `github_pr_flow.py` merged into
>    `run.py`. The split existed because one could write to GitHub and one
>    could not.
> 2. **Nothing here writes to a repository.** The branch/commit/PR functions
>    were removed along with the HTTP client they used, so `--execute` reports
>    a *simulated* PR carrying the full plan. Every mention below of creating a
>    branch, commit or pull request describes what was removed.
> 3. **Setup and commands** (sections 1 and 3) are superseded; see
>    `../README.md`, and `run_da.py` at the repo root for the Jira entry point.
>
> Everything else -- what each check does, the governance rules, the catalogue
> -- is still accurate. No validation or mutation behaviour changed.

A pipeline that takes a plain-language configuration-change request (`input.json`), validates it deterministically against `config_changes.json`, and — only if valid — edits the target Kubernetes/Kustomize files in a GitOps repository and opens a pull request for them.

All the code lives in this folder, organized into three layers:

```
engine/
│
├── core/                  Shared building blocks — no business logic of its own
│   ├── config.py              Environment variables, file paths, constants, small shared utilities
│   ├── exceptions.py          The error types every other file raises/catches
│   ├── github.py              Read-only GitHub access (check a file/service exists, fetch file content)
│   └── llm_hitl.py            Every LLM (Groq) call and every human-clarification (HITL) prompt
│
├── processing/             The actual validate → mutate logic
│   ├── validation.py          Steps 1-3: match the request, run every pre-check, decide VALID/INVALID/INCOMPLETE/CONFLICT
│   └── mutation.py            Step 4a: if VALID, safely edit the one field and stage it locally
│
├── workflow/               The two things you actually run
│   ├── main.py                 Validate + stage locally. Never touches the real GitHub repo.
│   └── github_pr_flow.py       Validate + stage + branch/commit/PR. Only writes to GitHub with --execute.
│
├── config_changes.json     Source of truth — what every configuration-change type requires/checks/touches
├── input.json               The request you're currently processing
└── mutation_output/         Auto-created — every run's staged edit(s) + a result.json, one folder per run
```

`config_changes.json` is the single source of truth for what each configuration-change type requires, validates, and touches. Nothing about a specific service, port, or field is hardcoded in the Python — it's all driven by that file.

## 1. Setup

**Prerequisites:** Python 3.10+, a GitHub personal access token with read/write access to the target GitOps repo, and a [Groq](https://console.groq.com/) API key (the LLM is used only for interpreting free-text human requests — never for reading repo content or deciding what to mutate).

```bash
cd configuration_changes
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file in this directory (already present in this checkout, but if you're setting up fresh):

```bash
GROQ_API_KEY=your-groq-api-key
GROQ_MODEL=openai/gpt-oss-120b        # optional, this is the default
GITHUB_TOKEN=your-github-token
GITOPS_REPO=owner/repo                # optional, defaults to the sample repo this was built against
GITOPS_REPO_BRANCH=main               # optional; omit to always use the repo's current default branch
```

`GITHUB_TOKEN` and `GROQ_API_KEY` are required — the tool exits immediately with a clear error if either is missing.

## 2. Describe the change in `config_change_modules/input.json`

```json
{
  "configuration_change": "CPU request/limit change",
  "service": "cf-mandi-ocr-formbuilder",
  "environment": "dev",
  "target_value": "300m",
  "request_or_limit": "Request"
}
```

- `configuration_change`, `service`, `environment` are always required. Everything else the matched `config_changes.json` entry needs (a desired value, "request or limit", "which field", a port identity, ...) can either be included here up front, or left out — the tool will interactively ask (HITL) for whatever's genuinely missing or ambiguous, but it will **never** re-ask for something already given, and never silently guess or overwrite an explicit value.
- `configuration_change` doesn't need to be an exact match — abbreviations, typos, and partial names ("cpu", "req", "port") are resolved deterministically and/or via the LLM against the `request` names and fields declared in `config_changes.json`; if it's genuinely ambiguous, you'll be asked to pick.
- You can point at a different input file by passing it as the first positional argument (see below); it defaults to `input.json` in this same folder.

## 3. How to run it

Both commands are run **as Python modules, from the `ips_agent_da` repo root**
— the imports are relative, so the package resolves from there and nowhere else:

```bash
python -m src.da.section_c_cfg.engine.workflow.main --verbose
python -m src.da.section_c_cfg.engine.workflow.github_pr_flow --verbose
```

The `python3 -m workflow.main` form in the rest of this section is the
pre-relocation spelling; prefix it as above.

### `python3 -m workflow.main`
Runs Steps 1–4a only: validate the request, and — if valid — compute the exact edit and stage it locally.
**Never touches the real GitHub repo in any way** (only reads from it, to check things exist and fetch current values). Nothing is committed, branched, or pushed — this is the safe option when you just want to see whether a request is even possible and what it would change.

```bash
python3 -m workflow.main --verbose
python3 -m workflow.main path/to/other_input.json --verbose
```

### `python3 -m workflow.github_pr_flow --verbose`
Runs the same validate + stage as `main.py`, and then **prints the plan** for Step 5 (the branch name, which file(s) would be committed, the PR title/body) — but does **not** create anything on GitHub. This is a dry run. Use it to review exactly what would happen before you actually do it.

### `python3 -m workflow.github_pr_flow --verbose --execute`
Same as above, but for real: creates the feature branch, commits the changed file(s) to it, and opens one pull request against the default branch. **This is the only command in this entire tool that writes anything to the real GitHub repository.** Always run the dry run first.

```bash
python3 -m workflow.github_pr_flow --verbose            # dry run — prints the plan only
python3 -m workflow.github_pr_flow --verbose --execute  # real — creates branch, commit(s), PR
python3 -m workflow.github_pr_flow other_input.json --execute
```

`--execute` is the *only* flag anywhere in this tool that writes to the real repository — every other command (including plain `github_pr_flow` without it) only reads from GitHub, or writes to your local `mutation_output/` folder.

## 4. What each file does

| File | What it does |
|---|---|
| `core/config.py` | Loads `.env`, defines constants (repo name, file paths, retry limits), and holds small shared helper functions (string/number coercion, Kubernetes quantity math, the `--verbose` trace printer). |
| `core/exceptions.py` | The 5 error types used everywhere: `ValidationToolError`, `GroqApiError`, `GitOpsAccessError`, `ConflictError`, `MutationError`. |
| `core/github.py` | Read-only GitHub access: checks whether a service/file exists in the repo, fetches file content, lists the repo's file tree. Never writes anything. |
| `core/llm_hitl.py` | Everything involving the LLM (Groq) and asking you questions (HITL). Matches your request text to a `config_changes.json` entry, asks you to clarify when it's ambiguous, resolves "which field did you mean," asks for missing values, and detects a conflict between your answer and `input.json`. The LLM can only ever pick from a fixed list of options here — it never invents a value or reads repository files. |
| `processing/validation.py` | The core validation logic (Steps 1–3). Reads `input.json` + `config_changes.json`, figures out which configuration change you mean, fetches only the exact files/fields it needs from GitHub, runs every pre-check (target exists, valid quantity, request ≤ limit, no-op check, dependent files agree with each other, etc.), and returns `VALID` / `INVALID` / `INCOMPLETE` / `CONFLICT`. |
| `processing/mutation.py` | Step 4a — only runs if `validation.py` returned `VALID`. Re-fetches the target file fresh from GitHub, double-checks the current value hasn't changed since validation, edits exactly that one field, and writes the result to `mutation_output/<timestamp>/`. Never touches the real repo. |
| `workflow/main.py` | The CLI for validate + stage-locally (see "How to run it" above). Cannot write to GitHub even if you wanted it to — that code path doesn't exist in this file. |
| `workflow/github_pr_flow.py` | Step 5 — the only file that can write to GitHub. Runs the same validate+mutate as `main.py`, and if that succeeds, creates a feature branch, commits the changed file(s), and opens one pull request. Dry-run by default; only writes for real with `--execute`. |
| `config_changes.json` | Not code — the data file defining every configuration-change type this tool knows about: what fields it needs, which repo files/paths to check, what the pre-checks are. This is what makes the tool generic instead of hardcoded per service. |
| `input.json` | Not code — the actual request being processed right now. Overwrite it (or pass a different file as an argument) per request. |
| `mutation_output/` | Not code — auto-created. Every run gets its own timestamped subfolder containing the edited file(s) plus a `result.json`. Nothing here was ever pushed anywhere. |

### What each run produces

- **`workflow.main`** — prints `VALID` / `INVALID` / `INCOMPLETE` / `CONFLICT` with the exact reasons, and — if valid — the mutation result (`COMPLETED` / `SKIPPED` / `REFUSED` / `ABORTED` / `FAILED`). Exit codes: `COMPLETED=0, SKIPPED=2, REFUSED/ABORTED/FAILED=1` (`3` on a bad/missing input file).
- **`workflow.github_pr_flow`** — on `--execute`, prints the created branch name, commit SHA(s), and PR URL. A request that touches more than one file (e.g. a Service port change that also updates its VirtualService route) is always committed together as **one** branch and **one** PR, never split up. Exit codes: `COMPLETED/DRY_RUN=0, SKIPPED=2, PARTIAL/FAILED=1`.

A non-zero exit code always means the change did **not** go through — check the printed reasons; nothing partial or silent ever happens.

## 5. Safety notes

- Every deterministic check (repo reads, YAML/JSON parsing, cross-file consistency, mutation, git operations) is plain Python + the GitHub REST API — the LLM is used only to interpret ambiguous human wording and is bounded to a small set of allowed answers per question; it never reads repository content or picks a mutation target.
- A pre-existing inconsistency between two files that are supposed to agree (e.g. a Service port and its VirtualService route) is always reported and halts the request — it is never silently "fixed" as a side effect of an unrelated change.
- Secret / sealed-secret files are refused deterministically and are never read or written in plaintext.
- Nothing is committed or pushed without `--execute` on `workflow.github_pr_flow`, and nothing is merged automatically — a human always reviews and merges the opened PR.
- `workflow.main` has no code path to GitHub writes at all — running it can never accidentally push anything, regardless of flags.
