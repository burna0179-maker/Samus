# SMMS — Global Automation Project (HustleForge Autonomous Operations Layer v1.0)
Source: ChatGPT recovery chat 38

**Canonical relationship:**
- [UMBRELLA FRAMEWORK] master organizing spec — all SMMS pods plug into this
- [EXPANDS §6 application + agents + orchestration] full-spectrum automation layer
- [PAIRS WITH] WordPress dashboard plugin (this chat) for operator-facing surface
- [PAIRS WITH] `ai_receptionist_pod.py`, `alfred_document_agent.py`, all `*_pod.py` artifacts

## Prime objective
Unified automation brain that:
- Handles all strategic + tactical workflows
- Self-corrects through data feedback
- Rebuilds corrupted processes automatically
- Operates PC ↔ Phone ↔ Cloud
- Supports pods for each specialty role
- Maintains compliance, logs, vault protocols
- Runs scheduled + event-triggered tasks without manual input

## TIER 1 — Core Control Layer (canonical mapping)
| Component | Canonical equivalent |
|---|---|
| Orchestrator | §6 orchestration plane |
| DLQ + Replay Archive | §6 orchestration DLQ classification |
| Event Router | inter-agent envelopes (§10) |
| Vault | §6 security_extended + secrets management |
| Safe Mode / Auto-Repair | §6 self_heal_extended (3-tier recovery) |
| Stability Watchtower | §6 observability + §9 self-heal coverage gate |
| Telemetry & Heatmapping | observability_extended |
| API Chat Interface | §6 application FastAPI surface |

## TIER 2 — Functional Pods (16 total)

### Personal Systems (8)
1. Financial Manager
2. Relationship Coach (Kevin Samuels-style directive tone)
3. Daily Planner
4. Goal Accountability Engine
5. Magnetic Aura Layer
6. Background Repair Advisor
7. Credit Repair Agent
8. Property Revitalization Planner

### Business Systems (8)
1. Social Media Manager
2. Public Relations Specialist
3. Automation Architect
4. Customer Support AI
5. Billing Engine
6. CRM Ops Pod
7. Market Intelligence Pod
8. UGC Scheduler
9. Reputation Monitor
10. Product Deployment Manager

(Note: the original prompt listed 10 business systems; we map 16 total slots.)

## Automation modes
| Mode | Behavior |
|---|---|
| **A. Manual Trigger** | User initiates the workflow |
| **B. Smart Assist** | SMMS recommends + drafts → user approves → SMMS executes |
| **C. Full Auto** | SMMS executes without approval — posting, routing, scheduling, monitoring, repairs, daily routines, AI receptionist, API chat |

Full Auto guardrails:
- HMAC signing
- Vault constraints
- Behavior limits
- Priority rules

## Behavior models

### Model A — Daily Cycle (10 stages)
wake → data_scan → financial_pulse → social_presence_update → inbox_sweep →
relationship_calibration → health_habit_checks → business_alerts → risk_flags → end_of_day_consolidation

### Model B — Strategic Cycle (weekly / monthly)
financial_review → life_infrastructure_rebuild → skill_advancement_path →
property_credit_upgrades → business_expansion_planning → brand_reputation_audit →
pod_rotation_load_testing → market_mapping

### Model C — Emergency / Fast-Response
immediate_signal_handling → priority_rerouting → automatic_recovery →
vault_data_verification → boundary_reinforcement

## Global automation pipeline (6 layers)
1. **Input Capture** — chat / API / SMS / Termux / PC / sensors / logs
2. **Classification** — which pod / domain / mode / priority
3. **Processing** — task planning → decomposition → execution
4. **Reinforcement** — accuracy scoring → telemetry → memory shaping
5. **Repair** — corrupted steps → auto-rewrite → safe mode if required
6. **Output** — actions / posts / schedules / written material / alerts / reports

## Integration targets
- PC (main SMMS home)
- Z Flip 5 / Termux node (mobile bridge)
- Local LLM (Ollama / LM Studio per memory)
- Offline bridge
- API chat endpoint
- WordPress website (dashboard plugin below)
- Stripe products
- CRM
- Client pods
- Future network expansion nodes

## Security / privacy layer
- Vault with role separation
- BYOK / HMAC signing
- DLQ Archive with replay logging
- Evidence logs
- API rate blocks
- Permission scopes per pod
- Safe Mode fallback for chain-error clusters

## Endpoints (WordPress plugin contract)
```
GET  /v1/health/full              — full system health
POST /v1/deploy/profile/daily_cycle      — trigger daily automation cycle
POST /v1/deploy/profile/social_suite     — spin up all social automation
POST /v1/deploy/profile/market_sweep     — market + competitor intel
POST /v1/deploy/profile/auto_repair      — self-healing routines
POST /v1/cluster/control          — cluster control plane
POST /api/chat                    — operator chat
```

## SMMS Config schema (`config/smms_config.yaml`)
```yaml
server:
  base_url: "http://127.0.0.1:8080"
  app_import_path: "src.server_app:app"
  pidfile: "logs/smms_server.pid"

deploy:
  profiles:
    social_suite:    {description: "...", endpoint: "/v1/deploy/profile/social_suite", method: "POST"}
    daily_cycle:     {description: "...", endpoint: "/v1/deploy/profile/daily_cycle", method: "POST"}
    market_sweep:    {description: "...", endpoint: "/v1/deploy/profile/market_sweep", method: "POST"}
    auto_repair:     {description: "...", endpoint: "/v1/deploy/profile/auto_repair", method: "POST"}

cluster: {endpoint: "/v1/cluster/control", method: "POST"}

auth:
  use_bearer: true
  token_env_var: "SMMS_API_TOKEN"
  header_name: "Authorization"
```

## CLI commands (`src/smms_cli.py`)
```
python -m src.smms_cli start      # start FastAPI server
python -m src.smms_cli stop       # stop server (pidfile-based)
python -m src.smms_cli status     # query /v1/health/full
python -m src.smms_cli deploy <profile>  # trigger profile endpoint
```

## WordPress plugin: `hf-smms-dashboard`
Location: `wp-content/plugins/hf-smms-dashboard/hf-smms-dashboard.php`

Features:
- Top-level "HF SMMS" menu (dashicons-admin-site-alt3)
- Settings page: SMMS Base URL + API token
- Dashboard page with 3 cards:
  1. **System Health** — Refresh button → `/v1/health/full` (AJAX)
  2. **Deployment Actions** — Run Daily Cycle / Run Social Suite buttons
  3. **Chat Test** — input + Send → `/api/chat` (direct from browser)
- Bearer token sent as `Authorization: Bearer <token>` if configured
- WP nonce-protected AJAX handlers (`hf_smms_health`, `hf_smms_deploy`)
- Inline dark-themed CSS (charcoal #111827 + teal #22d3ee accent)

## Deferred build options (chat 38 menu)
- Full ZIP package with folders + scripts
- Internal/External product sheets
- Billing automation modules
- Pod manifest v2
- PC ↔ Phone integration guide
- Operational timeline map
- Whitepaper version
- Full technical architecture diagrams
- Drag-and-drop WordPress dashboard
- Deployment command suite
