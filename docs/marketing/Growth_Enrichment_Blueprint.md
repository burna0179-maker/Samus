# Samus Growth Enrichment Blueprint — GEO/LEO/AIO + Prospecting + Social

**Created:** 2026-06-04
**Source intelligence:** Opinly.ai competitor distillation (3 strategy docs) cross-referenced against the live Samus codebase (`backend/`) and the Hustleforge website (`/website`, Next.js).
**Purpose:** Distill the marketing systems/sub-systems/features/schema/strategy/resources Samus *should* have, map them against what it *already* has, and define a prioritized, buildable, dormant-by-construction roadmap to fill the gaps.

> House pattern (enforced across the ecosystem): every new capability ships **dormant**, **flag-gated default-OFF**, **fail-closed**, with **tests**, and is **operator-armed**. Outward actions (sending email, posting social, publishing) stay DRY-RUN until explicitly enabled. This blueprint follows that pattern.

---

## 0. The thesis (why this matters now)

The funnel entry point has moved upstream into AI interfaces. Six numbers reframe the mission:

| Metric | Figure | Implication for Samus |
|---|---|---|
| Google AI-Mode searches ending in zero clicks | 93% | Ranking ≠ traffic |
| Organic CTR drop when AI Overviews trigger | −61% | Traditional SEO ROI eroding |
| LLM citations from first 30% of content | 44% | *Where* claims sit matters |
| Brands cited via 3rd-party vs own domain | 6.5× | Own site is the weakest citation source |
| Earned-media AI-citation lift | +239–325% | Distribution ≈ creation |
| Marketers tracking LLM citations today | 14% | First-mover window is open |

**Strategic frame:** Samus must optimize for *being the answer AI gives*, not just the link Google shows — across three layers: **GEO** (how content is structured to be extractable), **LEO** (how Samus becomes a known entity with topical authority), **AIO** (how Samus wins the answer-selection pipeline and *measures* it).

This applies on two fronts simultaneously:
1. **Samus's own visibility** (the Hustleforge company brand/website/social) — "increase visibility for the company."
2. **What Samus sells** (SEO/content/prospecting services to customers) — every gap closed here is also a richer product.

---

## 1. What Samus already has (verified in code)

A strong outbound + fulfillment engine already exists. This is the foundation we build *onto*, not over.

### Marketing / SEO product surface
- **SEO audit** (`backend/seo/`): deterministic single-page technical audit — title/meta/H1/viewport/canonical/OG/mixed-content, **schema.org detection**, alt-text/links/analytics presence, robots.txt read, PageSpeed CWV (LCP/CLS), passive TLS/DNS security audit. Evidence-sourced (Codex G6 — only verifiable findings reach the customer report).
- **Content drafts** (`backend/seo/content.py`): Haiku-generated title/meta/H1/body/CTA, budget-gated, templated fallback.
- **Monthly retainer cycle** (`backend/retainer/`): 4-step DAG (re-audit → diff → apply fixes → render+send visibility report) with a rank-movement report template.
- **SKUs** (`backend/catalog/registry.py`): `seo_audit` $149, `service_seo_implementation` $200, `retainer_seo_optimization` $300/mo.

### Prospecting / outreach / CRM
- **Prospecting** (`backend/prospecting/`): Google Places discovery → 0-100 lead scorer (industry fit, rating, review volume, **inverse SEO opportunity**) → 3-stage owner-contact enrichment cascade → callsheet generation (templated or LLM). 32-field `ProspectRecord`.
- **Signal filter** (`backend/signal_filter/`): 7-axis pre-qualification gate (≥0.62 admission).
- **Outreach** (`backend/outreach/`): email campaigns (Apollo source → SES/SendGrid), CAN-SPAM enforced, closer FSM, objection handler, **social adapter (LinkedIn + Facebook, DRY-RUN, stake-sentence gated)**, **2-day follow-up cadence**.
- **CRM** (`backend/crm/`): 7 DynamoDB entities (Prospect/Contact/Conversation/CallState/Opportunity/OperatorTask/Artifact), opportunity FSM, follow-ups-due view, **conversion funnel telemetry** (lead→prospect→opp→proposal→closed_won).
- **Voice** (`backend/voice/`): Vapi outbound dialer + inbound receptionist + operator console. Autonomous closer DORMANT (VR-G5 gated).
- **Strategy** (`backend/strategy/`): hierarchical bandit (industry→policy_family) + reward-density attribution.
- **Intake** (`backend/intake/`): public onboarding form, rate-limited + CAPTCHA.

