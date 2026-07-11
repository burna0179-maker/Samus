# Testing Evidence

## Snapshot Inventory

Measured from the supplied repository archive:

| Metric | Count |
|---|---:|
| `tests/test_*.py` files | 416 |
| Direct `def test_` / `async def test_` declarations | 4,582 |
| Python files excluding caches | 4,560 |

These values measure repository structure. They are not a substitute for a successful test run.

## Evidence Categories

The test tree includes coverage for:

- shared application bootstrap;
- authentication and authorization;
- queue and worker behavior;
- persistence and ledger adapters;
- CRM lifecycle;
- prospecting and scoring;
- outreach and buying signals;
- voice and call processing;
- finance and webhooks;
- strategy and control loops;
- governance and codex behavior;
- security hardening;
- end-to-end contracts.

## Recommended Published Evidence

Add CI that records:

```bash
pytest -q --junitxml=artifacts/junit.xml
ruff check backend tests
python scripts/generate_repo_metrics.py
```

Optional:

- branch and line coverage;
- dependency audit;
- container build;
- Markdown link validation;
- YAML parsing;
- secret scan;
- image vulnerability scan.

## Integrity Rules

- Never claim a suite passed unless the exact commit was tested.
- Record excluded tests and reasons.
- Distinguish unit tests from live-provider integration tests.
- Treat historical release test counts as historical.
- Avoid badges that point to disabled or non-blocking workflows.
