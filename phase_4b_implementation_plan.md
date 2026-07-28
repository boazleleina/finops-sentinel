# Phase 4 (Part B) — Implementation Plan

**Branch:** `feat/phase-4-part-b` (off `feat/phase-4-part-a` / `main`)
**Spec source:** `implementation_plan.md` §6 "Phase 4 — LLM Advisor adapter + metric-based idleness", minus the items already shipped in Part A.
**Status of Part A (done):** Advisor port + Ollama adapter + template fallback, `ec2_idle` scanner, `NOTIFY_ONLY_RULES` gate, advisor budget, multi-region scan.

---

## 1. Scope

Five deliverables remain in Phase 4. Everything below is derived from the spec, with deviations called out explicitly in §9.

| # | Deliverable | Spec line |
|---|---|---|
| B1 | `rds_idle` scanner (metric-inferred) + `rds_stopped` sibling (state-based) | "the expensive case from my §1 table" |
| B2 | `s3_lifecycle` scanner family | "different waste shape: buckets are free to exist, storage isn't" |
| B3 | Alembic migration for new `ResourceType` values | "New ResourceType values mean a CHECK-constraint migration" |
| B4 | Right-sizing digest (14-day metrics → Advisor suggestion, advisory-only) | "posted as an advisory-only digest (no buttons)" |
| B5 | Anomaly v1 — rolling z-score on daily estimated spend, Advisor narrates | "deterministic ... in domain; the Advisor only narrates it" |

**Explicitly out of scope:** any RDS remediation playbook, any object-deleting S3 remediation, Cost Explorer / real billing data, K8s manifests (Phase 5), Terraform (Phase 6).

---

## 2. Design decisions made up front

These are the choices that shape the diff. Each has a one-line rationale so the code review is about execution, not re-litigating direction.

### 2.1 RDS is entirely notify-only in v1

No entry in `PLAYBOOK_ALLOWLIST` for `ResourceType.RDS_INSTANCE`, **and** both rules listed in `NOTIFY_ONLY_RULES`. Double-gated on purpose: deleting a database is the highest blast-radius action this tool could take, and "delete with final snapshot" deserves its own phase and its own guardrail tests, not a ride-along in a scanner PR.

The rule-level gate is what matters for UX: without it, Slack renders an Approve button that the domain then refuses (`approve_blocked_no_playbook`), which is a worse experience than no button at all.

### 2.2 `stopped RDS` is flagged immediately, not after N days

AWS auto-restarts a stopped RDS instance after 7 days, and storage bills the whole time. There is no "stopped long enough to be abandoned" window that makes sense here — the state itself is the finding. Config knob (`RDS_STOPPED_THRESHOLD_DAYS`, default `0`) exists anyway so a team that stops dev databases nightly can suppress it.

### 2.3 S3 ships as two rules, not three

The spec names three waste shapes. Two of them are cheaply and accurately measurable; one is not.

| Rule | Measurable? | Disposition |
|---|---|---|
| `s3_incomplete_multipart` | Yes — `ListMultipartUploads` + `ListParts` gives exact byte counts | Own rule, **remediable** |
| `s3_no_lifecycle` | Bucket size from CloudWatch; savings are a heuristic | Own rule, **notify-only** |
| noncurrent versions piling up | Would need a full `ListObjectVersions` walk — unbounded cost on a real bucket | Folded into `s3_no_lifecycle` **evidence** (versioning enabled + no `NoncurrentVersionExpiration` rule), not its own rule |

### 2.4 The one S3 remediation is `abort_incomplete_multipart_uploads`, not "apply a lifecycle policy"

The spec says remediation should be additive, never bulk-delete. Aborting an incomplete multipart upload deletes no object — no object was ever completed; it discards orphaned parts that are billing at full storage rate. It is immediate, exactly measurable, and verifiable in the LocalStack integration test, where a lifecycle rule would take up to 24h to act and is therefore untestable end-to-end.

Guardrail: the playbook **re-checks upload age at execution time** against `S3_INCOMPLETE_MPU_AGE_DAYS`, not just at detection time. An upload that started 10 minutes ago is a live client; one that started 8 days ago is dead.

### 2.5 Bucket size comes from CloudWatch only; the seed script publishes synthetic metrics

S3 has no size API. Real accounts get it from the daily `AWS/S3 BucketSizeBytes` metric. LocalStack does not emit that metric on its own.

An earlier draft proposed a bounded `ListObjectsV2` fallback to cover the gap. **Dropped** — it is a workaround for an emulator limitation leaking into production code paths, and it would run against real buckets too.

