# _shared

Files under `_shared/` are part of the wider **HustleForge** multi-agent
ecosystem, not this Samus subsystem. They are shared across every agent
(Samus, Anita, Darwin, Major, Hivemind) and live outside any single agent's
repository.

`_shared/` is **not included in this portfolio snapshot**. Documentation
that references it (`_shared/scripts/Hustleforge.Secrets.psm1`,
`_shared/autonomy/contract.py`, `_shared/quorum_hub/`, etc.) is describing
the operator's on-disk layout of the full ecosystem — a directory structure
outside this repository.

The scripts that reference `_shared/scripts/Hustleforge.Secrets.psm1` will
therefore not run out-of-the-box against this snapshot; substitute a
DPAPI-backed secret helper of your own, or run the stack with `.env`-based
secret loading on Linux/VM targets.
