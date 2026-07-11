# ADR-020 (DRAFT) — Internal tax-categorization + strategic tax-minimization engine

> **Status: RESOLVED — promoted to ADR-020 in 08_decisions_log.md.**

**Date:** 2026-07-07 (manual)
**Authored by:** Claude (Opus 4.7), at operator request
**Scope:** `backend/finance/`, `backend/cognitive/`, `backend/morning.py`
**Correlation ID:** (none — not rule-triggered)

## Operator inputs (answered 2026-07-10)

| Question | Answer | Source |
|---|---|---|
| 1. Federal tax classification | **Disregarded entity** — single-member LLC default, Schedule C on personal 1040. No Form 8832 or 2553 filed. IRS Letter 147C confirms "SOLE MBR". | `Executive Docs/04_Tax/Tax_Documentation_Overview.md` |
| 2. Federal filing status | **Head of Household** | Operator (2026-07-10) |
| 3. Home-office square footage | **300 sqft** — simplified method ($5/sqft = $1,500/yr max). Master bedroom/closet at 2290 Cheim Blvd, Marysville CA 95901. | Operator (2026-07-10) |
| 4. 1099 contractor payments | **None in 2026** — clean baseline confirmed. | `Executive Docs/04_Tax/Tax_Documentation_Overview.md`, `Executive Docs/06_Records/Debt_and_Liabilities_Summary.md` |
| 5. Scope | **Federal + CA only** — LLC formed in CA, principal office in Marysville CA, no other state nexus. | `Executive Docs/01_Formation/Articles_of_Organization.md` |

## Resolution

**Decision:** ALLOW
**Operator:** alex
**Resolved at:** 2026-07-10T00:00:00+00:00
**Rationale:** ALLOW — promoted to ADR-020 in 08_decisions_log.md. All 5 required operator inputs answered (see table above). The tax-categorization engine design is adopted as proposed in the original draft. Implementation phased per the design sections below. Key entity facts confirmed: disregarded entity (no S-corp election), Head of Household filing status, 300 sqft home-office (simplified method, $1,500/yr), no 1099 contractors, federal + CA scope only.

---

## Design (retained from original draft)

### 1. Tax categorization (extends `backend/finance/`)

- **`BankTransaction.tax_category: str = ""`** and **`CodbItem.tax_category: str = ""`** — new fields, backward compatible.
- **`backend/finance/tax_rules_<year>.yaml`** — versioned, per-tax-year ruleset. Maps vendor substrings / CODB categories to Schedule C lines.
- **`backend/finance/tax_categorizer.py`** — pure logic, zero I/O. Every `TaxCategoryMatch` carries `{category_id, matched_rule, confidence, evidence}`. Unmatched items surface as operator-reviewable, never silently guessed.

### 2. Tax-break discovery reasoner (mirrors `codb_reasoner.py`)

**`backend/cognitive/tax_reasoner.py`** — RECOMMEND-ONLY via `GuidanceLedger`. Discovers from codified thresholds:
- Home-office deduction: simplified method, 300 sqft × $5 = $1,500/yr
- QBI 20% pass-through deduction estimate against net LLC income
- Self-employed health insurance deduction (only if premium tracked in CODB)
- Retirement-contribution headroom (SEP-IRA / Solo 401k) — suppressed when runway < threshold

### 3. Estimated tax liability projection

**`backend/finance/estimated_tax.py`** — pure math, zero I/O:
- Self-employment tax: 15.3% on net SE income up to SS wage base, 2.9%+ beyond
- Federal income tax: Head of Household brackets
- CA franchise tax: $800/yr minimum + graduated LLC fee tiers on gross receipts
- Quarterly due dates (Apr 15 / Jun 15 / Sep 15 / Jan 15) with running liability

### 4. Strategic spending/saving/disbursement timing

Extends tax reasoner RECOMMEND-ONLY surface. Cross-checked against `get_runway()`.

### 5. Morning-brief integration

New `TAX` section in `backend/morning.py`: running estimated liability, next due date, open recommendations, categorization coverage.

### 6. Deliberately NOT built (v1)

- No auto-filing with IRS/FTB
- No auto-execution of disbursements
- No aggressive/gray-area positions
- No multi-entity or multi-state beyond CA
- No S-corp election modeling (entity stays disregarded)
