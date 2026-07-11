# Development Guide

## Purpose

Provide a repeatable workflow for setting up, changing, testing, and reviewing Samus.

## Scope

Local Python development, container development, tests, documentation, and contribution discipline.

## Intended Audience

Contributors and technical reviewers.

## Source of Truth

`pyproject.toml`, requirements files, Dockerfiles, Compose files, and test configuration.

## Environment

- Python 3.11
- Git
- Docker with Compose
- PowerShell for primary operator scripts
- Optional provider credentials for integration tests or live capabilities

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

When reproducing the tested dependency set, use `requirements.lock` and the Docker constraints strategy rather than installing unconstrained latest versions.

## Branch Discipline

The supplied snapshot is on branch `samus`, ahead of its remote, with extensive modified files. Before documentation or runtime refactors:

```powershell
git status --short --branch
git diff --stat
```

Preserve unrelated work. Prefer a dedicated branch:

```powershell
git switch -c docs/reviewer-readiness
```

Do not include generated caches, local environments, secrets, runtime data, or unrelated source changes in documentation commits.

## Architecture Rules

- Put domain logic in service modules, not route handlers.
- Keep HTTP and worker paths behaviorally aligned.
- Add shared abstractions only when more than one workcell needs them.
- Register capabilities explicitly.
- Classify external side effects for governance.
- Define idempotency before enabling retryable execution.
- Route provider requests through shared clients where available.
- Prefer deterministic execution before adding an LLM call.
- Preserve correlation IDs, metrics, and audit evidence.
- Document active, optional, dormant, and deferred status.

## Adding a Workcell

Minimum expected structure:

```text
backend/example/
    __init__.py
    app.py
    models.py
    service.py
    worker.py        # only when queue-backed execution is required
```

Also update:

- capability registry;
- gateway target routing;
- settings and `.env.example`;
- queue provisioning if applicable;
- Dockerfile and Compose;
- unit and contract tests;
- architecture and operations documentation.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Focused test:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_common_authz.py -q
```

The snapshot contains 416 test files and 4,582 directly declared test functions. Regenerate counts rather than manually updating them.

## Lint

```powershell
.\.venv\Scripts\python.exe -m ruff check backend tests
```

Formatting rules should be made explicit in CI before claiming automated formatting enforcement.

## Documentation Validation

Recommended checks:

```powershell
python -c "import yaml; yaml.safe_load(open('protocol_contract.yaml', encoding='utf-8'))"
git grep -n "Architecture_Samus.md"
git grep -n "docs/"
```

Add a Markdown link checker and YAML parser to CI.

## Dependency Changes

- Update top-level requirements intentionally.
- Regenerate the lock file using the documented lock process.
- Preserve upper bounds or constraints where supply-chain stability is required.
- Rebuild affected images.
- Run tests that exercise the changed dependency.
- Record security-sensitive upgrades in the changelog.

## Commit Strategy

Recommended documentation sequence:

1. `docs: add canonical reviewer navigation`
2. `docs: split design operations security development guides`
3. `docs: extract release history`
4. `docs: add architecture decision records`
5. `docs: add reviewer evidence and limitations`
6. `docs: repair links and validate structured files`

Keep runtime changes separate unless required to fix a broken documentation reference or test.

## Pull Request Expectations

A good PR explains:

- problem and scope;
- implementation;
- affected workcells;
- security and governance impact;
- persistence and replay implications;
- tests executed;
- documentation changed;
- deferred work.

## Definition of Done

- code path is wired, not only implemented;
- capability and authorization are declared;
- failure and retry behavior are tested;
- state ownership is clear;
- observability exists;
- documentation uses the status vocabulary;
- exact validation commands and results are recorded.
