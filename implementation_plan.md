# FinOps Sentinel — AWS Cost Optimization Agent
## My Build Spec (Local-First, Ports & Adapters, Phase-by-Phase)

**Who's building this:** Me — Boaz Leleina, entry-level backend/cloud engineer, finishing my MS in August 2026, trying to break into cloud/AI engineering roles in the US.
**My strategy:** Build and fully test every component locally (moto + LocalStack) before I touch a real AWS account. I'm staying inside the AWS Free Tier the whole time. I'm using this project to learn Docker, GitHub Actions, Terraform, and Kubernetes — one at a time, in that order, so I'm never juggling more than one new tool per phase.
**My architecture:** Ports & adapters (hexagonal). Every external technology — Slack, FastAPI, SQLite, boto3, Ollama — is a swappable adapter behind a domain-owned interface. See §3.
**LLM:** Local, via Ollama — `qwen3:30b-a3b` primary (my M5 Pro has 64GB unified memory; MoE runs fast), `qwen3:8b` fallback for CI. Advisory only — it never decides anything.

---

## 1. Why I'm Building This

Here's the problem I keep running into, and the reason this project exists: **AWS bills me for what's provisioned, not what I'm actually using.** A running EC2 instance sitting at 0% CPU costs exactly the same as one maxed out. An RDS instance with zero connections for six months still bills every hour. An EBS volume attached to nothing still bills for every GB. An Elastic IP bills the second it's not attached to a running instance. The meter runs on existence, not activity — AWS has no billing concept of "you forgot about this."

And AWS Budgets doesn't save me here either. A budget alert is just a notification when my spend crosses a threshold — the resources I forgot about keep running and billing right past it. (Budget "actions" can technically stop EC2/RDS at a threshold, but they're blunt, off by default, and rarely configured. What I'm building is a much smarter version of that idea.)

So the real problem is: **"idle" isn't a label AWS gives me — I have to infer it myself**, and the inference is different depending on the resource:

| Resource situation | How I detect idleness | Difficulty |
|---|---|---|
| Unattached EBS volume | State: `status=available` in DescribeVolumes | Trivial (Phase 1) |
| Orphaned Elastic IP | State: no association | Trivial (Phase 1) |
| Stopped EC2 (still paying for its EBS/IP) | State + stop timestamp > N days | Easy (Phase 1) |
| Old/orphaned snapshots | Age + source volume gone | Easy (Phase 1) |
| **Running-but-unused EC2/RDS** (the expensive case) | **CloudWatch metrics**: CPU <5% for 7–14d, near-zero network, `DatabaseConnections=0` sustained | Inference (Phase 4) — I never auto-remediate this one |

**What I'm actually building:** an agent that watches state and metrics, turns "this exists, nothing has touched it in 30 days, it's costing me ~$X/month" into a notification with Approve/Deny buttons, and only executes remediation (snapshot → delete) after I personally approve it. Every step gets logged to an audit trail.

**Why this is better than what AWS already gives me** (I want to explain this clearly in interviews): Trusted Advisor, Compute Optimizer, and Cost Explorer's rightsizing recommendations all *recommend* but never close the loop — no approval step, no remediation, no audit trail. Trusted Advisor's useful cost checks are also gated behind Business-tier support plans I don't pay for. What I'm building is policy I control end-to-end, with a human decision gate and an actual execution engine behind it.

**The core loop I'm implementing:**

```
[Agent scans AWS] → [Finds idle resource] → [LLM drafts explanation] → [Notification: Approve/Deny]
        ↑                                                                        │
        └── [Remediator executes (dry-run first)] ← [Service logs decision & updates state] ←┘
```

---

## 2. Architecture Overview

```
                    ┌─────────────────────────────────────────────┐
                    │                 SCAN LOOP                    │
                    │  (CLI/CronJob locally · EventBridge+Lambda   │
                    │   in production)                             │
                    └─────────────────────────────────────────────┘
                                        │
        ┌──────────────┐    ┌───────────▼───────────┐    ┌──────────────────┐
        │  AWS account │───▶│   Scanners (boto3     │───▶│  Findings store   │
        │  (LocalStack │    │   adapter)            │    │  (repo adapter:   │
        │   in dev)    │    │  rule-based detectors │    │  SQLite→Postgres) │
        └──────────────┘    └───────────┬───────────┘    └────────┬─────────┘
                              ┌─────────▼──────────┐               │
                              │  Advisor adapter   │               │
                              │  Ollama: qwen3     │               │
                              │  explains + drafts │               │
                              └─────────┬──────────┘               │
                                        │                          │
                              ┌─────────▼──────────┐     ┌─────────▼─────────┐
                              │  Notifier adapter  │     │  Inbound adapters  │
                              │  (Slack v1;        │────▶│  (FastAPI + CLI)   │
                              │  Telegram-ready)   │     │  → domain services │
                              │  [Approve] [Deny]  │     │  log decision,     │
                              └────────────────────┘     │  update state      │
                                                          └─────────┬─────────┘
                                                                    │ approved
                                                          ┌─────────▼─────────┐
                                                          │  Remediator        │
                                                          │  (cloud adapter)   │
                                                          │  dry-run default,  │
                                                          │  snapshot→delete,  │
                                                          │  audit log         │
                                                          └────────────────────┘
```

