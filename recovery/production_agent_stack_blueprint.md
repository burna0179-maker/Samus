# Production Agent Stack Blueprint
Source: ChatGPT recovery chat 35

**Canonical relationship:**
- [SYNTHESIS] consolidates all prior Python-stack references into one deployable blueprint
- [ALIGNS WITH §2 directory layout] but generalizes for cloud-native deployment
- [EXPANDS §6 application + orchestration + observability + security_extended] all combined

## High-level architecture
```
Client (Web / API / CLI)
    ↓
API Gateway (FastAPI + Uvicorn/Gunicorn + Pydantic v2 + OAuth2/JWT)
    ↓
Agent Orchestrator (LangGraph / custom FSM — hybrid)
    ↓
┌────────────┬───────────────┬────────┬────────────┐
│ Planner    │ Tool Router   │ Memory │ Guardrails │
└────────────┴───────────────┴────────┴────────────┘
    ↓
Tool Executors (async workers — Celery / Arq)
    ↓
┌────────────┬─────────┬───────┬───────────────┐
│ Vector DB  │ SQL DB  │ Cache │ External APIs │
└────────────┴─────────┴───────┴───────────────┘
    ↓
Model Providers (OpenAI / Anthropic / Local LLM via vLLM or Ollama)
```

## Layer recommendations

### API
- FastAPI (async) + Uvicorn/Gunicorn
- Pydantic v2 schemas
- fastapi-users for OAuth2/JWT

### Orchestration
- **Hybrid:** LangGraph for deterministic flows + custom FSM for strict production paths
- Agents share: structured prompts, typed I/O, retry/fallback, budget tracking

### LLM abstraction
```python
class LLMProvider:
    async def generate(self, prompt: str, schema=None) -> dict: ...
```
Supports: OpenAI / Anthropic / Azure OpenAI / vLLM / Ollama / Together / Groq.
Features: streaming, JSON-schema enforcement, cost tracking, auto-fallback, per-role temperature.

### Tool interface
```python
class Tool:
    name: str
    description: str
    async def run(self, **kwargs) -> dict: ...
```
Categories: retrieval (vector/SQL/web/docs), execution (python sandbox/API adapters/Playwright), system (memory/cache/scheduler).
Routing: LLM zero-shot OR deterministic mapping OR hybrid confidence scoring.

### Memory architecture
- **Short-term:** Redis (conversation state, window trimming)
- **Long-term:** Qdrant (chunking + embedding pipeline + metadata)
- **Structured:** PostgreSQL (user data, workflow state, JSONB)

### Task execution
- Celery or Arq (Redis-backed)
- Durable queue + retries + DLQ
- Progress streaming via WebSockets

### Guardrails (layered)
1. Input filter (regex + embedding similarity)
2. Model moderation API
3. Output validation (JSON schema)
4. Policy engine (custom rules)
5. Tool access permissions per role
6. Rate limiting (SlowAPI)
7. Prompt-injection detection
8. Tool scope restrictions

### Observability
- **Logs:** structlog + OpenTelemetry tracing
- **Metrics:** Prometheus + Grafana
- **LLM:** Langfuse or W&B (token/latency/hallucination tracking)
- **Eval:** regression prompts + synthetic benchmarks + RAG recall tests

### Persistence
| Purpose | Tech |
|---|---|
| User data | PostgreSQL |
| Cache | Redis |
| Vector store | Qdrant |
| Object storage | S3-compatible |
| Audit logs | PostgreSQL + Blob |

### Deployment
- Docker multi-stage builds; separate containers: api / worker / scheduler / vector / redis / postgres
- Kubernetes (production) + Helm charts + HPA
- CI/CD: GitHub Actions → Docker build/push → Helm deploy

### Directory structure
```
agent_stack/
├── api/{main.py, routers/, schemas/}
├── agents/{planner.py, tool_router.py, critic.py, state_machine.py}
├── tools/{base.py, retrieval.py, python_exec.py, web.py}
├── memory/{vector.py, redis_memory.py, sql_memory.py}
├── llm/{provider.py, openai.py, local.py}
├── workers/{tasks.py, celery_app.py}
├── guardrails/policies.py
└── infra/{Dockerfile, docker-compose.yml, helm/}
```

## Minimal-viable production stack
For lean start:
- FastAPI + LangGraph + OpenAI + Qdrant + Redis + PostgreSQL + Celery + Docker Compose
- Scale to Kubernetes when traffic grows.

## Production hardening checklist
- Strict typing everywhere
- Timeouts on every external call
- Circuit breakers
- Tool sandboxing
- Deterministic agent paths for critical flows
- Replayable conversations
- Cost caps per user
- Feature flags
- Canary deployments

## Scaling strategy
- **Horizontal:** scale API pods, worker pool, vector DB replicas
- **Vertical:** dedicated LLM inference cluster (vLLM) + GPU autoscaling
- **Multi-region:** stateless API + shared vector DB + region-local Redis

## Operational checklist
- Latency under 2s for short tasks
- Tool timeout < 15s
- Retry limit = 3
- JSON-schema validation on all LLM output
- Observability dashboard live
- Automated nightly evaluation
