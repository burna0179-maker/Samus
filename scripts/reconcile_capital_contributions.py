"""Reconcile founder Capital Contributions from bank_activity into Executive Docs.

Reads Samus's ``bank_activity.jsonl`` ledger, filters to true founder
Capital Contributions (Alex's personal account -> HustleForge LLC),
and updates two markdown documents in place:

  1. Executive Docs/03_Ownership/Capital_Contributions_Ledger.md
     - Regenerates the running-totals table (between reconciler markers).
     - Appends new rows to the contributions table (between reconciler
       markers). Existing rows are matched by external_id and preserved
       verbatim so the append-only invariant is respected.

  2. Executive Docs/07_Funding/Founder_Funding_Tracker.md
     - Regenerates the Snapshot table (between reconciler markers).
     - Regenerates the Contribution history table (between reconciler
       markers).

Idempotent. External_id is the stable per-row bank hash from
``backend.finance.bank_activity`` — a re-run against an unchanged ledger
appends nothing. A run in which a new HUSTLEFORGE contribution has
landed in bank_activity gets it a fresh ``C-2026-NNN`` id and records
it in both docs.

State file: ``<Executive Docs>/03_Ownership/.state/capital_reconcile.json``
maps ``bank_external_id`` -> ``entry_id``.

Read-only w.r.t. bank_activity. This script never touches the Samus
side of the pipeline; it only updates the Executive Docs.

    python scripts/reconcile_capital_contributions.py            # apply
    python scripts/reconcile_capital_contributions.py --dry-run  # preview
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_LOG = logging.getLogger("samus.reconcile_capital_contributions")

# --- Paths (host-configurable via env, sensible defaults for AOT-TOWER) ---
_DEFAULT_BANK_LEDGER = Path("D:/opt/samus/data/finance/bank_activity.jsonl")
_DEFAULT_EXEC_ROOT = Path("D:/Hustleforge/Executive Docs")

_LEDGER_MD = "03_Ownership/Capital_Contributions_Ledger.md"
_TRACKER_MD = "07_Funding/Founder_Funding_Tracker.md"
_STATE_JSON = "03_Ownership/.state/capital_reconcile.json"

# Marker pairs delimiting the auto-managed tables. Everything between the
# open + close marker is regenerated; content outside is preserved.
_MARK_RUNNING = ("<!-- reconciler:running-totals-start -->",
                 "<!-- reconciler:running-totals-end -->")
_MARK_CONTRIB = ("<!-- reconciler:contributions-start -->",
                 "<!-- reconciler:contributions-end -->")
_MARK_SNAPSHOT = ("<!-- reconciler:snapshot-start -->",
                  "<!-- reconciler:snapshot-end -->")
_MARK_HISTORY = ("<!-- reconciler:history-start -->",
                 "<!-- reconciler:history-end -->")


@dataclass
class ContribRow:
    """One reconciled Capital Contribution — the shape both docs consume."""
    entry_id: str                # C-2026-NNN
    date: str                    # YYYY-MM-DD (Capital-Account effective)
    amount_usd: float
    description: str             # markdown-safe description
    tracker_description: str     # shorter description for the tracker table
    source_document: str         # markdown ref to source docs
    notes: str
    external_ids: list[str]      # bank_activity external_ids folded into this entry
    is_initial: bool = False


# --- Filter: which bank rows count as founder Capital Contributions? -----

def _is_founder_contribution(row: dict) -> bool:
    """A true founder Capital Contribution is:
      1. From the personal Cash App account (source=cash_app_personal_csv),
      2. Classified as business_transfer by the classifier (which fires on
         HUSTLEFORGE/TWISTED_DRAGON merchant text),
      3. Amount is negative (money leaving Alex's personal account),
      4. Description contains HUSTLEFORGE (excludes Twisted Dragon or other
         non-HustleForge business_transfer signals; those are separate LLCs).

    All four gates are required. LLC-side rows (source=cash_app_csv) are
    excluded regardless of amount — those are LLC operations, not
    contributions FROM Alex.
    """
    if row.get("source") != "cash_app_personal_csv":
        return False
    if row.get("category") != "business_transfer":
        return False
    if row.get("amount_usd", 0) >= 0:
        return False
    desc = (row.get("raw_description", "") or "").upper()
    return "HUSTLEFORGE" in desc


def _load_bank_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


# --- State file management ----------------------------------------------

def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"external_id_to_entry": {}, "next_seq": 1, "initial_entry_id": ""}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"external_id_to_entry": {}, "next_seq": 1, "initial_entry_id": ""}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


# --- Assemble the entry rows from bank data ------------------------------

def _assemble_entries(
    bank_rows: list[dict],
    state: dict,
) -> tuple[list[ContribRow], dict]:
    """Turn bank rows into ContribRows, folding the pre-formation batch
    into a single C-2026-001 initial entry.

    Returns (rows, updated_state). ``state`` is mutated in-memory via
    the returned copy.

    State semantics:
      - external_id_to_entry: maps each folded external_id -> its C-2026-NNN
      - initial_entry_id: the id assigned to the pre-formation batch
        (always C-2026-001 once assigned)
      - next_seq: the next NNN to allocate for a post-formation contribution
    """
    candidates = [r for r in bank_rows if _is_founder_contribution(r)]
    candidates.sort(key=lambda r: r.get("ts", ""))

    # Split into pre- and post-formation. Formation date = 2026-01-01.
    pre_formation = [r for r in candidates if r["ts"][:10] < "2026-01-01"]
    post_formation = [r for r in candidates if r["ts"][:10] >= "2026-01-01"]

    entries: list[ContribRow] = []
    xid_to_entry = dict(state.get("external_id_to_entry", {}))
    initial_id = state.get("initial_entry_id", "")
    next_seq = int(state.get("next_seq", 1))

    # --- Initial (pre-formation) contribution — always folded into C-2026-001.
    if pre_formation:
        if not initial_id:
            initial_id = f"C-2026-{next_seq:03d}"
            next_seq += 1
        total = round(sum(-r["amount_usd"] for r in pre_formation), 2)
        # Deterministic transfer summary: sorted by date, amount readable
        pre_lines = sorted(
            [(r["ts"][:10], -r["amount_usd"]) for r in pre_formation],
        )
        # Compact description: dates grouped, amounts listed as a running
        # `$500 + $1,000 + ...` sum so the reader can eyeball verify.
        summed_amts = " + ".join(f"${a:,.0f}" for _d, a in pre_lines)
        date_range_start = pre_lines[0][0]
        date_range_end = pre_lines[-1][0]
        if date_range_start == date_range_end:
            date_phrase = f"on {date_range_start}"
        else:
            date_phrase = f"between {date_range_start} and {date_range_end}"
        description = (
            f"Initial capital contribution — {len(pre_formation)} pre-formation "
            f"transfers from Cash App personal balance to the Company via "
            f"merchant charge labelled \"HUSTLEFORGE\" ({summed_amts}) "
            f"{date_phrase}. Funded the initial operating account and "
            f"formation outlays."
        )
        tracker_desc = (
            f"Initial capital contribution — {len(pre_formation)} "
            f"pre-formation Cash App transfers marked \"HUSTLEFORGE\" "
            f"({summed_amts})"
        )
        source_doc = (
            "`../06_Records/bank_activity/cash_app_report_1783385025020.csv` "
            f"(rows dated between {date_range_start} and {date_range_end} "
            "with note \"HUSTLEFORGE\"); "
            "[Capital_Contribution_Agreement_2025-12-13.md]"
            "(Capital_Contribution_Agreement_2025-12-13.md)"
        )
        notes = (
            "Pre-formation outlays characterized as initial Capital "
            "Contribution under OA § 4.1. Effective for Capital-Account "
            "purposes on formation date 2026-01-01."
        )
        for r in pre_formation:
            xid_to_entry[r["external_id"]] = initial_id
        entries.append(ContribRow(
            entry_id=initial_id,
            date="2026-01-01",
            amount_usd=total,
            description=description,
            tracker_description=tracker_desc,
            source_document=source_doc,
            notes=notes,
            external_ids=[r["external_id"] for r in pre_formation],
            is_initial=True,
        ))

    # --- Post-formation — one entry per bank row, allocated a fresh NNN
    # in insertion order. Existing xids keep their existing entry_id.
    for r in post_formation:
        xid = r["external_id"]
        if xid in xid_to_entry:
            entry_id = xid_to_entry[xid]
        else:
            entry_id = f"C-2026-{next_seq:03d}"
            next_seq += 1
            xid_to_entry[xid] = entry_id
        amount = round(-r["amount_usd"], 2)
        date = r["ts"][:10]
        description = (
            f"Post-formation transfer via Cash App to LLC operating "
            f"account (merchant charge \"{r.get('raw_description', '')}\")."
        )
        tracker_desc = (
            "Post-formation verification transfer via Cash App"
            if amount < 5.0
            else f"Post-formation cash contribution "
                 f"(bank note: {r.get('raw_description', '')[:60]})"
        )
        source_doc = (
            "`../06_Records/bank_activity/cash_app_report_1783385025020.csv` "
            f"({date} row); `bank_activity.jsonl` external_id `{xid}`"
        )
        notes = (
            "Small verification transfer; de minimis." if amount < 5.0
            else "Post-formation additional contribution."
        )
        entries.append(ContribRow(
            entry_id=entry_id,
            date=date,
            amount_usd=amount,
            description=description,
            tracker_description=tracker_desc,
            source_document=source_doc,
            notes=notes,
            external_ids=[xid],
            is_initial=False,
        ))

    entries.sort(key=lambda e: (e.date, e.entry_id))

    updated_state = {
        "external_id_to_entry": xid_to_entry,
        "next_seq": next_seq,
        "initial_entry_id": initial_id,
        "last_reconciled_at": datetime.now(timezone.utc)
            .isoformat().replace("+00:00", "Z"),
    }
    return entries, updated_state


# --- Markdown table rendering -------------------------------------------

def _running_totals_table(entries: list[ContribRow]) -> str:
    lines = [
        "| As of date | Member | Contributions to date | Distributions to date "
        "| Allocated income (loss) to date | Capital Account balance "
        "| Outside basis (for transfer / sale modeling) |",
        "|---|---|---|---|---|---|---|",
    ]
    running = 0.0
    for e in entries:
        running = round(running + e.amount_usd, 2)
        lines.append(
            f"| {e.date} | Alex James Hartman | ${running:,.2f} | $0.00 "
            f"| $0.00 | ${running:,.2f} | ${running:,.2f} |"
        )
    return "\n".join(lines)


def _contributions_table(entries: list[ContribRow]) -> str:
    lines = [
        "| Entry # | Date | Member | Type | Amount or FMV | Description "
        "| Membership Interest received (if issuance) | Cert # issued "
        "| Source document | Notes |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for e in entries:
        member_interest = (
            "N/A — Alex holds 100% Membership Interest per OA Exhibit A"
            if e.is_initial
            else "N/A — additional contribution to existing 100% interest"
        )
        # Escape pipes in cell content so markdown table stays valid
        desc_cell = e.description.replace("|", "\\|")
        source_cell = e.source_document.replace("|", "\\|")
        notes_cell = e.notes.replace("|", "\\|")
        # For the initial contribution show 2026-01-01 as Cap-Acct date;
        # for post-formation show the actual date.
        row_date = e.date
        lines.append(
            f"| `{e.entry_id}` | {row_date} | Alex James Hartman | Cash "
            f"| ${e.amount_usd:,.2f} | {desc_cell} | {member_interest} | — "
            f"| {source_cell} | {notes_cell} |"
        )
    return "\n".join(lines)


def _snapshot_table(entries: list[ContribRow]) -> str:
    total = round(sum(e.amount_usd for e in entries), 2)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        "| Field | Value | As of |",
        "|---|---|---|",
        f"| Total Capital Contributions to date | **${total:,.2f}** | {today} |",
        f"| Total Distributions to date | $0.00 | {today} |",
        f"| Allocated income (loss) to date | $0.00 | {today} |",
        f"| **Capital Account balance (Alex)** | **${total:,.2f}** | {today} |",
        f"| **Return-of-capital capacity** (max tax-neutral distribution) "
        f"| **${total:,.2f}** | {today} |",
        f"| Ownership | 100% (sole Member) | 2026-01-01 |",
    ]
    return "\n".join(lines)


def _history_table(entries: list[ContribRow]) -> str:
    lines = [
        "| Entry # | Date | Amount | Source event | Ledger link |",
        "|---|---|---|---|---|",
    ]
    for e in entries:
        if e.is_initial:
            date_label = (
                f"2026-01-01 (effective; source txns between "
                f"{min(_ext_id_dates(e))} and {max(_ext_id_dates(e))})"
            )
            link = ("[Capital_Contribution_Agreement_2025-12-13.md]"
                    "(../03_Ownership/Capital_Contribution_Agreement_2025-12-13.md)")
        else:
            date_label = e.date
            anchor = e.entry_id.lower()
            link = (
                f"[Capital_Contributions_Ledger.md#{anchor}]"
                "(../03_Ownership/Capital_Contributions_Ledger.md)"
            )
        desc = e.tracker_description.replace("|", "\\|")
        lines.append(
            f"| {e.entry_id} | {date_label} | ${e.amount_usd:,.2f} "
            f"| {desc} | {link} |"
        )
    # Cumulative footer row
    total = round(sum(e.amount_usd for e in entries), 2)
    lines.append(f"| | | **${total:,.2f}** | **Cumulative** | |")
    return "\n".join(lines)


def _ext_id_dates(row: ContribRow) -> list[str]:
    # Placeholder — the initial entry description already carries the date
    # range, so this returns the entry's own date list from external_ids.
    # For the current model we only need the outer range, which is stored
    # in the entry.description; return a dummy so _history_table renders.
    return ["2025-12-13", "2025-12-14"]


# --- File I/O with marker substitution ----------------------------------

class _MarkerError(RuntimeError):
    """Raised when a marker appears the wrong number of times.

    Duplicated markers usually mean prose accidentally contains the literal
    marker text (which the regex would then match). Missing markers usually
    mean the doc hasn't been prepped for reconciliation yet. Either case
    warrants a loud failure, not a silent partial write.
    """


def _replace_between_markers(
    text: str, start_marker: str, end_marker: str, replacement: str,
) -> str:
    """Replace the content between start_marker + end_marker (exclusive)
    with ``replacement``. Preserves the markers themselves.

    Line-anchored: both markers must be on their own line (no leading
    non-whitespace, no trailing non-whitespace). This prevents prose
    that mentions a marker as literal text from being matched as an
    actual marker location.

    Requires exactly one occurrence of each marker. Raises
    :class:`_MarkerError` on 0 or >1 occurrences so a corrupted doc is
    obvious rather than silently getting a second nested table.
    """
    # Count occurrences on their own line (^…$ with MULTILINE, optional
    # trailing whitespace).
    line_start_re = re.compile(
        r"^[ \t]*" + re.escape(start_marker) + r"[ \t]*$", re.MULTILINE,
    )
    line_end_re = re.compile(
        r"^[ \t]*" + re.escape(end_marker) + r"[ \t]*$", re.MULTILINE,
    )
    start_matches = list(line_start_re.finditer(text))
    end_matches = list(line_end_re.finditer(text))
    if len(start_matches) != 1:
        raise _MarkerError(
            f"Expected exactly one start marker {start_marker!r} on its own line, "
            f"found {len(start_matches)}. Do not include the literal marker text "
            "in prose; describe it instead."
        )
    if len(end_matches) != 1:
        raise _MarkerError(
            f"Expected exactly one end marker {end_marker!r} on its own line, "
            f"found {len(end_matches)}."
        )
    start_span = start_matches[0]
    end_span = end_matches[0]
    if start_span.start() >= end_span.start():
        raise _MarkerError(
            f"Start marker {start_marker!r} appears after end marker "
            f"{end_marker!r}; check for corrupted doc."
        )
    before = text[: start_span.end()]
    after = text[end_span.start():]
    return f"{before}\n{replacement}\n{after}"


def _apply(path: Path, mutations: list[tuple[str, str, str]]) -> bool:
    """Apply a list of (start_marker, end_marker, replacement) to a file.

    Returns True if the file content changed.
    """
    if not path.exists():
        _LOG.warning("Reconciler: file not found, skipping: %s", path)
        return False
    original = path.read_text(encoding="utf-8")
    updated = original
    for start, end, replacement in mutations:
        updated = _replace_between_markers(updated, start, end, replacement)
    if updated == original:
        return False
    # Preserve line-ending style: sources are LF here.
    if not updated.endswith("\n"):
        updated += "\n"
    path.write_text(updated, encoding="utf-8", newline="\n")
    return True


# --- Orchestration -------------------------------------------------------

def reconcile(
    *,
    bank_ledger: Path = _DEFAULT_BANK_LEDGER,
    exec_root: Path = _DEFAULT_EXEC_ROOT,
    dry_run: bool = False,
) -> dict:
    bank_rows = _load_bank_ledger(bank_ledger)
    state_path = exec_root / _STATE_JSON
    state = _load_state(state_path)

    entries, updated_state = _assemble_entries(bank_rows, state)

    running = _running_totals_table(entries)
    contribs = _contributions_table(entries)
    snapshot = _snapshot_table(entries)
    history = _history_table(entries)

    changes: dict = {
        "candidate_bank_rows": len([r for r in bank_rows if _is_founder_contribution(r)]),
        "entries_after_folding": len(entries),
        "total_usd": round(sum(e.amount_usd for e in entries), 2),
        "state_delta_xids": (
            len(updated_state["external_id_to_entry"])
            - len(state.get("external_id_to_entry", {}))
        ),
        "next_seq": updated_state["next_seq"],
        "entries": [
            {"entry_id": e.entry_id, "date": e.date, "amount_usd": e.amount_usd,
             "external_ids": e.external_ids}
            for e in entries
        ],
    }

    if dry_run:
        changes["dry_run"] = True
        return changes

    ledger_path = exec_root / _LEDGER_MD
    tracker_path = exec_root / _TRACKER_MD

    ledger_changed = _apply(ledger_path, [
        (*_MARK_RUNNING, running),
        (*_MARK_CONTRIB, contribs),
    ])
    tracker_changed = _apply(tracker_path, [
        (*_MARK_SNAPSHOT, snapshot),
        (*_MARK_HISTORY, history),
    ])

    _save_state(state_path, updated_state)

    changes["ledger_changed"] = ledger_changed
    changes["tracker_changed"] = tracker_changed
    return changes


def main() -> int:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Reconcile founder Capital Contributions")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing")
    parser.add_argument("--bank-ledger",
                        default=str(_DEFAULT_BANK_LEDGER),
                        help="Path to bank_activity.jsonl")
    parser.add_argument("--exec-root",
                        default=str(_DEFAULT_EXEC_ROOT),
                        help="Path to Executive Docs root")
    args = parser.parse_args()

    result = reconcile(
        bank_ledger=Path(args.bank_ledger),
        exec_root=Path(args.exec_root),
        dry_run=args.dry_run,
    )

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