Instead, `scripts/seed_localstack.py` publishes synthetic `AWS/S3 BucketSizeBytes` datapoints to LocalStack's CloudWatch, exactly as `publish_idle_metrics()` already does for `AWS/EC2` in Part A. Same mechanism, no new production code, local demo still works.

Unknown size produces **no finding**, logged at INFO — the same "refuse to judge on thin data" posture as `ec2_idle`'s `min_datapoints`.

### 2.6 `s3_no_lifecycle` savings are a stated fraction, not the bucket's full cost

A bucket with no lifecycle policy is not 100% waste. Reporting full storage cost as "potential savings" would let one large bucket dominate the total and make every number in the tool untrustworthy.

Estimate: `bucket_monthly_cost × S3_LIFECYCLE_ADDRESSABLE_FRACTION` (default `0.20`). The fraction, the raw bucket cost, and the raw size all go into `evidence` so the number is auditable rather than magic. Documented in `pricing.py` alongside the existing list-price caveats.

### 2.7 Anomaly detection uses pydantic models + stdlib `statistics`, not pandas

**Decided.** The spec says "deterministic pandas, in domain". `pandas` is not on the import-linter forbidden list, but the contract is literally named *"Domain is pure Python (pydantic only)"*. A rolling mean/stdev/z-score is ~20 lines of `statistics`. Taking a 60MB dependency to avoid writing them would make the architecture claim in the README false.

Shapes (`SpendSnapshot`, `SpendAnomaly`) are pydantic models in `domain/models.py` like every other domain type; the math is stdlib. No new runtime dependency in this phase.

**Deliberate deviation from the spec — see §9 (D1).**

### 2.8 "Daily estimated spend" means daily estimated *waste*, honestly labelled

There is no billing data in this system — Cost Explorer needs a real account (Phase 6) and costs $0.01/request. What is actually available daily is the total `est_monthly_cost_usd` of open findings plus inventory counts.

So v1 detects anomalies in **estimated monthly waste**, stored in a new `spend_snapshots` table, and every user-facing string says so. A Cost Explorer-backed adapter can replace the input later without touching the z-score logic — that is the port design paying off, and it is a better README line than a wrong number.

### 2.9 `CloudGateway.get_instance_metric_averages` becomes generic

RDS metrics live in namespace `AWS/RDS` with dimension `DBInstanceIdentifier`. Adding a second near-identical port method would be duplication; adding one per service does not scale.

Replace it with:

```python
def get_metric_averages(
    self,
    namespace: str,
    dimension_name: str,
    dimension_value: str,
    metric_name: str,
    days: int,
    period_seconds: int = 3600,
) -> list[float]: ...
```

One method, one adapter implementation, callers pass their namespace. Touches `ports/cloud.py`, `adapters/aws/gateway.py`, `scanners/ec2_idle.py`, and the fake gateways in `tests/`. Small and contained; do it first so B1 builds on it.

### 2.10 The digest re-fetches metrics; it does not persist them per-scan

Right-sizing needs 14-day metrics for *non-idle* instances too, so scan findings do not carry them. The alternatives were a new `metric_summaries` table written every scan, or one extra CloudWatch pass in the weekly digest command.

Chose the extra pass: `GetMetricStatistics` is $0.01/1000 requests, the digest is weekly, and it keeps the schema and the `Scanner` port unchanged.

### 2.11 `PLAYBOOK_ALLOWLIST` stays keyed by resource type

S3 now has one remediable rule and one notify-only rule on the same `ResourceType`. `NOTIFY_ONLY_RULES` already handles that. Type-keying only truly breaks when two rules on one type need *different* playbooks, which is not yet the case. Note the pressure in a comment; a rule-keyed allowlist is a Phase 7 refactor, not a Phase 4B one.

### 2.12 Anything free LocalStack cannot exercise is skipped and noted, never worked around

**Decided policy for this phase.** Where LocalStack has no free-tier support for a service, the feature still ships with full moto unit coverage, but gets **no integration test** — and the gap is recorded rather than papered over with emulator-shaped production code (see §2.5, which this policy is what killed).

Each skip is recorded in two places so it stays visible:
- an `IntegrationCoverage` note in the README's testing section, and
- a `pytest.mark.skip` in `tests/integration/` carrying the reason, so the gap shows in test output instead of being invisible.

Applies in Phase 4B to:

| Gap | Consequence | Revisited in |
|---|---|---|
| LocalStack RDS is Pro-tier | `rds_idle` / `rds_stopped` are moto-only; no seeded RDS instances, no end-to-end RDS scan | Phase 6 (real AWS, read-only, `DRY_RUN=true`) |
| No native `AWS/S3 BucketSizeBytes` | Seed script publishes synthetic datapoints (§2.5) — covered, not skipped | — |
| Lifecycle-policy application takes up to 24h to act | Reinforces D3: `abort_incomplete_multipart_uploads` is the remediation, and it *is* end-to-end testable | Phase 7 if ever wanted |

