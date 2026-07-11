# Website build orders

One JSON file per customer build. The file matches
`backend.website.models.WebsiteOrder` exactly (unknown fields are rejected).

## Fields

| Field | Notes |
|---|---|
| `customer_name` | required |
| `source` | `operator` \| `cash_engine` \| `api` |
| `settlement_kind` | `barter` (work-for-debt) \| `invoice` \| `gratis` |
| `settlement_lender_id` | for `barter`: the `lender_id` in `backend/finance/liabilities.yaml` (e.g. `sample-customer`) |
| `settlement_amount_usd` | dollar value applied at settle |
| `brief.business_name` | required |
| `brief.business_description` | feeds copy + SEO |
| `brief.contact_email/phone/address` | set via Site Properties (business_info stage) |
| `brief.existing_site_id` | the Wix **metaSiteId** to adopt (skips provisioning) |
| `brief.template_id` | only if you want Samus to *create* a new site from a template |
| `brief.pages[]` | `{slug, title, content:{...}}` — the content to populate |

## Settle never edits the ledger

A `barter` settlement does **not** auto-write `liabilities.yaml` (that file is
operator-curated). It emits a settlement marker and creates an operator task
reminding you to append the `repayments[]` row for the lender yourself.

## Walk-through

Seed creds once (real console as Alex, not via `!`). `-Name` is the fixed
label below (NOT the value); the value is pasted at the masked prompt. `-Force`
is required because `-Scope Samus` is an agent scope and the host walk-through
runs as Alex — it needs an Alex-readable (Alex-DPAPI) copy.

```powershell
Import-Module D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1
Set-HfSecret -Scope Samus -Name WixApiKey -Force       # paste the API token at the masked prompt
Set-HfSecret -Scope Samus -Name WixAccountId -Force    # paste the account-id GUID at the prompt
# optional, once a Wix CMS collection exists for the chosen template:
Set-HfSecret -Scope Samus -Name WixContentCollectionId -Force
```

Then, one verb per step (supervised: each stage needs an explicit approve):

```powershell
cd D:\Hustleforge\.worktrees\samus\Samus\scripts
.\Run-WebsiteBuild.ps1 start   --order website_orders\harmony.json
.\Run-WebsiteBuild.ps1 approve --order-id <wb-id> --stage brief
.\Run-WebsiteBuild.ps1 advance --order-id <wb-id>
.\Run-WebsiteBuild.ps1 status  --order-id <wb-id>
# ...repeat approve+advance per stage. publish/deliver also need -LivePublish.
```

The stage sequence: `brief → provision → business_info → content → seo → qa →
publish → deliver → settle`. Stages that need a credential, a CMS collection
id, or the live-publish flag **park** (resumable) rather than guess.
