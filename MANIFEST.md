# Documentation Bundle Manifest

This bundle is a proposed replacement and addition set. It does not modify the original repository.

## Canonical files

- `README.md`
- `ARCHITECTURE.md`
- `CHANGELOG.md`
- `Architecture_Samus.md`

## Developer documentation

- `docs/README.md`
- `docs/DESIGN.md`
- `docs/OPERATIONS.md`
- `docs/SECURITY.md`
- `docs/DEVELOPMENT.md`
- `docs/PROTOCOL.md`

## Release history

- `docs/releases/README.md`
- `docs/releases/2.1.1.md`

## Architecture decisions

- `docs/adr/README.md`
- ADR-0001 through ADR-0008

## Reviewer material

- `review/README.md`
- `review/ENGINEERING_SUMMARY.md`
- `review/TESTING_EVIDENCE.md`
- `review/DESIGN_DECISIONS.md`
- `review/TRADEOFFS.md`
- `review/KNOWN_LIMITATIONS.md`
- `review/INTERVIEW_GUIDE.md`
- `review/REPOSITORY_AUDIT.md`

## Recommended application sequence

1. Preserve or commit unrelated work.
2. Create a dedicated documentation branch.
3. Compare each proposed file with current code.
4. Apply canonical files first.
5. Add reviewer and ADR material.
6. Extract the remainder of historical releases.
7. repair links.
8. Run Markdown, YAML, secret, and test validation.