### 2.13 The coverage gate splits into global 90% and `domain/` 95%

**Decided.** `ci.yml` currently gates at `--cov-fail-under=80`, inherited verbatim from the spec's Phase 3 line. Actual coverage on the unit suite alone is **94%** (1334 statements, 76 missed), so the gate is 14 points below reality and catches nothing.

Replace it with two gates:

```bash
# global — adapters included, headroom for boto3 I/O paths
pytest tests/ --cov=src/finops_sentinel --cov-report=term-missing --cov-fail-under=90

# the layer the architecture thesis rests on
coverage report --include='*/domain/*' --fail-under=95
```

**`ports/` is deliberately excluded from the second gate.** It reports 71 statements at 100%, and every one of them is free: the abstract bodies are `...` under `# pragma: no cover`, so what gets counted is import lines and `def` lines, which execute at class-definition time. Importing the package covers `ports/` completely without a single test. Including it would pad the denominator by 71 statements and buy roughly a full point of fake headroom. The gate measures `domain/` alone.

Why split rather than one bigger number:

- A flat 90% lets `domain/` rot to 85% as long as adapters compensate. That is backwards — the entire spec argues the domain is the part that must never break. The second gate says so in CI instead of only in the README.
- A flat 95% global would push toward thin mock-boto3 tests written purely to move a number. The two weakest files are `adapters/aws/gateway.py` (74%) and `adapters/notifications/slack.py` (77%) — both adapter I/O, both awkward to cover honestly.

**Does 95% on `domain/` force thin tests?** No — because of what is actually uncovered. Coverage measures execution, not assertion, so no percentage ever proves test quality; its job is only to catch branches nobody touched. `domain/` sits at 96% today, and all 9 missed statements are in `services.py`:

| Line(s) | Uncovered branch |
|---|---|
| 213, 225 | resource vanished mid-notify; lost CAS race in `notify_open_findings` |
| 269, 276 | finding / resource missing in `approve_finding` |
| **312–318** | **`approve_blocked_no_playbook` audit** |
| 321 | lost CAS race in `approve_finding` |
| 393, 399 | `deny_finding` early returns |

Every one is a guardrail or concurrency branch, testable with in-memory fakes. Line 312–318 is a spec §2 mandatory guardrail — *"only playbooks in PLAYBOOK_ALLOWLIST can run"* — with zero coverage today. These are tests worth writing on merit, independent of any number.

That is the distinction the split encodes: a gate induces thin tests when the remaining uncovered code is expensive-to-fake I/O. `domain/` is pure functions over fakes, so there is nothing to mock your way through.

**Prerequisite (W0):** write those 9 branches as guardrail tests *before* the gate lands. Without them `domain/` is 96% against a 95% floor — about one statement of headroom, which would trip on the first new defensive branch. With them it is ~100%, and 95% becomes a real floor. PR 2 then adds `domain/rightsizing.py` and `domain/anomaly.py`, both pure and cheap to cover fully, widening it further.

Phase 4B pressure on the global gate: W0–W2 add substantially to `gateway.py` (RDS describes, S3 describes and lifecycle/versioning lookups, the MPU playbook), already the weakest file. Projected global after PR 1 is ~93%, so 90% holds without number-chasing.

The spec's own Phase 1 bar was "≥85% on domain + scanners" — the repo already clears it. 80% global was never the real target.

**Not in scope:** the actual anti-thinness tool is mutation testing (`mutmut` over `domain/`), which deletes assertions and checks that tests fail. Phase 7 material, noted so it is not confused with what a coverage gate can do.

---

## 3. Work packages

Six sequenced packages, each a self-contained commit with its tests green before the next starts.

### W0 — Groundwork (ports + enum + migration 0003)