### Content assets (customer deliverables, not Samus's own marketing)
- **Product packs** (`backend/products/packs/`): `creator_quickstart` $150, `content_funnel` $300 (TOFU/MOFU/BOFU + 7-email nurture template), `authority_accelerator` $500 (12-month editorial + PR). These are *markdown playbooks shipped to buyers*, not engines Samus runs for itself.

### Website (`/website`, Next.js)
- Landing page, **blog** (WordPress.com-backed, categories, ISR), services catalog, hardcoded testimonials, FAQ component, pricing, **OpenGraph + Twitter Card metadata**, dynamic OG image.

---

## 2. Gap matrix — what's missing, mapped to the framework

Legend: ❌ missing · ◐ partial/dormant · ✅ present

### Layer GEO — content extractability for AI
| Capability | Status | Where it should live |
|---|---|---|
| Answer-first / "golden answer" (40–60w) content structure | ❌ | `backend/seo/content.py` (generator) + `geo/` formatter |
| FAQ-section generation (5–10 Q&A, 40–60w each) | ❌ | `backend/seo/` content + schema |
| JSON-LD schema **generation** (FAQPage/Article/Organization/HowTo/Product) | ❌ (only *detects*) | `backend/seo/schema_builder.py` (new) + website |
| `llms.txt` generation | ❌ | website static route + `backend/seo/` as a deliverable |
| `robots.txt` AI-bot allow rules (GPTBot/ClaudeBot/anthropic-ai) | ❌ | website + audit recommender |
| `sitemap.xml` | ❌ | website (`sitemap.ts`) |
| Content-freshness / quarterly refresh automation | ❌ | `backend/retainer/` scheduler |

### Layer LEO — entity authority
| Capability | Status | Where |
|---|---|---|
| Content **cluster / hub-and-spoke** topical-authority generation | ❌ | `backend/seo/clusters.py` (new) |
| Consistent-NAP / entity-consistency checks | ❌ | audit recommender |
| Author-entity markup | ❌ | website + schema builder |
| E-E-A-T signal scaffolding | ◐ | content generator |

### Layer AIO — answer-selection + measurement
| Capability | Status | Where |
|---|---|---|
| **AI citation tracking** (query GPT/Perplexity/Claude on ICP Qs, count Samus citations) | ❌ | `backend/visibility/` (new workcell) |
| AI **share-of-voice** vs competitors | ❌ | `backend/visibility/` |
| AI-referral traffic analytics (GA4 segmentation: chatgpt.com/perplexity.ai/…) | ❌ | analytics doc + website |
| Comparison pages ("Samus/Hustleforge vs X") generation | ❌ | `backend/seo/clusters.py` + website |

### Prospecting pipeline depth
| Capability | Status | Where |
|---|---|---|
| Multi-touch, day-cadenced **nurture sequences** + behavioral branching | ◐ (2-day only) | `backend/outreach/sequences.py` (new) |
| **Activation/onboarding** sequence (first value in 48h) | ❌ | `backend/outreach/sequences.py` |
| **Re-engagement** on inactivity | ❌ | `backend/outreach/sequences.py` |
| Warm outbound (LinkedIn + email synced multi-touch from content) | ◐ | sequences + social |
| **Case-study generator** / social-proof aggregation | ❌ | `backend/proof/` (new) |
| **Referral / affiliate loop** (dual-sided, tolt.io-style) | ❌ | `backend/referral/` (new) + website |
| CAC-by-channel / source attribution rollup | ◐ (funnel exists, no rollup) | `backend/common/conversion_funnel.py` ext. |

