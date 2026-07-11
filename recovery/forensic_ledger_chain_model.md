# Forensic Ledger Chain Model — multi-ledger evidence substrate
Source: ChatGPT recovery chats 26 + 30

**Canonical relationship:**
- F:\\Samus iteration (memory: project_samus_plane_iteration)
- [EXPANDS §6 data plane ledger schema] consistent multi-ledger forensic substrate
- [EXPANDS canonical_v1 Annex_SchemaCatalog ledger entry] adds per-domain ledgers
- [NEW] cross-ledger forensic graph (incident IDs threading multiple chains)

## Three forensic ledgers (parallel chain model)

| Ledger | Location pattern | Chain key derivation | Verified by |
|---|---|---|---|
| Integrity ledger | `<data>/scarlett/integrity_ledger.jsonl` | HKDF from IPC secret | `verify_ledger_chain()` |
| Egress ledger | `<data>/scarlett/deception/egress_ledger.jsonl` | `egress_ledger_chain:` + registry key | `verify_egress_ledger()` |
| Incident index | `<data>/scarlett/deception/incident_index.jsonl` | `incident_index_chain:` + registry key | `verify_incident_index()` |

Deletion or edit of ANY entry in ANY ledger breaks the hash chain — detectable:
- by the watchdog on next cycle
- by the API on demand

## Chain entry shape
```json
{
  "seq": <monotonic int>,
  "ts": <unix float>,
  "type": "<event_type>",
  "prev_hash": "<sha256 of prior entry || zero on genesis>",
  "payload": {...},
  "hmac": "<HMAC-SHA256(entry || prev_hash, rotating epoch key)>"
}
```

Canonical encoding: `json.dumps(sort_keys=True, separators=(",", ":"))`.

## Verification function pattern
```python
def verify_egress_ledger(path, key, max_entries=100):
    """Walk chain from tip backward; return {ok, broken_seq, reason}."""
    # reason ∈ {"json", "prev_hash", "hmac", "ok"}
```

## Live verification (not assumed)
**Critical anti-pattern fix:** `snapshot()` previously returned `ledger_chained: True` hardcoded. Replace with live `verify_egress_ledger(max_entries=100)` call. Now includes `ledger_breaks` in the response.

## Trust-state audit trail (chat 30)
`<data>/scarlett/deception/trust_audit.jsonl` — append-only, records all three state transitions:
- `registry_degraded` — when tamper detected
- `repair_mode_entered` — when operator unlocks
- `trust_restored` — when operator re-signs

**Recommended hardening:** make `trust_audit.jsonl` itself tamper-evident via same hash-chain discipline.

## Deception-layer hostile test matrix (chat 30)
| Action | Expected | Status |
|---|---|---|
| Tamper canary registry sig | Degrade → block `_save()` → block epoch rotation | Verified |
| Remove signature file | `missing_signature_file` degrade | Verified |
| Tamper honeypot registry | Same fail-closed path | Verified |
| Edit old egress ledger entry | `verify_egress_ledger()` detects `hash_tampered` | Verified |
| Truncate ledger tail | Chain break: next entry's `prev_hash` won't match | Verified |
| `/deception` API | Shows degraded posture w/ `trusted, degraded_reason, degraded_at` | Verified |
| Canary severity in degraded mode | Capped to `warning` (never `critical`) | Verified |
| `rotate_epoch()` in degraded | Returns current epoch unchanged | Verified |

## Cross-ledger correlation (next step)
Thread incident IDs across ledgers:
- integrity event references incident ID
- incident index references sealed incident file hash
- egress entry references incident ID when relevant

Result: not just chained ledgers, but a **chained evidence graph**.

## Verification-failure response policy
Broken chain MUST:
- Raise trust degradation where appropriate
- Be visible in `/status` snapshot
- Contribute to containment/lockout decisions when severe
- NOT be silently warned