| File | Change |
|---|---|
| `ports/cloud.py` | Replace `get_instance_metric_averages` with generic `get_metric_averages` (§2.9). Add `describe_rds_instances()`, `describe_s3_buckets()`, `get_bucket_lifecycle_configuration()`, `get_bucket_versioning()`, `list_incomplete_multipart_uploads()` |
| `adapters/aws/gateway.py` | Implement the above. Add `rds` and `s3` boto3 clients alongside `ec2`/`cloudwatch`. `ClientError` on any of them logs a warning and returns empty — a metrics or listing outage never fails a scan |
| `adapters/aws/scanners/ec2_idle.py` | Update the three metric calls to the new signature (`namespace="AWS/EC2"`, `dimension_name="InstanceId"`) |
| `domain/models.py` | `ResourceType.RDS_INSTANCE = "rds_instance"`, `ResourceType.S3_BUCKET = "s3_bucket"` |
| `adapters/persistence/sqlalchemy_repo.py` | Extend the `check_resource_type` CHECK constraint |
| `alembic/versions/0003_*.py` | Rewrite the CHECK constraint via `op.batch_alter_table(recreate="always")` — SQLite cannot `ALTER` a constraint. **Must re-declare the existing constraints and indexes inside the batch block or they are silently dropped.** `downgrade()` restores the old constraint and will fail if rows with new types exist; document that in the docstring |
| `tests/unit/test_ec2_idle_scanner.py`, `tests/conftest.py` | Update fake gateways for the new port signature |
| `tests/unit/test_services.py` | **Guardrail-branch backfill (§2.13 prerequisite).** The 9 uncovered `services.py` statements, all with in-memory fakes: resource vanished mid-notify; lost CAS race in notify; missing finding and missing resource in `approve_finding`; **`approve_blocked_no_playbook`** (a spec §2 guardrail with zero coverage today); lost CAS race in approve; both `deny_finding` early returns. Lands **before** the new gate so 95% has real headroom rather than one statement |
| `.github/workflows/ci.yml` | Split coverage gate per §2.13 — global 90%, `--include='*/domain/*'` at 95% |

**Done when:** existing suite is green against the new port; `domain/` coverage is ~100% and the new gate passes; `alembic upgrade head` then `downgrade -1` then `upgrade head` round-trips on a copy of `.sentinel.db`.

### W1 — RDS scanners

| File | Change |
|---|---|
| `adapters/aws/scanners/rds.py` (new) | `IdleRDSScanner` and `StoppedRDSScanner`. Both `discover()` off one `describe_rds_instances()` call and filter on `DBInstanceStatus` in `evaluate()` — RDS `DescribeDBInstances` has no server-side status filter, so one call serves both |
| | `IdleRDSScanner`: `available` instances whose average `DatabaseConnections` stays at/below `RDS_IDLE_MAX_CONNECTIONS` across the window, with `>= RDS_IDLE_MIN_DATAPOINTS` datapoints. Short series ⇒ no verdict |
| | `StoppedRDSScanner`: `stopped` instances, evidence carries storage size, engine, class, and the 7-day auto-restart warning |
| `ports/pricing.py` + `adapters/aws/pricing.py` | `rds_instance_monthly(instance_class, engine, region)` and `rds_storage_monthly(size_gb, storage_type, region)`. Rate tables with cited sources, same defaults-are-mid-range policy as `EC2_HOURLY`. Stopped instances price on **storage only** — compute is not billed while stopped |
| `domain/rules.py` | `NOTIFY_ONLY_RULES += {"rds_idle", "rds_stopped"}`, with the §2.1 rationale as a comment. No `PLAYBOOK_ALLOWLIST` entry |
| `domain/summaries.py` | Template copy for both rules |
| `bootstrap.py` | Register both in `get_scanners()` |
| `config.py` | `rds_idle_observation_days=14`, `rds_idle_min_datapoints=24`, `rds_idle_max_connections=0.0`, `rds_stopped_threshold_days=0` |
| `tests/unit/test_rds_scanners.py` (new) | moto: idle instance flagged; busy instance not flagged; thin metric series not flagged; stopped instance flagged; `finops:protected=true` instance excluded by domain; approving an `rds_idle` finding is refused and audited as `approve_blocked_notify_only` |
| `tests/integration/test_localstack_e2e.py` | A `pytest.mark.skip` placeholder carrying the reason, per §2.12 — the gap shows in test output instead of being invisible |
| `pyproject.toml` | dev extra → `moto[ec2,cloudwatch,rds,s3]` |

**Integration gap (§2.12):** LocalStack RDS is Pro-tier, so there is no seeded RDS instance and no end-to-end RDS scan. Coverage is moto-only, skipped-and-noted, revisited in Phase 6 against a real account in read-only `DRY_RUN=true` mode. The seed script gains no RDS section.

### W2 — S3 scanners + the one additive playbook