### Social-media presence
| Capability | Status | Where |
|---|---|---|
| Social **scheduler / content-calendar** engine | ❌ (adapter has no scheduler/route) | `backend/social/` (new) |
| **Instagram + X** adapters (only LinkedIn+FB today) | ❌ | `backend/social/adapters/` |
| **Blog → 6-asset repurposing** generator | ❌ | `backend/social/repurpose.py` |
| Monthly-theme architecture (cluster → social calendar) | ❌ | `backend/social/calendar.py` |
| Email **newsletter / digest** automation | ◐ (voice digest only) | `backend/outreach/` |

---

## 3. Prioritized build roadmap (phased, dormant-first)

Ordered by **leverage ÷ risk**. Early phases are additive, non-outward, reversible. Outward phases (send/post/publish) ship DRY-RUN and are operator-armed last.

> **Build status — 2026-06-04: all phases A–F implemented + tested.**
> - **A** ✅ website AI-visibility — on `main` @ `5b0fe367` (`website/`)
> - **B** ✅ GEO content engine — `backend/seo/{geo_format,schema_builder}.py`
> - **C** ✅ AIO measurement — `backend/visibility/`
> - **D** ✅ social engine — `backend/social/`
> - **E** ✅ nurture sequences — `backend/outreach/sequences.py`
> - **F** ✅ proof + referral — `backend/proof/`, `backend/referral/` (website referral UI deferred)
>
> 92 unit tests across B–F (all green); A verified via `next build`. Everything is **dormant / DRY-RUN / fail-closed by construction** and **not yet wired** to HTTP `/work`, SQS, or schedulers — activation (capability registration, dispatch-policy, credentials, flags) is the operator's deliberate next step. Nothing posts, sends, or publishes live until armed.

### Phase A — Company visibility quick-wins (website, lowest risk, fastest "visibility" payoff)
Pure additive static/route files on the decoupled Next.js site. No backend, no outward sends.
1. `app/robots.ts` — allow GPTBot / ClaudeBot / anthropic-ai / PerplexityBot; sitemap ref.
2. `app/sitemap.ts` — dynamic from WordPress slugs + static routes.
3. `public/llms.txt` (or `app/llms.txt/route.ts`) — curated AI crawl map.
4. JSON-LD `<script>` injection: `Organization` (home), `Article` (blog posts), `FAQPage` (FAQ component), `BreadcrumbList`.
5. GA4 AI-referral segmentation note + config.

**Effect:** Directly "increases visibility for the company"; makes the brand machine-readable to the exact crawlers driving 2026 discovery. Deployable independently.

### Phase B — GEO content engine upgrade (improves the *product* Samus sells + its own content)
6. `backend/seo/geo_format.py` — answer-first restructurer: golden-answer block, short paras, bulleted extraction, definition blocks, FAQ section. Pure transform, unit-testable.
7. `backend/seo/schema_builder.py` — emit valid JSON-LD (FAQPage/Article/Organization/HowTo/Product) from audit/content data.
8. Wire both into `content.py` drafts and the retainer visibility deliverable (additive; templated fallback preserved).

**Effect:** Every SEO deliverable becomes AI-citable; richer `service_seo_implementation` / `retainer` product.

### Phase C — AIO measurement workcell (new, measurement-only, no outward writes)
9. `backend/visibility/` workcell: given ICP question set, query GPT/Perplexity/Claude (budget-gated via existing `llm_client` + global cap), parse for Samus/competitor mentions → **citation rate** + **share-of-voice** + cited-source-domain log. JSONL ledger + CRM artifact.
10. Weekly scheduled run + report section.

**Effect:** Puts Samus in the 14% who measure AI visibility; closes the loop that tells every other phase what's working.