### Safety guardrails I'm not compromising on (built in Phase 1, live in the domain layer, tested forever)
1. `DRY_RUN=true` by default, everywhere. Real deletion requires the env flag AND my per-finding approval.
2. Tag-based protection: anything tagged `finops:protected=true` (configurable denylist) is never actionable, period.
3. Snapshot-before-delete for anything holding data. Deleting the snapshot itself is a separate, later, approved action.
4. Allowlist of remediable resource types — the agent can only act where I've written an explicit playbook.
5. Append-only audit log for every event (scan, notify, approve, deny, execute, failure) with timestamp, actor, ARN.
6. IAM split: read-only scanner role; remediation role scoped to only `ec2:CreateSnapshot`, `ec2:DeleteVolume`, `ec2:ReleaseAddress`, `ec2:TerminateInstances` with tag conditions. Never admin — I don't want a bug in my own code taking down something important.

These live in `domain/` — pure Python, no framework imports — so no adapter swap can ever accidentally remove them.

---

## 3. Architecture Principles — Ports & Adapters

The design rule for this whole project: **dependencies point inward.** I want every technology choice (Slack, FastAPI, SQLite, boto3, Ollama) to be swappable without touching business logic, so I'm applying the open-closed principle structurally, not just per-class.

**Four layers:**

1. **Domain (core)** — models, guardrail rules, the state machine, and use-case services (`run_scan`, `approve_finding`, `deny_finding`, `expire_stale`). Pure Python. Imports NOTHING external — no boto3, no FastAPI, no SQLAlchemy, no Slack SDK. If a file in `domain/` imports a third-party package (other than pydantic), that's a bug.

2. **Ports** — interfaces (ABCs) the domain defines, in the domain's own vocabulary, describing what it needs from the outside world:
   - `FindingsRepository` — save/query findings, record decisions, append audit events
   - `Notifier` — `notify(finding)` and `parse_callback(payload) -> Decision`
   - `Advisor` — `summarize(finding) -> str` (with a mandatory template fallback)
   - `CloudGateway` — `describe_*` for scanners, `execute(playbook, finding)` for remediation
   - `Scanner` — `scan(gateway) -> list[Finding]`, one subclass per rule
   Ports speak domain language: the Notifier port says `notify(finding)`, never `post_to_slack_webhook(json)`.

3. **Adapters** — concrete implementations of ports. Outbound: `SlackNotifier`, `SqlAlchemyRepository`, `Boto3Gateway`, `OllamaAdvisor`. Inbound (ways requests arrive — these are adapters too): FastAPI routes, the CLI. Adapters import the domain; the domain never imports an adapter. Route handlers and CLI commands are thin — they parse input, call a domain service, format output. Zero business logic in them.

4. **Composition root** — `bootstrap.py`, the ONE place that reads config and wires adapters into ports. Nothing else in the codebase knows which concrete adapter is in use.

**My universal swap recipe** (same move for every layer): write a new adapter implementing the existing port → add a config option → change one line in bootstrap. Slack→Telegram, FastAPI→Django, SQLite→Postgres, LocalStack→real AWS — all identical in shape. Nothing in domain/, no other adapter, and no test of domain logic changes.

**Testing payoff:** domain services get tested with in-memory fakes (`FakeNotifier` appends to a list, `InMemoryRepository` is a dict) — milliseconds, no Docker, no moto. moto/LocalStack only test the adapters themselves (does `Boto3Gateway` really call AWS right?). This splits my test suite into a huge fast core and a small slow edge.