| File | Change |
|---|---|
| `adapters/aws/scanners/s3.py` (new) | `S3LifecycleScanner` emitting two rules. `discover()` lists buckets, resolves each bucket's region (a bucket is global-namespace but region-homed — skip buckets outside the scanner's own region so multi-region scans don't double-count), pulls tags, lifecycle config, versioning, size (§2.5), and incomplete MPUs |
| | `s3_no_lifecycle`: size ≥ `S3_MIN_BUCKET_SIZE_GB` and no lifecycle configuration. Evidence: size, versioning state, whether any `NoncurrentVersionExpiration` rule exists, the addressable fraction used |
| | `s3_incomplete_multipart`: uploads older than `S3_INCOMPLETE_MPU_AGE_DAYS`. Evidence: upload count, summed part bytes, oldest initiation timestamp |
| `ports/pricing.py` + `adapters/aws/pricing.py` | `s3_storage_monthly(size_gb, storage_class, region)`. S3 Standard $0.023/GB-mo, cited. Addressable-fraction caveat documented next to the existing list-price caveats |
| `adapters/aws/gateway.py` | Playbook `abort_incomplete_multipart_uploads` — re-checks age per upload (§2.4), aborts, returns `{"aborted": n, "bytes_reclaimed": …, "skipped_too_recent": m}`. Honors `dry_run` like every other playbook |
| `domain/rules.py` | `PLAYBOOK_ALLOWLIST[ResourceType.S3_BUCKET] = "abort_incomplete_multipart_uploads"`; `NOTIFY_ONLY_RULES += {"s3_no_lifecycle"}` with the §2.11 comment about why the type-keyed allowlist still holds |
| `domain/summaries.py` | Template copy for both rules |
| `bootstrap.py`, `config.py` | Register scanner; `s3_min_bucket_size_gb=50`, `s3_incomplete_mpu_age_days=7`, `s3_lifecycle_addressable_fraction=0.20` |
| `docker-compose.yml` | `SERVICES=ec2,cloudwatch,sts,s3` |
| `scripts/seed_localstack.py` | Seed: one large bucket with no lifecycle policy, one with a policy (must **not** be flagged), one with a >7-day-old incomplete MPU, one tagged `finops:protected=true`. Plus `publish_bucket_size_metrics()` — synthetic `AWS/S3 BucketSizeBytes` datapoints, mirroring the existing `publish_idle_metrics()` (§2.5) |
| `tests/unit/test_s3_scanners.py` (new) | moto: no-lifecycle bucket over threshold flagged; under threshold not flagged; bucket with a policy not flagged; unknown size (no `BucketSizeBytes` datapoints) not flagged; MPU older than threshold flagged with correct byte total; MPU newer not flagged; protected bucket excluded; playbook aborts only the aged upload and skips the fresh one; `dry_run=True` aborts nothing |
| `tests/integration/test_localstack_e2e.py` | Extend: seed → scan → S3 findings present → approve the MPU finding → uploads actually gone → audit trail complete |

### W3 — Digest transport (`Notifier.send_digest`, `Advisor.narrate`)

| File | Change |
|---|---|
| `ports/notifier.py` | `send_digest(title: str, sections: list[str]) -> str \| None` — advisory only, **no buttons**, contract states that explicitly |
| `adapters/notifications/console.py`, `slack.py` | Implement. Slack: header + section blocks, no `actions` block |
| `ports/advisor.py` | `narrate(topic: str, facts: dict[str, Any]) -> str` — same never-raise + degrade-to-template contract as `summarize` |
| `domain/summaries.py` | `render_template_narration(topic, facts)` — the deterministic floor for `narrate` |
| `adapters/advisor/ollama.py` | Implement `narrate` with a strict Pydantic-validated JSON response; every failure mode falls back to the template, exactly like `summarize` |
| `adapters/advisor/template.py` | Implement `narrate` via the template renderer |
| `tests/unit/test_notifiers.py`, `test_advisor.py` | Digest send for both notifiers; `narrate` degrades on timeout / bad JSON / schema violation; any test fakes implementing these ports updated |

Adding a second Advisor method is anticipated growth — the spec's Phase 7 already plans a `query()` method on this port.

### W4 — Right-sizing digest

| File | Change |
|---|---|
| `adapters/aws/pricing.py` | Downsize + Graviton candidate map next to `EC2_HOURLY` (it is price-table knowledge). `m5.xlarge → [m5.large, m6g.xlarge, m6g.large]`, etc. |
| `ports/pricing.py` | `rightsizing_candidates(instance_type, region) -> list[RightsizingCandidate]` — supplies *what is cheaper*, never *whether to recommend* |
| `domain/models.py` | `RightsizingCandidate` (target type, monthly cost, monthly saving) and `RightsizingSuggestion` |
| `domain/rightsizing.py` (new) | Pure: given a resource, its metric summary, and the candidate list, decide whether to suggest. Rule — `max` CPU below `RIGHTSIZING_CPU_HEADROOM_PERCENT` over the window with `>= RIGHTSIZING_MIN_DATAPOINTS` datapoints. Uses **max**, not mean: a box that peaks at 90% once a day is correctly sized even if its mean is 4% |
| `domain/services.py` | `build_rightsizing_digest(targets, repo, advisor, pricing, ...) -> list[RightsizingSuggestion]`, capped at `DIGEST_MAX_ITEMS`, biggest saving first. Advisor narrates the summary; suggestions themselves are deterministic |
| `adapters/inbound/cli.py` | `sentinel digest [--no-send]` — renders a rich table locally and posts via `send_digest`. Intended for a weekly schedule (Phase 5 CronJob) |
| `config.py` | `rightsizing_observation_days=14`, `rightsizing_cpu_headroom_percent=40.0`, `rightsizing_min_datapoints=24`, `digest_max_items=10` |
| `tests/unit/test_rightsizing.py` (new) | Pure-domain, fakes only: over-provisioned instance suggests the right target and saving; spiky instance not suggested; thin series not suggested; instance type with no cheaper candidate not suggested; suggestions ordered by saving and capped |
| `tests/unit/test_digest.py` (new) | `FakeNotifier` + `FakeAdvisor` — digest composes, sends once, contains no approve affordance; a raising advisor still produces a digest |