### Phase D — Social content engine (extend existing adapter; DRY-RUN)
11. `backend/social/` : add **Instagram + X** adapters alongside LinkedIn/FB; unify under a `SocialPost` scheduler.
12. `backend/social/repurpose.py` — one blog post → 6 native assets (LI text, LI carousel outline, LI link post, IG carousel, IG reel script, X thread) via LLM, stake-sentence gated.
13. `backend/social/calendar.py` — monthly-theme → weekly-rhythm slot filler; persists a content calendar; emits scheduled `SocialPost`s (DRY-RUN until armed).

**Effect:** "Robustness and diversity of marketing, social media presence." Compounding blog→social flywheel.

### Phase E — Prospecting nurture depth (outward when armed; build dormant)
14. `backend/outreach/sequences.py` — declarative multi-touch sequences (welcome/nurture/onboarding/re-engagement) with day-cadence + behavioral branches; consumes CRM CallState/Conversation; DRY-RUN dispatch.
15. CAC-by-channel rollup on `conversion_funnel`.

### Phase F — Proof + referral loop (outward; last, highest-trust-required)
16. `backend/proof/` — case-study generator (structured Challenge→Tried→Used→Result→Quote) from closed_won opportunities + artifacts; social-proof aggregation page feed.
17. `backend/referral/` — dual-sided referral loop (trigger at first-result moment), share-link tracking, website referral UI. (Evaluate tolt.io vs self-hosted.)

---

## 4. Cross-cutting schema additions (sketch)

```
# GEO golden-answer block (content.py output extension)
golden_answer: str        # 40–60 words, self-contained, citable
faq: list[{q: str, a: str}]   # 5–10, each a = 40–60 words
schema_jsonld: list[dict]     # FAQPage / Article / Organization ...

# AIO citation record (backend/visibility)
CitationProbe:
  query: str
  platform: "chatgpt"|"perplexity"|"claude"|"google_ai"
  samus_cited: bool
  competitors_cited: list[str]
  cited_domains: list[str]
  ts: str
ShareOfVoice: {topic, samus_pct, competitor_pcts, sample_n, ts}

# Social calendar (backend/social)
SocialPost: {platform, format, body, link, image_ref, theme, cluster,
             pipeline_fn: educate|prove|engage|convert, scheduled_at,
             stake_sentence, status: draft|scheduled|dry_run|sent}
ContentTheme: {month, cluster, weekly_focus[]}

# Nurture (backend/outreach/sequences)
Sequence: {id, kind: welcome|nurture|onboarding|reengagement,
           touches: [{day, channel, template_id, branch_on}]}
```

---

## 5. Resources / external dependencies to provision (operator)
| Need | Resource | Status |
|---|---|---|
| AI-citation querying | existing `llm_client` + global $/day cap (no new dep) | ✅ available |
| Social scheduling | native APIs (LinkedIn/IG/X) or Buffer/Later (~$15/mo) | decide |
| Referral tracking | tolt.io (%) or self-host | decide |
| Schema validation | schema.org JSON-LD (no dep) | ✅ |
| GA4 AI-referral | GA4 property + custom channel group | operator |
| Keyword/rank data (optional deepening) | GSC API (free) > Ahrefs/SEMrush | operator |

---

## 6. Guardrails (non-negotiable, per ecosystem doctrine)
- **Outward actions DRY-RUN by default** (`SAMUS_SOCIAL_DRY_RUN`, sequence dispatch flags) — operator arms explicitly.
- **Stake-sentence gate** on all social/outreach (operator-authored, guard-validated).
- **CAN-SPAM** footer on every email (already enforced).
- **LLM global $/day cap** (already enforced) governs repurposing + citation probes.
- **Codex / VR-G5–G8** governance respected; no new privileged controllers.
- **LinkedIn/IG TOS**: automated posting is operator-accepted risk; keep DRY-RUN until Alex decides per platform.
- **Evidence-sourced** customer-facing claims only (Codex G6).

---

*This blueprint is the canonical distillation of the Opinly intelligence applied to Samus. Build phases A–F are independently shippable; each is dormant-by-construction and operator-armed.*