**My restraint rule (so I don't over-engineer):** a port exists only where a second implementation is plausible. That's exactly five here: repository, notifier, advisor, cloud gateway, inbound. I'm NOT abstracting config loading, logging, or time — YAGNI applies to ports too. If an interviewer asks why there's no `ClockPort`, the answer is "because I'll never swap the clock, and indirection isn't free."

---

## 4. Tech Stack & Local/Prod Mapping

| Layer | Local (build & test) | Production (I deploy this last) |
|---|---|---|
| AWS | **LocalStack** (docker-compose, `localhost:4566`) for integration/demo; **moto** for adapter unit tests | Real account, Free Tier, $1 billing alarm first |
| AWS switching | Same `Boto3Gateway` adapter; single `AWS_ENDPOINT_URL` setting | unset → real AWS |
| Scheduler | CLI / K8s CronJob (kind) | EventBridge rule every 6h → Lambda |
| Inbound API | uvicorn / K8s Deployment (kind) | Lambda + Mangum + API Gateway HTTP API |
| LLM | Ollama adapter: `qwen3:30b-a3b` primary, `qwen3:8b` fallback/CI. OpenAI-compatible endpoint; model is pure config | Same (my Mac stays the LLM host) or template fallback |
| State | SQLAlchemy repository adapter on SQLite | Same adapter, Postgres URL (stretch) — connection-string swap |
| Notifier | Slack adapter (webhook v1 → Block Kit v2); port designed so a Telegram/Discord adapter is a drop-in | Same |
| Slack tunnel | cloudflared/ngrok for interactive callbacks | API Gateway URL |
| Containers | Docker + compose profiles (`dev`, `full` w/ ollama) | Image from GHCR |
| CI | GitHub Actions: ruff → mypy → pytest (fast domain tests + moto adapter tests) → LocalStack integration → build/scan/push | `terraform plan` on PR, manual apply |
| IaC | `tflocal` against LocalStack first | Terraform, S3 backend + DynamoDB lock |
| K8s | kind + Helm — **local only, for learning** | **Not deployed** — EKS control plane is ~$73/mo; a periodic scanner doesn't justify that. Documented tradeoff in my README. |

My mocking cheat-sheet: **moto = in-process mock for fast adapter unit tests** (`@mock_aws`, no network). **LocalStack = fake cloud in Docker** that the CLI, Terraform, and my running app can all point at. Later: **SAM CLI/Lambda RIE** to test Lambda packaging locally, **tflocal** to apply Terraform against LocalStack before real AWS. And for domain logic, neither — in-memory fakes only.

---

## 5. Repository Structure

The folder layout mirrors the layers directly — I should be able to point at any file and say which layer it's in:

```
finops-sentinel/               ← repo root
├── README.md                  # thesis, architecture diagram, demo GIF, design tradeoffs
├── .env.example               # every config var, commented, safe defaults (DRY_RUN=true),
│                              #   updated in the same commit as any config.py change
├── pyproject.toml             # deps + ruff + mypy + pytest config; src layout
├── docker-compose.yml         # profiles: dev (app+localstack), full (+ollama)
├── Dockerfile                 # multi-stage, non-root, healthcheck
├── .github/workflows/
│   ├── ci.yml                 # lint, typecheck, domain tests (fast), adapter tests (moto),
│   │                          #   integration (LocalStack svc), coverage gate
│   └── build.yml              # on tag: build → Trivy scan → push GHCR
├── src/finops_sentinel/       ← the Python package (src layout: everything lives inside here)
│   ├── __init__.py
│   ├── config.py              # pydantic-settings: DRY_RUN, AWS_ENDPOINT_URL, DB_URL,
│   │                          #   NOTIFIER=slack, ADVISOR=ollama, OLLAMA_MODEL, thresholds,
│   │                          #   protected tags
│   ├── bootstrap.py           # COMPOSITION ROOT: reads config, wires adapters into ports.
│   │                          #   The only file that knows which concrete adapters exist.
│   │
│   ├── domain/                # LAYER 1 — pure Python, zero external imports (pydantic only)
│   │   ├── models.py          # Resource, Finding, Decision, AuditEvent, StrEnums + TRANSITIONS
│   │   ├── rules.py           # guardrails: protected tags, allowlist, dry-run policy
│   │   └── services.py        # use-cases: run_scan(), approve_finding(), deny_finding(),
│   │                          #   expire_stale(), record_audit() — all framework-free
│   │
│   ├── ports/                 # LAYER 2 — ABCs the domain depends on
│   │   ├── repository.py      # FindingsRepository
│   │   ├── notifier.py        # Notifier: notify(finding), parse_callback(payload)->Decision
│   │   ├── advisor.py         # Advisor: summarize(finding)->str
│   │   ├── cloud.py           # CloudGateway: describe_*, execute(playbook, finding)
│   │   └── scanner.py         # Scanner ABC: scan(gateway) -> list[Finding]
│   │
│   └── adapters/              # LAYER 3 — one folder per technology
│       ├── aws/
│       │   ├── gateway.py     # Boto3Gateway (honors AWS_ENDPOINT_URL)
│       │   ├── scanners/      # ebs_unattached.py, eip_orphaned.py, ec2_stopped.py,
│       │   │                  #   ebs_old_snapshots.py, ec2_idle.py (Phase 4)
│       │   ├── playbooks.py   # snapshot_then_delete_volume, release_eip, terminate_stopped
│       │   └── pricing.py     # static price table (sources cited)
│       ├── persistence/
│       │   └── sqlalchemy_repo.py   # implements FindingsRepository; SQLite or Postgres by URL
│       ├── slack/
│       │   ├── notifier.py    # implements Notifier: Block Kit, signing-secret verification
│       │   └── payloads.py
│       ├── ollama/
│       │   ├── advisor.py     # implements Advisor: strict JSON, timeout → template fallback
│       │   └── prompts.py
│       └── inbound/           # inbound adapters — thin, no business logic
│           ├── api/           # FastAPI: main.py, routes.py
│           │                  #   GET /resources (inventory), GET /findings,
│           │                  #   POST /decisions/{id}, POST /callbacks/{channel}, GET /audit
│           └── cli.py         # sentinel scan | serve | seed | smoke-llm
├── scripts/seed_localstack.py # fake idle resources incl. one protected-tagged volume
├── tests/
│   ├── domain/                # in-memory fakes, no Docker/moto — the big fast suite
│   ├── adapters/              # moto (aws), tmp SQLite (persistence), respx (slack/ollama)
│   └── integration/           # LocalStack end-to-end through real adapters
├── terraform/                 # Phase 6
│   ├── modules/{iam,lambda_scanner,api,eventbridge,billing_alarm}/
│   └── envs/prod/
└── k8s/                       # Phase 5 — local kind only
    ├── manifests/             # cronjob, deployment, service, secret, configmap
    └── helm/finops-sentinel/
```

Notes to myself on this layout:
- The `src/` layer is the Python "src layout" convention — the package can't be imported by accident, so tests always run against the installed package (`pip install -e .`). pyproject must declare the package path accordingly.
- Import direction is enforceable: `domain/` imports nothing from `ports/` implementations or `adapters/`; `adapters/` import `domain/` and `ports/`; only `bootstrap.py` imports adapters concretely. I'll add an import-linter rule in CI so this can't silently rot.
- The callback route is `POST /callbacks/{channel}` — channel-generic on purpose. The route hands the raw payload to whatever Notifier adapter is configured via `parse_callback()`; Slack's signing-secret logic lives inside the Slack adapter, not in the route.

### Core data model (domain/models.py)

Two central entities — `Resource` (inventory: everything the scanner discovers, healthy or not) and `Finding` (a rule firing against a resource) — plus satellite tables that record what happened to each finding. Design rule: **immutable resource attributes (type, id, arn, region) live on `Resource`; mutable state gets snapshotted onto `Finding` at detection time** so I keep forensic proof of why a rule fired even after the resource changes or disappears.

```python
class FindingStatus(StrEnum):
    OPEN = "open"; NOTIFIED = "notified"; APPROVED = "approved"; DENIED = "denied"
    REMEDIATED = "remediated"; FAILED = "failed"; EXPIRED = "expired"

# The state machine lives WITH the enum, in the domain, tested with fakes:
TRANSITIONS: dict[FindingStatus, set[FindingStatus]] = {
    FindingStatus.OPEN:     {FindingStatus.NOTIFIED},
    FindingStatus.NOTIFIED: {FindingStatus.APPROVED, FindingStatus.DENIED, FindingStatus.EXPIRED},
    FindingStatus.APPROVED: {FindingStatus.REMEDIATED, FindingStatus.FAILED},
    # DENIED / REMEDIATED / FAILED / EXPIRED are terminal in v1
}

class ResourceLifecycle(StrEnum):
    ACTIVE = "active"; DELETED = "deleted"     # soft-delete only — inventory rows are never removed

class ResourceType(StrEnum):
    EBS_VOLUME = "ebs_volume"; ELASTIC_IP = "elastic_ip"
    EC2_INSTANCE = "ec2_instance"; EBS_SNAPSHOT = "ebs_snapshot"

class Resource(BaseModel):                     # INVENTORY — one row per discovered resource
    id: str                                    # surrogate UUID PK; every FK points here
    resource_id: str                           # natural key (vol-0abc...) — UNIQUE; becomes
                                               #   (account_id, resource_id) at org scale w/o touching FKs
    resource_type: ResourceType                # immutable → lives here
    resource_arn: str                          # immutable → lives here
    region: str                                # immutable → lives here
    current_tags: dict                         # MUTABLE — latest known, refreshed every scan
    lifecycle: ResourceLifecycle
    first_seen_at: datetime
    last_seen_at: datetime                     # not seen this scan → mark DELETED, never drop the row

class Finding(BaseModel):
    id: str
    resource_ref: str                          # FK → Resource.id (ON DELETE RESTRICT — findings
                                               #   and their audit trail outlive the resource)
    rule: str                                  # "ebs_unattached_30d", ... — open set: new scanners
                                               #   are new strings, never schema changes
    evidence: dict                             # SNAPSHOT at detection — raw facts that fired the rule
    tags_at_detection: dict                    # SNAPSHOT — proves why `protected` was computed
    est_monthly_cost_usd: Decimal              # NEVER float for money — Decimal / NUMERIC(12,4) in DB
    llm_summary: str | None                    # nullable — pipeline works without the LLM
    status: FindingStatus
    protected: bool
    detected_at: datetime                      # first fired
    last_seen_at: datetime                     # updated on re-scan dedupe: (resource_ref, rule) UNIQUE

class Decision(BaseModel):                     # 1:N — history, never overwritten; latest wins
    finding_id: str; actor: str
    action: Literal["approve","deny"]
    decided_at: datetime
    channel: str                               # "slack", "api", "cli", ... — open set BY DESIGN
                                               #   (closed sets I control = enums; extension points = strings)

class AuditEvent(BaseModel):                   # append-only; finding_id nullable for system events
    ts: datetime; event: str; finding_id: str | None; detail: dict
```

Satellite tables (same 1:N shape off findings): `notifications` (channel, `message_ref` — required so the adapter can edit the original message after a decision, status, sent_at) and `remediations` (playbook, dry_run, result, `snapshot_id` — the recovery path, must be durably recorded, started/finished_at). One row per attempt; retries and dry-runs are attempts too.

**Enforcement is layered, one source of truth:** the state machine (legal *transitions*) is business logic — `domain/services` consults `TRANSITIONS` and rejects illegal moves (double-clicks, replays, approving a remediated finding). The value sets (legal *strings*) are data integrity — enforced in Pydantic at the edge AND as `CHECK (status IN (...))` constraints in the repository adapter, generated from the StrEnums so Python stays the single source of truth. Plain TEXT + CHECK, deliberately NOT native Postgres `CREATE TYPE ... AS ENUM` — CHECK works identically on SQLite and Postgres and avoids PG enum migration pain. Status is a materialized column (queried constantly), the satellite tables are the proof; I'm consciously rejecting deriving status from event history — elegance not worth the per-query cost here.

**Concurrency — transitions are compare-and-swap, not check-then-write.** A Slack approve can race the expiry job; a double-click fires two approves; overlapping scans race the upsert. Checking status in Python then writing is a TOCTOU race. So every status transition executes as an atomic conditional update in the repository — `UPDATE findings SET status='approved' WHERE id=? AND status='notified'` — and the service checks rows-affected: 1 = won, 0 = someone else transitioned first → return "already decided". The domain still owns TRANSITIONS (it decides WHICH CAS to attempt); the repository executes it atomically. Non-negotiable for a system that deletes things — tested in tests/domain with a fake repo that simulates the lost race.

**Production hygiene, from the first table:**
- All datetimes timezone-aware UTC (`datetime.now(UTC)`, never naive `now()`); `TIMESTAMPTZ` on Postgres. The 72h expiry and "stopped N days" thresholds are exactly where naive datetimes silently break.
- Money is `Decimal` / `NUMERIC(12,4)`, never float — habit matters even for estimates.
- **Alembic from Phase 1**: the schema is a versioned artifact, evolved only via migration scripts — never by editing models and recreating. Known future migrations (Postgres, `account_id`) make this non-optional.
- `created_at` / `updated_at` on every table.
- Known-and-deferred (README note, zero code): `audit_events`/`findings` grow unboundedly by design — at scale they'd get monthly partitioning + a retention policy; if I ever filter INSIDE `evidence`, that's Postgres JSONB + GIN index territory. At my scale, neither applies.

**Scan is two passes:** (1) inventory upsert — every discovered resource written/refreshed in `resources` (last_seen_at, current_tags), unseen resources marked DELETED; (2) rule evaluation — scanners fire, findings written referencing resources. The scanner is the ONLY writer to inventory; users read it via `GET /resources`. Indexes: findings(status), findings(detected_at), unique(resource_ref, rule), resources unique(resource_id).

---

## 6. Phase-by-Phase Build Plan

Local-first: **Phases 0–5 are entirely local.** I don't touch a real AWS account until Phase 6. Each phase introduces at most one new infrastructure tool and ends with a "Done when" gate I verify myself before moving on.

### Phase 0 — Skeleton + fake cloud (Days 1–2) · new tool: Docker (consume)
- Init repo, pyproject (ruff/mypy/pytest, src layout), lay out the FULL folder structure above including empty `domain/ ports/ adapters/` with `__init__.py`s, plus the import-linter CI rule stub.
- `.env.example` at the repo root, created from day one and kept in lockstep with config.py forever: every variable documented with a one-line comment, safe defaults (`DRY_RUN=true`), placeholder markers for secrets (never real values). This file doubles as my future self-hosting doc — anyone else running this later configures from it.
- docker-compose with LocalStack (ec2, cloudwatch, sts).
- `seed_localstack.py`: 2 unattached EBS volumes (one tagged `finops:protected=true`), 1 orphaned EIP, 1 stopped EC2, 2 old snapshots.
- config.py + a minimal bootstrap.py.
- **Done when:** `docker compose up -d && python scripts/seed_localstack.py` then `aws --endpoint-url=http://localhost:4566 ec2 describe-volumes` shows my seeded resources, and `pip install -e . && python -c "import finops_sentinel"` works.

### Phase 1 — Domain core + ports + first AWS adapters (Days 3–7)
- `domain/`: models (Resource + Finding + StrEnums + TRANSITIONS table), rules (guardrails), and `services.run_scan()` orchestrating the TWO-PASS scan: pass 1 inventory upsert (every discovered resource → `resources`, refresh last_seen_at/current_tags, mark unseen DELETED), pass 2 rule evaluation (scanners fire → findings referencing resources). Protected-tag exclusion and dedupe live in the domain, not in adapters.
- Repository adapter enforces the value sets with CHECK constraints generated from the StrEnums; dedupe key is unique `(resource_ref, rule)`; status transitions implemented as atomic compare-and-swap updates (see §5 concurrency note). Alembic initialized here — the very first schema arrives as migration 0001, and every schema change after is a migration.
- `GET /resources` inventory endpoint arrives with the API in Phase 2; `sentinel scan` shows both inventory count and findings in Phase 1.
- `ports/`: all five ABCs (repository, notifier, advisor, cloud, scanner) — even though only some have real adapters yet. Fakes count as implementations.
- Adapters: `Boto3Gateway`, `SqlAlchemyRepository` (SQLite), scanners `ebs_unattached`, `eip_orphaned`, `ec2_stopped`; static price table with cited sources (gp3 ≈ $0.08/GB-mo, public IPv4/EIP ≈ $3.60/mo).
- Tests split by layer: `tests/domain/` with in-memory fakes (guardrails, dedupe, state machine); `tests/adapters/` with moto per scanner incl. the protected-tag case. ≥85% coverage on domain + scanners.
- `sentinel scan` CLI (inbound adapter) prints a rich table.
- **Done when:** scan of seeded LocalStack yields correct findings, protected volume excluded by DOMAIN logic (provable with a fake gateway, no AWS at all), tests green, import-linter passes.

### Phase 2 — HITL loop: services + inbound API + Slack adapter (Days 8–13)
- Domain services: `approve_finding()`, `deny_finding()`, `expire_stale()` (72h) — remediation trigger and guardrail re-check live HERE, framework-free.
- Inbound FastAPI adapter: `GET /findings?status=`, `POST /decisions/{id}`, `POST /callbacks/{channel}`, `GET /audit` — all thin wrappers over services.
- Slack adapter v1: incoming webhook summary. v2: Block Kit **Approve/Deny** buttons → `POST /callbacks/slack` → adapter verifies signing secret + parses payload into a Decision → service does the rest → adapter edits the original message ("✅ Approved by @boaz"). Local exposure via cloudflared.
- Remediation playbooks in the AWS adapter: `release_eip`, `snapshot_then_delete_volume` (create → poll → delete), `terminate_stopped_instance`. All honor DRY_RUN (log exactly what they'd do).
- Swap-proof test: run the whole approve flow in `tests/domain/` with FakeNotifier + InMemoryRepository — zero Slack, zero AWS, zero HTTP. If this passes, a Telegram adapter later can't break my business logic.
- Integration test: seed → scan → approve via real API + LocalStack → resource actually gone → audit trail complete.
- **Done when:** full loop demonstrated end-to-end against LocalStack with my real (free) Slack workspace, in both dry-run and live modes. **This is my launch-post milestone — I'm recording the demo GIF here.**

### Phase 3 — Docker authoring + GitHub Actions (Days 14–17) · new tools: Dockerfile, CI
- Multi-stage Dockerfile (slim runtime, non-root, healthcheck); compose profiles `dev`/`full`.
- `ci.yml`: ruff → mypy → import-linter → domain tests (fast) → adapter tests (moto) → integration (LocalStack service container) → coverage gate (fail <80%).
- `build.yml`: on tag, build + Trivy scan + push to GHCR.
- **Done when:** a PR triggers green CI, and a tag publishes a scanned image.

### Phase 4 — LLM Advisor adapter + metric-based idleness (Days 18–22)
- Ollama adapter implementing the Advisor port: OpenAI-compatible client, `OLLAMA_MODEL` config (primary `qwen3:30b-a3b`, fallback `qwen3:8b`), strict JSON schema, timeout/parse failure → template fallback baked into the PORT contract so the pipeline can never block on the LLM. `sentinel smoke-llm`: 10 runs of my real advisor prompt, Pydantic-validated — my gate for trying any different model.
- `ec2_idle` scanner (AWS adapter): CloudWatch CPU <5% + near-zero network over 7–14d (synthetic metrics in LocalStack/tests). Domain rule: these findings are notify-only/approval-gated — never auto-remediated (standbys and batch boxes look idle without being safe to kill).
- Right-sizing digest: 14-day metric summaries → Advisor suggests smaller/Graviton type + estimated savings, posted as an advisory-only digest (no buttons).
- Anomaly v1: rolling z-score on daily estimated spend (deterministic pandas, in domain); the Advisor only narrates it.
- **Done when:** notifications carry LLM summaries, the weekly digest posts, and killing Ollama mid-run degrades gracefully to templates (tested deliberately).

### Phase 5 — Kubernetes locally (Days 23–28) · new tool: K8s (kind)
- kind cluster; manifests: CronJob (scanner, 15m demo cadence), Deployment+Service (API), Secret (Slack), ConfigMap (thresholds), requests/limits set.
- Convert to a Helm chart (values: dry-run, thresholds, model, notifier).
- README note: "K8s locally for learning; Lambda in prod — a periodic scanner doesn't justify a $73/mo control plane."
- **Done when:** `kind create cluster && helm install` runs the whole loop against LocalStack inside the cluster. **My local build is complete and tested here — this is my deployment gate.**

### Phase 6 — Terraform + real AWS (Days 29–35) · new tool: Terraform
- **First thing I do:** billing alarm at $1 (its own module, applied before anything else).
- Rehearse every module with `tflocal` against LocalStack before pointing at real AWS.
- Modules: `iam` (scanner read-only; remediator narrowly scoped w/ tag conditions), `lambda_scanner` (EventBridge 6h), `api` (Lambda+Mangum+HTTP API). Remote state: S3 + DynamoDB lock.
- CI: `terraform plan` on PR (read-only creds; stretch: GitHub OIDC → AWS role instead of long-lived keys). Manual apply.
- Deploy with `DRY_RUN=true`; then one real approved remediation on a disposable 1GB volume I create myself.
- Free-tier landmines I'm watching: never EKS (~$73/mo), never a NAT gateway (~$32/mo — Lambda needs no VPC here), delete test snapshots (free tier is 1GB-month), HTTP API 1M req/mo free is plenty.
- **Done when:** scheduled Lambda scans the real account, Slack buttons work against the deployed API, IAM passes a least-privilege review I do myself, monthly bill ≈ $0.

### Phase 7 — Stretch (I'll pick 1–2, never letting these block me)
- **Conversational query interface (read-only)** — ask the agent questions in Slack: "what's costing me the most this month?", "why was that volume flagged?", "what did I approve last week?". The LLM translates natural language into repository queries (via a small allowlisted set of query functions — never raw SQL) and answers from real findings/inventory data. Strictly read-only, so it can't touch the guardrail surface; makes the system feel like an agent instead of a cron job with nice messages. Highly demo-able for @siliconmoran. Architecture: one new domain service (`answer_question`), the Advisor port grows a `query()` method, the Slack adapter routes app_mention events to it.
- **Second notifier adapter** (Telegram or Discord) — the cheapest possible proof that the ports design works: new adapter + one config line, zero domain changes. Great demo/reel material.
- **Checkov in CI** on terraform/ + one custom policy (deny gp2, require cost-allocation tags) — "policy-as-code" resume line.
- **Terraform plan scanning:** parse `terraform show -json plan.out`, flag over-provisioned types, comment on the PR via Actions — catches waste *before* deploy.
- **Postgres swap** — connection-string change on the existing repository adapter + alembic migration; another one-line proof of the architecture.
- **Org-scale design doc (write-up only, not code)** — see §9.

---

## 7. Working Agreement — What I'm Pasting to Opus Each Phase

> You are building FinOps Sentinel per the attached spec. Rules:
> 1. Implement ONLY the current phase. Don't scaffold future phases.
> 2. Respect the ports & adapters architecture in §3/§5 strictly: domain/ imports nothing external (pydantic only); adapters import domain, never the reverse; business logic lives in domain/services, never in route handlers, CLI commands, or adapters; only bootstrap.py wires concrete adapters. The import-linter config enforces this — keep it passing.
> 3. The §2 guardrails are mandatory with tests in the same PR: DRY_RUN default, protected-tag exclusion, snapshot-before-delete, allowlisted playbooks, append-only audit. They live in domain/, tested with in-memory fakes.
> 4. Every scanner and playbook ships with moto unit tests in the same PR; every domain service ships with fake-based tests in tests/domain/.
> 5. I'm learning Docker/Actions/K8s/Terraform through this project — explain each such decision in comments or README as you go.
> 6. Every AWS call goes through the Boto3Gateway adapter and respects AWS_ENDPOINT_URL so LocalStack and real AWS are interchangeable.
> 7. Small commits, conventional commit messages, typed functions under ~40 lines.
> 8. Every new config variable is added to `.env.example` in the same commit that introduces it, with a one-line comment explaining it and a safe default (`DRY_RUN=true`).
> 9. Finish by verifying the phase's "Done when" checklist and updating the README.
>
> Implement Phase N. Here's my current repo state: [tree + relevant files].

---

## 8. My Portfolio & Content Plan (alongside, not after)
- README leads with my §1 thesis + architecture diagram + a 30–60s demo GIF (Slack Approve → resource deleted → audit log) + a "Design decisions" section (ports & adapters and the five-ports restraint rule, K8s-local/Lambda-prod, LLM-as-advisor-only, IAM split, the gap vs. AWS-native tools).
- B-roll each phase for @siliconmoran — one reel per concept (LocalStack, Slack HITL buttons, "I swapped Slack for Telegram in one config line", kind CronJob, Terraform IAM).
- Public launch post at the end of Phase 2, once the HITL loop actually works — not waiting for the whole thing.
- Resume bullet draft: "Built a human-in-the-loop FinOps agent (Python/FastAPI/boto3, hexagonal architecture) that detects idle AWS resources via state and CloudWatch inference and executes snapshot-safe remediation through Slack approvals; fully tested locally against LocalStack with a fake-driven domain test suite, deployed serverlessly with Terraform (EventBridge→Lambda) at ~$0/mo; GitHub Actions CI, containerized, K8s CronJob validated on kind."

---

## 9. Future Direction — Org-Scale (My Design Notes, Not Build Scope)

Not part of v1 — keeping this so I don't lose the thinking, and as a README/interview talking point: "I designed for this, but deliberately scoped v1 to a single account."

**What carries forward unchanged:** the entire domain layer, all ports, and every adapter except wiring. Scanners never need to know about accounts — they run against whatever gateway session they're handed. This is the ports design paying off again: multi-account is a change to how gateways are CONSTRUCTED (bootstrap), not to any scanner or service.

**What I'd add, as three separate layers:**

1. **Account-iteration layer (IAM + bootstrap, not scanner code).**
   Each member account gets a narrow cross-account role (`FinOpsSentinelReadOnly`) trusting a central hub account, deployed org-wide via **CloudFormation StackSets** under AWS Organizations. The hub loops: `sts:AssumeRole` per account → construct a `Boto3Gateway` per session → run the same scanners. `Finding` gains an `account_id` field. Remediation role stays separate and narrower; auto-remediation only for accounts that explicitly opt in.

2. **Findings store, upgraded.**
   Same repository port; Postgres adapter with `account_id`/`org_unit` on every row, so I can query "everything across 40 accounts" or "just this OU."

3. **Notification routing, ownership-aware.**
   One channel doesn't scale past a handful of accounts — it becomes noise and gets ignored (the actual failure mode of most in-house cost tools, not a technical limit). Needs a `team:`/`owner:` cost-allocation tag convention, with notifications routed per-team. A separate aggregation/reporting layer (top waste by account, trends) sits on top, decoupled from the per-finding approval flow.

**Why not now:** I can't meaningfully test it solo (needs multiple real AWS accounts), and it would eat time better spent finishing the core loop. Revisit only if this becomes a real OSS project or a job-specific extension.