### W5 — Anomaly v1

| File | Change |
|---|---|
| `alembic/versions/0004_*.py` | `spend_snapshots(id, snapshot_date UNIQUE, total_estimated_monthly_usd, open_findings, active_resources, captured_at)` |
| `adapters/persistence/sqlalchemy_repo.py` | `SpendSnapshotModel` + upsert-by-date (several scans a day collapse to one row, last write wins) |
| `ports/repository.py` | `record_spend_snapshot(...)`, `get_spend_snapshots(since: date) -> list[SpendSnapshot]` |
| `domain/models.py` | `SpendSnapshot`, `SpendAnomaly` (date, value, mean, stdev, z, direction) |
| `domain/anomaly.py` (new) | Pure stdlib z-score (§2.7). Guards: `< ANOMALY_MIN_HISTORY_DAYS` points ⇒ `None`; `stdev == 0` ⇒ `None` (no div-by-zero, and a perfectly flat series has no anomalies); `abs(z) >= ANOMALY_Z_THRESHOLD` ⇒ anomaly, direction reported |
| `domain/services.py` | `run_scan` writes today's snapshot before returning (audited as `spend_snapshot_recorded`); `detect_spend_anomaly(repo, ...)`; digest prepends the anomaly section when one exists, narrated by the Advisor |
| `config.py` | `anomaly_window_days=14`, `anomaly_min_history_days=7`, `anomaly_z_threshold=2.0` |
| `tests/unit/test_anomaly.py` (new) | Pure: clear spike detected with correct z; flat series ⇒ none; short history ⇒ none; zero-stdev ⇒ none; drop detected with negative direction |
| `tests/unit/test_repository.py` | Snapshot upsert-by-date; window query boundaries |

### W6 — Docs, config surface, CI, verification

W6 is not one commit at the end — it splits across both PRs, each shipping the docs for what it contains (working-agreement rule 9).

- `.env.example`: every one of the ~14 new variables, each with a one-line comment and a safe default (spec working-agreement rule 8).
- `README.md`: a "Phase 4 (Part B) Completed" section matching the existing style — the RDS notify-only rationale, the S3 additive-remediation stance, the honest framing of "estimated waste" vs. billed spend, and the stdlib-not-pandas call.
- `README.md`: a testing-coverage subsection listing every §2.12 skip-and-note gap and when it is revisited.
- `ci.yml`: confirm the LocalStack service container exposes `s3`. Replace `--cov-fail-under=80` with the split gate from §2.13 — global 90% plus a `coverage report --include='*/domain/*' --fail-under=95` step. **Lands in PR 1**, after the W0 guardrail tests, so PR 2's new domain modules (`rightsizing.py`, `anomaly.py`) are held to the domain gate from their first commit.
- Run the §5 verification checklist.

---

## 4. New configuration surface

| Variable | Default | Meaning |
|---|---|---|
| `RDS_IDLE_OBSERVATION_DAYS` | `14` | Connection-metric window |
| `RDS_IDLE_MIN_DATAPOINTS` | `24` | Below this, no verdict |
| `RDS_IDLE_MAX_CONNECTIONS` | `0.0` | Average `DatabaseConnections` at/below this is idle |
| `RDS_STOPPED_THRESHOLD_DAYS` | `0` | Flag stopped instances immediately (§2.2) |
| `S3_MIN_BUCKET_SIZE_GB` | `50` | Below this, a missing lifecycle policy is not worth an alert |
| `S3_INCOMPLETE_MPU_AGE_DAYS` | `7` | Detection **and** execution-time abort threshold |
| `S3_LIFECYCLE_ADDRESSABLE_FRACTION` | `0.20` | Share of bucket cost a lifecycle policy plausibly recovers (§2.6) |
| `RIGHTSIZING_OBSERVATION_DAYS` | `14` | Metric window for the digest |
| `RIGHTSIZING_CPU_HEADROOM_PERCENT` | `40.0` | Peak CPU below this ⇒ downsize candidate |
| `RIGHTSIZING_MIN_DATAPOINTS` | `24` | Below this, no suggestion |
| `DIGEST_MAX_ITEMS` | `10` | Cap on digest suggestions |
| `ANOMALY_WINDOW_DAYS` | `14` | Trailing window for mean/stdev |
| `ANOMALY_MIN_HISTORY_DAYS` | `7` | Below this, no verdict |
| `ANOMALY_Z_THRESHOLD` | `2.0` | \|z\| at or above this is an anomaly |

---

## 5. "Done when" — verification checklist

Spec gate: *"notifications carry LLM summaries, the weekly digest posts, and killing Ollama mid-run degrades gracefully to templates."* Part A covered the first and third for `summarize`; B extends them.

1. `ruff check`, `mypy`, `lint-imports` all clean.
2. Full suite green under the **split coverage gate** (§2.13): global ≥ 90%, `domain/` ≥ 95% (`ports/` excluded — its coverage is free).
3. `alembic upgrade head` → `downgrade -1` → `upgrade head` round-trips against a copy of `.sentinel.db`; a pre-migration DB upgrades without data loss.
4. `docker compose up -d && python scripts/seed_localstack.py && sentinel scan` reports S3 findings alongside the existing ones; the bucket that already has a lifecycle policy is **not** flagged; the protected bucket is excluded.
5. Approving the `s3_incomplete_multipart` finding in Slack aborts the aged upload only, leaves a fresh one alone, and writes a complete audit trail. With `DRY_RUN=true` nothing is aborted.
6. Approving an `rds_idle` or `rds_stopped` finding is refused and audited as `approve_blocked_notify_only`; Slack renders no Approve button for them. **Verified in moto, not LocalStack** (§2.12), and the skip appears in `pytest` output with its reason.
7. `sentinel digest` posts a button-free digest with right-sizing suggestions ordered by saving.
8. With ≥ 7 days of seeded `spend_snapshots` and an injected spike, the digest leads with a narrated anomaly section.
9. `ollama stop` mid-`digest` still produces the digest, with template narration in place of LLM prose.
10. `sentinel smoke-llm` still passes (Part A regression check).

---

## 6. Risks

| Risk | Mitigation |
|---|---|
| SQLite CHECK-constraint migration silently drops indexes | `batch_alter_table(recreate="always")` with every existing constraint and index re-declared; round-trip test in the gate |
| `downgrade` fails once `rds_instance` / `s3_bucket` rows exist | Expected and documented in the migration docstring — the downgrade is not lossless by design |
| LocalStack S3 has no `BucketSizeBytes` metric | Seed script publishes synthetic datapoints (§2.5); unknown size produces no finding |
| LocalStack RDS is Pro-tier — RDS ships with no end-to-end proof | Accepted per §2.12: moto-only unit coverage, a reasoned `pytest.mark.skip` in the integration suite, README note, revisited in Phase 6 |
| `s3_no_lifecycle` savings inflate the headline total | Fixed fraction, exposed in evidence, capped by the size threshold (§2.6) |
| Right-sizing digest adds a CloudWatch pass | Weekly cadence; `GetMetricStatistics` is $0.01/1000 requests |
| Two new abstract methods break every existing `Notifier`/`Advisor` implementation | W3 updates both real adapters and every test fake in the same commit |
| W0–W2's new `gateway.py` code drags global coverage under the new 90% gate | `gateway.py` is already the weakest file at 74%; every new describe/playbook method ships with a moto test in the same commit rather than after the gate trips |
| `narrate` prompt injection via finding evidence | Same posture as `summarize`: strict schema validation on the response, advisory-only output, never drives a decision |

---

## 7. Sequencing — two PRs

**Decided.** W0 → W1 → W2 → W3 → W4 → W5, with W6's docs split between the two PRs rather than trailing at the end.

### PR 1 — "Phase 4B: RDS and S3 scanners" (W0 + W1 + W2)

The review-sensitive half: new resource types, a schema migration, a new remediation playbook, and two new guardrail entries. Lands independently — the digest work needs none of it.

- W0 groundwork (port refactor, enum, migration 0003)
- W1 RDS scanners (notify-only)
- W2 S3 scanners + `abort_incomplete_multipart_uploads`
- Docs for the above: `.env.example` (7 vars — 4 RDS, 3 S3), README scanner + testing-gap sections, compose `SERVICES`, seed script
- `ci.yml` split coverage gate (§2.13) — lands here so PR 2 inherits it

Merge gate: verification items 1–6.

### PR 2 — "Phase 4B: right-sizing digest and spend anomaly" (W3 + W4 + W5)

Additive: two new port methods, one new CLI command, one new table. Touches no scanner and no guardrail.

- W3 digest transport (`send_digest`, `narrate`)
- W4 right-sizing digest + `sentinel digest`
- W5 anomaly + migration 0004
- Docs for the above: `.env.example` (7 vars — 4 right-sizing/digest, 3 anomaly), README digest/anomaly section

Merge gate: verification items 7–10, plus 1–3 re-run.

Commit style within each PR: one conventional commit per work package, tests in the same commit as the code they cover (working-agreement rule 4).

---

## 8. Files at a glance

**New:** `adapters/aws/scanners/rds.py`, `adapters/aws/scanners/s3.py`, `domain/rightsizing.py`, `domain/anomaly.py`, `alembic/versions/0003_*.py`, `alembic/versions/0004_*.py`, `tests/unit/test_rds_scanners.py`, `tests/unit/test_s3_scanners.py`, `tests/unit/test_rightsizing.py`, `tests/unit/test_anomaly.py`, `tests/unit/test_digest.py`

**Modified:** `.github/workflows/ci.yml`, `tests/unit/test_services.py`, `ports/cloud.py`, `ports/pricing.py`, `ports/notifier.py`, `ports/advisor.py`, `ports/repository.py`, `domain/models.py`, `domain/rules.py`, `domain/services.py`, `domain/summaries.py`, `adapters/aws/gateway.py`, `adapters/aws/pricing.py`, `adapters/aws/scanners/ec2_idle.py`, `adapters/advisor/ollama.py`, `adapters/advisor/template.py`, `adapters/notifications/console.py`, `adapters/notifications/slack.py`, `adapters/persistence/sqlalchemy_repo.py`, `adapters/inbound/cli.py`, `bootstrap.py`, `config.py`, `pyproject.toml`, `docker-compose.yml`, `scripts/seed_localstack.py`, `.env.example`, `README.md`, `tests/conftest.py`, `tests/unit/test_ec2_idle_scanner.py`, `tests/unit/test_notifiers.py`, `tests/unit/test_advisor.py`, `tests/unit/test_repository.py`, `tests/integration/test_localstack_e2e.py`

---

## 9. Deviations from the spec — all approved

| # | Spec says | Plan does | Why |
|---|---|---|---|
| D1 | "deterministic pandas, in domain" | pydantic models + stdlib `statistics` | The import-linter contract is named "Domain is pure Python (pydantic only)"; a z-score is 20 lines (§2.7) |
| D2 | "rolling z-score on daily estimated **spend**" | z-score on daily estimated **waste**, labelled as such | No billing data exists before Phase 6; the alternative is a number that is quietly wrong (§2.8) |
| D3 | S3 remediation is "apply lifecycle policy" | `abort_incomplete_multipart_uploads` | Additive-not-destructive intent preserved (no object is deleted), and it is the only S3 remediation testable end-to-end (§2.4) |
| D4 | Three S3 waste shapes | Two rules; noncurrent versions folded into evidence | Measuring noncurrent version size needs an unbounded `ListObjectVersions` walk (§2.3) |
| D5 | Stopped RDS as a plain state rule | Also notify-only, no playbook | Deleting a database deserves its own phase and its own guardrail tests (§2.1) |
| D6 | (not in spec) | Anything free LocalStack cannot exercise is skipped with a recorded note, never worked around | §2.12 — killed the `ListObjectsV2` size fallback and scoped RDS to moto-only |

---

## 10. Resolved decisions

Every open question from the first draft is now settled. Recorded here so the rationale survives the conversation.

| Question | Decision |
|---|---|
| pandas vs. stdlib for the z-score | **pydantic models + pure Python.** No new runtime dependency (D1) |
| D2 (waste vs. spend), D4 (two S3 rules), D5 (RDS notify-only) | **Accepted as recommended** |
| D3 — `abort_incomplete_multipart_uploads` instead of "apply lifecycle policy" | **Accepted** |
| Features free LocalStack cannot exercise | **Ship them, skip the integration test, record the note** (§2.12). No emulator-shaped code in production paths |
| Digest cadence | **`sentinel digest` as a manual/cron command now**; the weekly schedule lands as the Phase 5 CronJob |
| One PR or two | **Two** — PR 1 scanners + migration, PR 2 digest + anomaly (§7) |
| Coverage gate | **Split, not raised flat**: global 90% + `domain/` 95%, landing in PR 1 (§2.13). The old 80% was 14 points below actual. `ports/` excluded — its 100% is free. W0 backfills the 9 uncovered guardrail branches first so the floor is robust, not brittle |

**Ready to implement. Start at W0.**
