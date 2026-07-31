# FinOps Sentinel — Engineering Documentation

## Introduction

Cloud waste is not an event. It is a slow accumulation of things nobody
remembered to clean up: the volume left behind when an instance was terminated,
the Elastic IP reserved for a migration that finished last quarter, the staging
database somebody stopped instead of deleting — still billing for storage, and
scheduled to restart itself in seven days whether anyone wants it or not. Each
item is individually too small to chase. Together they are a line on the bill
that nobody can account for.

The awkward part is that this is not a detection problem. Most of it is
trivially detectable — an unattached volume is one API call away from obvious.
The hard part is that acting on it means **deleting infrastructure in a live
account**, and the cost of one wrong deletion dwarfs months of savings. So the
tooling splits into two unsatisfying camps: dashboards that report waste and
leave a human to do all the work anyway, and automation that acts confidently
and occasionally deletes the wrong thing.

**FinOps Sentinel is an attempt at the middle position.** It scans AWS
continuously, prices each piece of waste, explains it in plain language, and
pushes it to Slack with an Approve button — then, and only then, executes a
narrow, allowlisted, reversible remediation. The agent does the finding, the
pricing, the explaining, and the mechanics. A human does the deciding, always.

Three commitments shape everything downstream:

**Nothing is deleted without a human clicking a button.** There is no
auto-remediation path, not even for the safest rule. Five independent guardrail
layers stand between a detection and a deletion (§9), and the destructive
playbooks build their own recovery path — a volume is snapshotted, and the
snapshot is *waited on*, before deletion.

**The system refuses to guess.** A metric series too short to judge produces no
finding rather than a weak one. A bucket whose size is unknown produces no
finding. A spend anomaly with fewer than seven days of history reports *"too
little history to judge"* rather than *"no anomaly"* — because those are
different statements and only one of them is reassuring. Silence beats a false
positive, and an empty result over regions that failed to answer is reported as
incomplete rather than clean.

**Numbers say what they actually are.** A bucket without a lifecycle policy is
not wholly waste, so the finding reports a stated fraction of its cost with the
fraction, the raw size, and the full cost all in evidence. There is no billing
data in the system, so what the anomaly detector monitors is called *estimated
waste*, never *spend*. Prices are list prices, and the three reasons that makes
them optimistic are documented next to the price table.

### How it is built

The architecture is **hexagonal — ports and adapters**. The business rules live
in `domain/` and import nothing but pydantic: no boto3, no SQLAlchemy, no HTTP
client, no Slack SDK. Everything external sits behind six abstract ports that
the domain defines in its own vocabulary, and concrete adapters implement.

That decoupling is not decoration. It is enforced in CI by import-linter
contracts, and it is *proved* by a test that runs the entire approve-and-
remediate flow with a fake gateway, an in-memory repository, and a fake
notifier — zero AWS, zero HTTP, zero Slack. If that test passes, replacing
Slack with Telegram or SQLite with Postgres cannot break the approval logic,
because the approval logic never knew about either. The same property is what
lets the whole system run against LocalStack locally and real AWS in
production with no code change at all.

The LLM occupies a deliberately small role. It writes the prose on a
notification and narrates a digest; it never decides anything, never computes a
number, and its output is treated as untrusted display copy because the prompt
contains user-controlled tags. Every number it repeats was calculated
deterministically in the domain first, and if the model is unreachable, slow,
or returns malformed JSON, a template writes the same explanation and the
pipeline does not notice.

---

### About this document

A complete walkthrough of the system: what each part does, how the parts fit
together, and **why** each significant decision went the way it did. Written to
be handed to an engineer who has never seen the codebase.

The README explains how to *run* the project. This document explains how it
*works*.

---

## Table of contents

1. [What the system does](#1-what-the-system-does)
2. [The shape of the codebase](#2-the-shape-of-the-codebase)
3. [Architecture: ports and adapters](#3-architecture-ports-and-adapters)
4. [The domain layer](#4-the-domain-layer)
5. [The ports, one by one](#5-the-ports-one-by-one)
6. [The adapters, one by one](#6-the-adapters-one-by-one)
7. [Database design](#7-database-design)
8. [Execution flows end to end](#8-execution-flows-end-to-end)
9. [The safety model](#9-the-safety-model)
10. [Cost estimation](#10-cost-estimation)
11. [Configuration and composition](#11-configuration-and-composition)
12. [Testing strategy](#12-testing-strategy)
13. [Engineering decisions and their trade-offs](#13-engineering-decisions-and-their-trade-offs)
14. [Defects found by running it](#14-defects-found-by-running-it)
15. [Known limitations](#15-known-limitations)
16. [Extending the system](#16-extending-the-system)

---

## 1. What the system does

FinOps Sentinel scans AWS accounts for wasted spend, estimates what each piece
of waste costs per month, explains it in plain language, and — for a narrow set
of safe cases — remediates it after a human clicks Approve in Slack.

It runs on a loop of four verbs:

| Verb | Command | What happens |
|---|---|---|
| **Scan** | `sentinel scan` | Inventory every region, evaluate nine rules, persist findings, alert on new ones |
| **Decide** | Slack button or `POST /decisions/{id}` | A human approves or denies; guardrails re-checked; playbook runs |
| **Digest** | `sentinel digest` | Advisory patterns: right-sizing suggestions and spend anomalies. No buttons |
| **Expire** | `sentinel expire` | Findings nobody decided within 72h close themselves |

### The nine detection rules

| Rule | Detects | Actionable? |
|---|---|---|
| `ebs_unattached` | Volumes in `available` state | Yes — `snapshot_then_delete_volume` |
| `eip_orphaned` | Elastic IPs with no association | Yes — `release_eip` |
| `ec2_stopped` | Instances stopped beyond threshold | Yes — `terminate_stopped_instance` |
| `ebs_old_snapshot` | Snapshots past retention, or whose source volume is gone | Yes — `delete_ebs_snapshot` |
| `ec2_idle` | Running instances with flatlined CPU **and** network | No — advisory |
| `rds_idle` | Databases with no connections across the window | No — advisory |
| `rds_stopped` | Databases in `stopped` state | No — advisory |
| `s3_no_lifecycle` | Buckets over a size threshold with no lifecycle policy | No — advisory |
| `s3_incomplete_multipart` | Multipart uploads abandoned past threshold | Yes — `abort_incomplete_multipart_uploads` |

The split between actionable and advisory is not a maturity gradient — it is a
deliberate safety boundary, explained in [§9](#9-the-safety-model).

---

## 2. The shape of the codebase

```
src/finops_sentinel/
├── domain/              # Pure business logic. No AWS, no HTTP, no SQL.
│   ├── models.py        # Entities + the state machine table
│   ├── rules.py         # Guardrails: protection tags, playbook allowlist
│   ├── services.py      # Use cases: run_scan, approve, deny, digest
│   ├── summaries.py     # Deterministic prose (the LLM fallback floor)
│   ├── rightsizing.py   # Pure: is this instance over-provisioned?
│   └── anomaly.py       # Pure: is today's waste statistically unusual?
│
├── ports/               # Abstract interfaces. The domain's vocabulary.
│   ├── cloud.py         # CloudGateway — read AWS, execute playbooks
│   ├── repository.py    # FindingsRepository — persistence
│   ├── notifier.py      # Notifier — outbound alerts, inbound callbacks
│   ├── advisor.py       # Advisor — LLM prose
│   ├── pricing.py       # Pricing — what things cost
│   ├── authorization.py # Authorizer — may this actor approve?
│   └── scanner.py       # Scanner — the two-pass detection contract
│
├── adapters/            # Concrete implementations. All the messy details.
│   ├── aws/             # boto3 gateway, pricing tables, six scanners,
│   │                    #   per-approval STS credentials

│   ├── persistence/     # SQLAlchemy + SQLite
│   ├── notifications/   # Slack (Block Kit), Console
│   ├── advisor/         # Ollama (local LLM), Template (deterministic)
│   ├── authorization/   # Approver allowlist
│   └── inbound/         # Typer CLI, FastAPI HTTP
│
├── bootstrap.py         # Composition root — the only file that wires
└── config.py            # Settings (pydantic-settings), one per env var
```

The dependency rule is one-directional and machine-enforced:

```
inbound adapters ──▶ domain ◀── outbound adapters
                       │
                       ▼
                     ports  (domain defines what it needs)
                       ▲
                       └──── adapters implement it
```

**Nothing in `domain/` imports anything from `adapters/`.** Two import-linter
contracts in `pyproject.toml` enforce this in CI:

```toml
[[tool.importlinter.contracts]]
name = "Domain must not depend on adapters or the composition root"
type = "forbidden"
source_modules = ["finops_sentinel.domain", "finops_sentinel.ports"]
forbidden_modules = ["finops_sentinel.adapters", "finops_sentinel.bootstrap"]

[[tool.importlinter.contracts]]
name = "Domain is pure Python (pydantic only)"
type = "forbidden"
source_modules = ["finops_sentinel.domain"]
forbidden_modules = ["boto3", "botocore", "fastapi", "sqlalchemy", "alembic",
                     "slack_sdk", "typer", "rich", "uvicorn", "httpx"]
```

The second contract is stricter than the first and is why the anomaly detector
uses stdlib `statistics` rather than pandas — see [§13.6](#136-stdlib-statistics-not-pandas).

---

## 3. Architecture: ports and adapters

### Why this architecture

The claim being made is: *the business rules are independent of AWS, of Slack,
of SQLite, and of the LLM.* That claim is worth something only if it is
testable, so the design makes it testable.

The proof is a test that runs the **entire approve-and-remediate flow** with
zero AWS, zero HTTP, and zero Slack — a fake gateway, an in-memory repository,
a fake notifier. If that test passes, swapping Slack for Telegram cannot break
the approval logic, because the approval logic never knew about Slack.

### How a port earns its place

A port exists when the domain needs *a capability*, not when there happens to
be an external system. `CloudGateway` is not "the AWS API" — it is "the things
this domain needs a cloud to do." That is why it exposes
`describe_rds_instances()` and `execute(playbook, ...)` rather than a generic
`call_aws(service, operation, params)`. A generic passthrough would put AWS
concepts back in the domain and buy nothing.

### The composition root

`bootstrap.py` is the only module that knows which concrete adapters exist.
Everything else receives ports. This is what makes the swap points real:

```python
ADVISOR_PROVIDERS: dict[str, Callable[[], Advisor]] = {
    "ollama": _build_ollama_advisor,
    "template": TemplateAdvisor,
}
```

Adding a hosted-LLM backend means writing one adapter and adding one dict
entry. No caller changes, because no caller ever sees anything but `Advisor`.

---

## 4. The domain layer

### 4.1 `models.py` — entities and the state machine

Eight types. `Resource` (what exists in the cloud), `Finding` (what is wrong
with it), `Decision`, `AuditEvent`, plus the digest shapes
`RightsizingCandidate`, `RightsizingSuggestion`, `SpendSnapshot`, and
`SpendAnomaly`.

The digest shapes are deliberately **not** `Finding`s: a right-sizing
suggestion has nothing to approve — no resource is being deleted — so it
carries no status, no protection flag, and never enters the state machine.

The important part of this file is not the entities — it is that **the state
machine lives next to the enum it governs**:

```python
class FindingStatus(StrEnum):
    OPEN = "open"
    NOTIFIED = "notified"
    APPROVED = "approved"
    DENIED = "denied"
    REMEDIATED = "remediated"
    FAILED = "failed"
    EXPIRED = "expired"

TRANSITIONS: dict[FindingStatus, set[FindingStatus]] = {
    FindingStatus.OPEN:     {FindingStatus.NOTIFIED},
    FindingStatus.NOTIFIED: {FindingStatus.APPROVED, FindingStatus.DENIED,
                             FindingStatus.EXPIRED},
    FindingStatus.APPROVED: {FindingStatus.REMEDIATED, FindingStatus.FAILED},
    # DENIED / REMEDIATED / FAILED / EXPIRED are terminal in v1
}
```

The full lifecycle:

```
                    ┌──────────────── EXPIRED (72h, terminal)
                    │
   OPEN ──────▶ NOTIFIED ──────▶ DENIED (terminal)
    │               │
    │               └──────▶ APPROVED ──┬──▶ REMEDIATED (terminal)
    │                                   └──▶ FAILED (terminal)
    │
    └── protected findings stay here forever, by design
```

Four properties fall out of this design:

- **A re-scan can never resurrect a decided finding.** `save_finding()` updates
  evidence, cost, and `last_seen_at`, but never touches `status`. A `DENIED`
  finding stays denied no matter how many times it is re-detected.
- **Alerts fire exactly once**, on `OPEN → NOTIFIED`. This is why a second scan
  sends no Slack messages — the transition already happened.
- **Protected findings never leave `OPEN`.** They are reported in the CLI table
  and excluded from every total.
- **Dry-run stops at `APPROVED`.** Only a real execution reaches `REMEDIATED`,
  so the status distinguishes "a human said yes" from "the cloud actually
  changed."

### 4.2 `rules.py` — the guardrails

Small file, disproportionate importance. Two mechanisms:

**Tag-based protection.** Anything tagged `finops:protected=true` is never
notified and never actionable. `is_protected()` accepts both the domain's dict
form and AWS's native `[{"Key":..., "Value":...}]` list, so raw provider data
can be checked before it is ever converted.

**The playbook allowlist**, keyed by resource type:

```python
PLAYBOOK_ALLOWLIST: dict[ResourceType, str] = {
    ResourceType.EBS_VOLUME:   "snapshot_then_delete_volume",
    ResourceType.ELASTIC_IP:   "release_eip",
    ResourceType.EC2_INSTANCE: "terminate_stopped_instance",
    ResourceType.EBS_SNAPSHOT: "delete_ebs_snapshot",
    ResourceType.S3_BUCKET:    "abort_incomplete_multipart_uploads",
}
```

No entry means no action is possible. `RDS_INSTANCE` is absent deliberately.

**The rule-level gate** exists because the allowlist is keyed by *type*, and
some types carry both actionable and advisory rules:

```python
NOTIFY_ONLY_RULES: frozenset[str] = frozenset(
    {"ec2_idle", "rds_idle", "rds_stopped", "s3_no_lifecycle"}
)
```

Without this, two concrete disasters:

1. An `ec2_idle` finding is on a **running** instance. The type-keyed allowlist
   would hand it `terminate_stopped_instance` — which does not check state.
2. `s3_no_lifecycle` and `s3_incomplete_multipart` share `S3_BUCKET`. Approving
   "this bucket has no lifecycle policy" would abort multipart uploads, because
   that is the playbook registered for the type.

The type-keyed mapping holds only while no two rules on one type need
*different* playbooks. S3 is the closest it has come to breaking, and the code
says so in a comment. When it breaks, the allowlist becomes rule-keyed.

### 4.3 `services.py` — the use cases

The orchestration layer. Every function takes ports, never adapters.

#### `run_scan(targets, repo, max_workers)` — the two-pass scan

**Pass 1: discovery.** Every scanner in every region returns
`(Resource, raw_dict)` pairs. These are upserted, refreshing `last_seen_at` and
`current_tags`. Resources not seen this scan are marked `DELETED`.

**Pass 2: evaluation.** Each scanner receives its own region's inventory and
emits findings.

The two passes are separate because **evaluation sometimes needs inventory the
scanner did not discover**. `ec2_stopped` prices an instance by summing its
still-billing EBS volumes — volumes that `UnattachedEBSScanner` discovered.
Pass 1 builds the shared picture; pass 2 reads it.

Three isolation boundaries, each protecting against a specific failure:

| Boundary | Failure it prevents |
|---|---|
| **Per-region** | One region's outage or missing IAM grant blinding all others |
| **Per-scanner** | One unavailable service (e.g. RDS) discarding every other scanner's findings in that region |
| **DELETED sweep scoped to succeeded regions** | A failed region's inventory being marked deleted, which would silently disarm every finding it owns |

The per-scanner boundary was added after a real incident — see
[§14](#14-defects-found-by-running-it).

`run_scan` raises `RuntimeError` when **every** region fails. Returning "no
findings" there would read as a clean account, which is the most expensive lie
this system could tell.

Discovery is threaded (`ThreadPoolExecutor`); all repository writes stay on the
calling thread, so the repository needs no thread-safety guarantees.

#### `notify_open_findings(repo, notifier, advisor, budget)`

Sends alerts for `OPEN`, non-protected findings and transitions them to
`NOTIFIED`. Three details worth knowing:

- **Most-expensive-first**, and only the first `advisor_budget` findings pay for
  LLM inference. Local inference costs seconds per finding; an account with a
  thousand findings would otherwise run for hours. The rest get templates.
- **A failed send leaves the finding `OPEN`**, so the next scan retries it.
- **The CAS transition is checked**: if another process won the race, the loop
  continues rather than double-recording.

#### `commit_approval(...)` / `execute_approval(...)` — where the guardrails converge

Split in two because deciding is fast and remediating is not. `commit_approval`
runs every guardrail and the CAS — repository reads and one write — and returns
an `ApprovalPlan`; `execute_approval` runs the playbook, which for an EBS volume
waits on a snapshot and takes minutes. A channel with an acknowledgement
deadline runs the first half inline, edits its message (removing the buttons)
and runs the second half in the background. `approve_finding(...)` is the two
composed, for the CLI and the HTTP API, which have no such deadline.

The order of checks matters, because each produces a different audit event:

```
1.  Finding exists?                    → False
2.  Transition legal per TRANSITIONS?  → False
3.  Actor may approve?                 → audit "approve_blocked_unauthorized"
4.  Resource exists?                   → False
5.  Resource DELETED?                  → audit "approve_blocked_resource_gone"
6.  Protected (then or now)?           → audit "approve_blocked_protected"
7.  Rule is notify-only?               → audit "approve_blocked_notify_only"
8.  Type has a playbook?               → audit "approve_blocked_no_playbook"
9.  CAS NOTIFIED→APPROVED              → False if already decided
10. Execute playbook (in try/except)
11. dry_run? stop at APPROVED : → REMEDIATED
```

Protection is re-checked **at approval time, not just detection time** —
someone may have tagged the resource in between, and the newer intent wins.

The authority check (3) is in the domain on purpose. A channel adapter proves
the *transport*: a valid Slack signature says the request came through this app,
and the signing secret is app-level, so that proof is shared by everyone who can
see the message. Whether the person who clicked may delete infrastructure is a
different question, and it is asked here so it survives a swap to any other
channel. The Slack-shaped half of the problem — which workspace, which channel —
stays in the adapter next to signature verification, because `team.id` has no
meaning to a Telegram install.

The CAS at (9) names `NOTIFIED` as its expected value rather than the status
this invocation happened to read. That distinction is the difference between
stopping a concurrent double-click and stopping a *replay*: a second click or a
Slack retry-after-timeout arrives after the first request committed, so it reads
`APPROVED`, and `SET status='APPROVED' WHERE status='APPROVED'` would match a
row and remediate twice. The transition table refuses that case one gate above,
but only for as long as nobody adds a self-loop to it.

The gateway is resolved from *the resource's own region*, not a single
configured one. An EC2 call for a `eu-west-1` volume sent to the `us-east-1`
endpoint fails with `InvalidVolume.NotFound`, which would read as "already
deleted" rather than "wrong region."

Gateway resolution happens **inside** the try block, so a bad region or missing
credentials is recorded as a failed remediation rather than stranding the
finding in `APPROVED` with no explanation.

#### The digest functions

Three functions, deliberately separated:

- `build_rightsizing_digest(targets, pricing, ...) -> RightsizingReport`
  gathers CloudWatch data and applies the pure rule.
- `compose_digest_sections(report, anomaly, advisor, ...) -> list[str]`
  renders. Pure: no clock, no repository, no sending. Testable without a
  notifier.
- `send_digest(repo, notifier, report, ...)` sends once and audits.

`RightsizingReport` carries `regions_failed` and `instances_examined` alongside
the suggestions, because an empty suggestion list over 40 instances and an
empty one over zero regions mean opposite things and only one is safe to act on
by doing nothing.

### 4.4 `rightsizing.py` — peak, never average

```python
if len(cpu_series) < min_datapoints:
    return None                      # no history is not evidence
max_cpu = max(cpu_series)
if max_cpu >= cpu_headroom_percent:
    return None                      # it gets busy; leave it alone
```

**The decision to use `max` rather than `mean` is the single most important
line in this module.** An instance that idles all day and pegs 90% CPU once an
hour has a mean around 4%. A mean-based tool recommends halving the machine
that carries the actual workload. Averages are how right-sizing tools produce
advice that takes production down.

The 40% default pairs with a candidate table that steps down **at most one
size**: halving vCPU roughly doubles utilisation, so a 40% peak lands near 80%
on the target. Raising the threshold without shortening the candidate list
breaks that pairing silently — documented in both the config and the price
table.

### 4.5 `anomaly.py` — a z-score with guards

```python
ordered  = sorted(snapshots, key=lambda s: s.snapshot_date)
latest   = ordered[-1]
baseline = ordered[-(window_days + 1):-1]      # candidate day EXCLUDED

if len(baseline) < min_history_days: return None
stdev = statistics.stdev(values)
if stdev == 0: return None
z = (value - mean) / stdev
if abs(z) < z_threshold: return None
```

Every guard returns `None` — *no verdict* — rather than a weak one. An anomaly
alert that fires on three days of history trains people to ignore it, which
costs more than the alert it replaced.

**Excluding the candidate day from its own baseline** is load-bearing at these
sample sizes. With seven points, a spike included in the mean and stdev it is
being measured against inflates both enough to hide itself. There is a test
that pins exactly this.

**Zero stdev returns `None`** for two reasons: it is a division by zero, and it
is a meaningless question — a perfectly flat series has no distribution to be
an outlier in. It is also the common early case, when a dev account's findings
do not change day to day.

### 4.6 `summaries.py` — the deterministic floor

Per-rule copy plus two narrators (`rightsizing`, `spend_anomaly`). This is the
floor the `Advisor` port promises: whatever happens to the LLM, every finding
can still be explained.

Two hard requirements, both tested: it must never raise for any well-formed
input, and an unknown rule or topic must still produce something readable
rather than an exception in the middle of a notification.

---

## 5. The ports, one by one

### `CloudGateway` (`ports/cloud.py`)

Reads cloud state and executes remediation playbooks.

| Method | Returns |
|---|---|
| `describe_ebs_volumes()` | All volumes |
| `describe_elastic_ips()` | All addresses |
| `describe_ec2_instances()` | **Stopped** instances only |
| `describe_running_ec2_instances()` | **Running** instances only |
| `describe_ebs_snapshots()` | Self-owned snapshots |
| `describe_rds_instances()` | Every RDS instance, any state |
| `describe_s3_buckets()` | Buckets homed in this region, with config |
| `get_incomplete_multipart_uploads(bucket)` | Uploads with byte sizes |
| `get_metric_averages(namespace, dimensions, metric_name, days, period)` | CloudWatch series |
| `execute(playbook, resource_id, dry_run)` | Remediation result dict |

Two design notes:

**Stopped and running instances are separate methods** because they serve
different rules and the filter belongs server-side. RDS has no such split —
`DescribeDBInstances` has no status filter, so callers filter themselves, and
the port documents that asymmetry rather than hiding it.

**`get_metric_averages` takes a dimension *map*, not a name/value pair.** This
is not generality for its own sake: CloudWatch matches dimension sets
**exactly**. S3's `BucketSizeBytes` is published against `BucketName` *and*
`StorageType`; a query naming only one returns nothing at all. The original
single-pair signature would have made every bucket read as "size unknown"
against real AWS — and neither moto nor LocalStack publishes that metric, so
no end-to-end test could have caught it. An explicit unit test now pins the
two-dimension call.

The port also mandates graceful degradation: a `ClientError` on any listing or
metric call logs a warning and returns empty. A metrics outage must never fail
a scan.

### `FindingsRepository` (`ports/repository.py`)

Persistence for resources, findings, decisions, notifications, remediations,
audit events, and spend snapshots.

The method that carries the concurrency guarantee:

```python
def transition_finding(self, finding_id, expected, new) -> bool:
    """UPDATE findings SET status=:new WHERE id=:id AND status=:expected.
    Returns True only if THIS call performed the transition (rowcount 1)."""
```

Compare-and-swap, not read-then-write. A Slack double-click, a race against the
expiry job, or two concurrent scans can never execute a remediation twice — the
second caller gets `rowcount 0` and returns `False`.

`save_finding()` explicitly never writes status, which is what makes terminal
states terminal.

`record_spend_snapshot()` upserts **by date**. Appending would let a day that
happened to be scanned five times weigh five times as much in the anomaly
baseline — the z-score would measure scan cadence rather than spend.

### `Notifier` (`ports/notifier.py`)

Outbound alerts and inbound decision callbacks. Speaks domain language only;
webhooks, signatures, and Block Kit stay in the adapter.

`send_finding_alert()` **raises on delivery failure** — deliberately. The caller
leaves the finding `OPEN` and retries next scan. Swallowing the error would
lose the alert silently.

`send_digest(title, sections)` carries a contract in its docstring:
implementations **must not** attach approve/deny affordances. A digest reports
a pattern; there is no finding id for a decision to act on.

### `Advisor` (`ports/advisor.py`)

Two methods, one contract: **implementations must never raise.**

```python
def summarize(self, finding, resource) -> str   # explain one finding
def narrate(self, topic, facts) -> str          # prose for a digest section
```

The never-raise rule is in the port, not in each adapter, because a summary is
a nice-to-have and a notification is not. A dead LLM backend must not block the
pipeline.

`narrate` takes **already-computed facts**. The advisor narrates; it does not
compute. Every number was derived deterministically in the domain precisely so
a model cannot change what the system concluded — only how it reads.

### `Pricing` (`ports/pricing.py`)

Turns a resource's shape into an estimated monthly cost. Two constraints on
every implementation:

- **Never raise.** An unknown instance type falls back to a documented estimate.
  A finding with an approximate cost is useful; a scan that dies is not.
- **Never return zero as an "unknown" marker.** Zero reads as "free" in the
  savings total and would quietly hide waste.

Every method takes `region` even though the static adapter prices everything at
us-east-1 rates — so a live pricing adapter can be dropped in without touching
a single scanner signature.

`rightsizing_candidates()` supplies *what is cheaper*, never *whether to
recommend it*. The utilisation judgement lives in `domain/rightsizing.py`, so a
price-table edit can never change what counts as over-provisioned.

### `Scanner` (`ports/scanner.py`)

The two-pass contract:

```python
def discover(self, gateway) -> list[tuple[Resource, dict[str, Any]]]
def evaluate(self, resources) -> list[Finding]
```

`discover` returns the domain `Resource` **and** the raw provider dict, so
`evaluate` can read provider-specific fields without the domain model growing a
field for every AWS attribute.

Scanners that need CloudWatch fetch it during `discover` and cache it on
`self`, keeping `evaluate` pure. That caching is also why **scanners must never
be shared between regions** — each stamps its own region onto every resource,
and the cached series belong to one region's instances.

---

## 6. The adapters, one by one

### 6.1 `Boto3Gateway` (`adapters/aws/gateway.py`)

Implements `CloudGateway`. Holds four boto3 clients (`ec2`, `cloudwatch`,
`rds`, `s3`), all honouring `AWS_ENDPOINT_URL` so LocalStack and real AWS are
interchangeable with no code change.

Paginated everywhere it matters. `ClientError` is caught per-operation and
degrades to an empty result.

**S3 needs special handling.** `describe_s3_buckets()` resolves each bucket's
home region and skips buckets outside its own — bucket names are global but
each bucket lives in one region, so without this a three-region scan reports
every bucket three times. It also folds lifecycle, versioning, and tags into
one dict so scanners make one call rather than four per bucket. Each of those
lookups 404s on a bucket that simply has none configured; that is S3's normal
way of saying "not set," so each is caught individually.

`_bucket_region()` handles a documented S3 quirk: us-east-1 reports its
location constraint as `None`.

**The five playbooks:**

| Playbook | Behaviour |
|---|---|
| `release_eip` | Release the address |
| `terminate_stopped_instance` | Terminate |
| `snapshot_then_delete_volume` | Create snapshot → **wait for completion** → delete |
| `delete_ebs_snapshot` | Delete |
| `abort_incomplete_multipart_uploads` | Re-check age per upload, abort only the stale ones |

`snapshot_then_delete_volume` waits on the snapshot before deleting. That
waiter is the recovery path — deleting first and hoping the snapshot lands
would make the operation irreversible on failure.

`abort_incomplete_multipart_uploads` **re-checks upload age at execution
time**, not just at detection:

```python
cutoff = datetime.now(UTC) - timedelta(days=self.mpu_age_days)
for upload in self.get_incomplete_multipart_uploads(bucket):
    if initiated > cutoff:
        skipped += 1     # a client started this AFTER the alert went out
        continue
```

An approval can sit in Slack for hours. Between detection and the click, a
client may have begun a legitimate large upload. The result dict reports
`aborted`, `bytes_reclaimed`, and `skipped_too_recent` so the audit trail
records what actually happened rather than what was intended.

`dry_run` is checked once, centrally, before any playbook dispatch.

### 6.2 `StaticPricing` (`adapters/aws/pricing.py`)

Every rate in the system lives in this one file, with sources cited. Scanners
hold no prices of their own, so updating a rate is a one-line edit.

Three caveats are documented at the top of the file because they bound how far
the numbers can be trusted:

1. Other regions cost more; the table applies us-east-1 rates everywhere and
   logs a warning once per region.
2. List price is not your price — Reserved Instances and Savings Plans reduce
   it, and a commitment-covered instance saves nothing when deleted.
3. Snapshots bill on incremental blocks, so snapshot estimates are upper bounds.

**Defaults are mid-range, never cheapest.** `DEFAULT_EC2_HOURLY` is `t3.medium`.
An unknown type must not be dismissed as negligible, but must not manufacture
savings that dwarf real findings either.

**Engine multipliers** exist because commercial database engines carry licence
fees on identical hardware. Pricing a SQL Server instance as if it were
PostgreSQL would bury the single most expensive finding in the report.

**The right-sizing candidate table** encodes the safety rule from §4.4:

```python
RIGHTSIZING_CANDIDATES = {
    "m5.xlarge": ("m5.large", "m6g.xlarge", "m6g.large"),
    ...
}
```

At most one size step down, plus the same-size Graviton swap. A type not in the
table yields no suggestion — a type the file cannot price is one it has no
business recommending a replacement for.

### 6.3 The six scanners

| Scanner | Rules | Notes |
|---|---|---|
| `UnattachedEBSScanner` | `ebs_unattached` | State-based |
| `OrphanedEIPScanner` | `eip_orphaned` | State-based |
| `StoppedEC2Scanner` | `ec2_stopped` | Parses stop time out of `StateTransitionReason` with a regex — AWS exposes it nowhere else. Prices by summing the instance's real EBS volumes from pass-1 inventory, falling back to an assumed 8GB root volume and recording which basis it used in `evidence.cost_basis` |
| `OldEbsSnapshotScanner` | `ebs_old_snapshot` | Age **or** orphaned source volume |
| `IdleEC2Scanner` | `ec2_idle` | Requires CPU **and** network both below threshold |
| `IdleRDSScanner` + `StoppedRDSScanner` | `rds_idle`, `rds_stopped` | Both discover every instance; each filters in `evaluate` |
| `S3LifecycleScanner` | `s3_no_lifecycle`, `s3_incomplete_multipart` | One scanner, two rules of deliberately different power |

**Why RDS scanners both discover everything.** The upsert is keyed on
`resource_id`, so the duplicate is idempotent — and it keeps each scanner
independently correct. An instance in a transient state (`backing-up`,
`modifying`) still lands in the inventory instead of falling through the gap
between two filters and being swept up as `DELETED`.

**Why `rds_idle` reads connections, not CPU.** A database nobody connects to is
serving nobody. A quiet CPU can still be a replica or a nightly-batch target.

**Why `rds_stopped` has no grace period.** AWS auto-restarts a stopped RDS
instance after 7 days and bills allocated storage the whole time. There is no
"stopped long enough to be abandoned" window that makes sense — the state
itself is the finding. A `RDS_STOPPED_THRESHOLD_DAYS` knob was planned and
**dropped during implementation**: `DescribeDBInstances` exposes no
stopped-since timestamp, so "stopped for N days" is not a question the API can
answer, and shipping the knob would have shipped a lie.

**Why unknown bucket size produces no finding.** S3 has no size API; size comes
from the daily `BucketSizeBytes` CloudWatch metric. No datapoints means
unknown, and unknown is not evidence. The alternative — a bounded
`ListObjectsV2` walk — was explicitly rejected: it is a workaround for an
emulator limitation that would then run against real production buckets.

### 6.4 `SqlAlchemyRepository` (`adapters/persistence/`)

Implements `FindingsRepository` over SQLite. Covered in [§7](#7-database-design).

One adapter-level detail worth surfacing: an un-migrated database raises a raw
`IntegrityError` from the CHECK constraint, which tells nobody what to do. The
adapter catches it and re-raises with instructions:

```python
raise RuntimeError(
    f"The findings database does not allow resource type "
    f"'{resource.resource_type}'. It was created by an older version. "
    f"Run `alembic upgrade head` against {self.db_url} and scan again."
)
```

Failing hard here is correct — a half-written inventory would let the DELETED
sweep disarm findings the failed pass never reached. Only the message changed.

### 6.5 `SlackAdapter` (`adapters/notifications/slack.py`)

**Outbound.** Block Kit messages. The header differs by rule class, and
critically, the **actions block is conditional**:

```python
if remediable:
    blocks.append({"type": "actions", "elements": [approve_button, deny_button]})
else:
    blocks.append({"type": "context", "elements": [advisory_note]})
```

Advisory findings render with no buttons at all, rather than a button the
domain would refuse. Offering an action the system will reject is a worse
experience than offering none.

LLM output goes in its own `context` block and is **never used to build action
values**. Buttons carry `approve_{finding_id}` — ids the system generated, not
model output.

**Inbound.** `parse_callback()` verifies the Slack signature before parsing
anything:

- missing headers → `PermissionError`
- timestamp older than 5 minutes → `PermissionError` (replay defence)
- bad HMAC → `PermissionError`

FastAPI maps `PermissionError` to 401 and `ValueError` to 400. All Slack-specific
knowledge — form encoding, `response_url`, payload shape — stays inside this
adapter.

`confirm_decision()` edits the original message, keeping the alert text and
dropping the buttons so a decided finding cannot be clicked again.

### 6.6 `OllamaAdvisor` (`adapters/advisor/ollama.py`)

Talks to a local Ollama daemon. Ollama runs **natively on the host**, not in a
container: Docker on Apple Silicon has no GPU passthrough, so a containerized
Ollama would be CPU-only.

**The trust model is stated in the module docstring**: the prompt embeds cloud
metadata (tags, resource ids) that anyone with tagging permission can write, so
generated text is treated as untrusted display copy. It is shown to operators
and never parsed for decisions.

Four defences:

1. **An evidence allowlist.** Only ~12 named keys ever reach the prompt.
   Evidence is a raw cloud API dict and can be enormous.
2. **Field relabelling.** `observation_days` became "running for 14 days" in
   model output, so it is renamed to `metric_window_days` **in the prompt only**
   — stored evidence keeps the scanner's original keys.
3. **Structured output.** The Pydantic schema is handed to Ollama's `format`
   field *and* used to validate the reply, so a model that ignores the
   constraint is caught rather than trusted.
4. **Think-tag stripping.** Reasoning models wrap output in `<think>` blocks
   even with thinking disabled.

`_request()` is generic over prompt and schema (`_ResponseT` TypeVar), so the
format sent to Ollama and the validation applied to its reply cannot drift
apart.

Two entry points, deliberately:

- `summarize()` — never raises, degrades to template on any failure.
- `advise()` — the strict path that raises. It exists so `sentinel smoke-llm`
  can tell a real success from a silent fallback; through `summarize()` the two
  are indistinguishable.

### 6.7 Inbound adapters

**`cli.py`** (Typer + Rich) — six commands: `scan`, `digest`, `regions`,
`serve`, `smoke-llm`, `expire`.

The scan table is four columns, not seven. Rich shares width by content length,
and resource ids are the longest thing present, so an unconstrained layout
starved the savings and status columns to zero characters — losing the only
numbers anyone runs the tool for. Resource type was dropped (the rule name
implies it) and protection became a `[P]` marker on the id.

Failed scanners are grouped by error message and truncated to 140 characters:
one unavailable service produces an identical error for every scanner in every
region, which is six paragraphs of AWS prose for a single cause.

**`fastapi_app.py`** — six routes, all thin wrappers over domain services:
`GET /health`, `GET /findings?status=`, `GET /resources`, `GET /audit`,
`POST /decisions/{id}`, `POST /callbacks/{channel}`.

`_refusal_reason()` distinguishes a notify-only refusal, and an unauthorized
actor, from a generic one — those are the two refusals a user can trigger
deliberately and cannot guess from a generic message.

`POST /decisions/{id}` remediates inline; `POST /callbacks/{channel}` does not,
because it answers a channel with a three-second budget (§8.2).

---

## 7. Database design

SQLite via SQLAlchemy. Seven tables, all schema changes through Alembic.

### Entity relationships

```
RESOURCES ──1:N──▶ FINDINGS ──1:N──▶ DECISIONS
                       │
                       ├──1:N──▶ NOTIFICATIONS
                       ├──1:N──▶ REMEDIATIONS
                       └──1:N──▶ AUDIT_EVENTS

SPEND_SNAPSHOTS  (standalone; one row per day)
```

### Key design decisions

**Money is never a float.** A custom `SafeNumeric` TypeDecorator stores
`Decimal` as a string, because SQLite has no native decimal type and a float
column would silently round money.

```python
class SafeNumeric(TypeDecorator[Decimal]):
    impl = String
    def process_bind_param(self, value, dialect):   return str(value) if value is not None else None
    def process_result_value(self, value, dialect): return Decimal(value) if value is not None else None
```

**CHECK constraints are generated from the StrEnums**, so the database and the
domain cannot drift:

```python
def _in_clause(column: str, values: type[StrEnum]) -> str:
    members = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({members})"
```

Only the Alembic migration is hand-written.

**Finding ids are composite and stable**: `rule|resource_id`. A re-detected
finding updates in place rather than duplicating, and the id is human-readable
in logs and Slack payloads.

**Satellite tables are append-only.** Decisions, notifications, remediations,
and audit events are never overwritten — latest wins on read. A dry-run and a
real execution both produce remediation rows, so the history shows what was
attempted, not just what succeeded.

**`spend_snapshots.snapshot_date` is UNIQUE**, which is what makes the upsert an
upsert. Without it, scan cadence would decide how much each day weighs in the
anomaly baseline.

### Migration chain

Alembic revision IDs are hex, and order comes from `down_revision` pointers,
not filenames:

```
c447645246ac  initial_schema                   (down_revision = None)
      ↓
08872b80af2e  notifications_and_remediations
      ↓
a1c9f4d27b13  add_rds_and_s3_resource_types
      ↓
b7e2f81c4a90  add_spend_snapshots              (head)
```

**Migration `a1c9f4d27b13` is the interesting one.** SQLite cannot `ALTER` a
constraint, so widening the `resource_type` CHECK requires
`batch_alter_table(recreate="always")` — copy the table, rebuild it, swap it in.
Two traps were hit here:

1. **`table_args` *adds to* a reflected definition rather than replacing it.**
   The first version rebuilt `resources` carrying *both* the old four-value
   CHECK and the new six-value one — and the old one still rejected every new
   row. It ran without error either way; only dumping the resulting schema
   revealed it. The fix is `copy_from` with an explicit columns-only table
   definition, which suppresses reflection entirely.
2. **`copy_from` also suppresses index reflection**, so the unique index on
   `resource_id` has to be re-created by hand. `upsert_resource`'s
   insert-or-refresh logic depends on that uniqueness.

Its `downgrade()` deliberately **fails** if any `rds_instance` or `s3_bucket`
row exists. Silently deleting inventory rows to make a downgrade succeed would
orphan every finding referencing them.

---

## 8. Execution flows end to end

### 8.1 A scan

```
sentinel scan
  │
  ├─ bootstrap.get_scan_targets()          one gateway + 8 scanners per region
  │                                        (built eagerly — boto3 client
  │                                         construction is not thread-safe)
  ├─ run_scan(targets, repo)
  │    │
  │    ├─ PASS 1 (threaded, per region)
  │    │    └─ for each scanner: discover(gateway)
  │    │         ├─ success → resources
  │    │         └─ raise   → recorded in failures{}, others continue
  │    │
  │    ├─ if ALL scanners failed in a region → region marked failed
  │    ├─ if ALL regions failed              → RuntimeError
  │    │
  │    ├─ upsert_resource() for everything discovered
  │    ├─ mark_unseen_resources_deleted(scoped to succeeded regions)
  │    │
  │    ├─ PASS 2 (per region, skipping blind scanners)
  │    │    └─ evaluate(inventory) → findings → save_finding()
  │    │
  │    ├─ record_spend_snapshot()          today's estimated waste
  │    └─ audit "scan_completed"
  │
  ├─ notify_open_findings(repo, notifier, advisor, budget)
  │    └─ per finding, most expensive first:
  │         ├─ skip if protected
  │         ├─ advisor.summarize() (within budget) or template
  │         ├─ notifier.send_finding_alert()
  │         ├─ CAS OPEN → NOTIFIED
  │         └─ record_notification() + audit
  │
  └─ render the Rich table
```

### 8.2 An approval, from click to deleted volume

```
Slack "Approve" click
  │
  ▼
POST /callbacks/slack                     (FastAPI)
  │
  ├─ notifier.parse_callback(raw_body, headers)
  │    ├─ verify signature      → 401 if bad
  │    ├─ verify timestamp < 5m → 401 if stale
  │    ├─ verify team / channel → 401 if another install
  │    └─ payload → Decision(finding_id, actor, action)
  │
  ├─ _finding_region()                    resolved BEFORE remediation, because
  │                                       success may delete the resource row
  ▼
commit_approval(...)                      (domain) — no cloud call, milliseconds
  ├─ guardrail chain (§4.3) — any failure audits and returns None
  ├─ CAS NOTIFIED → APPROVED              already decided → None
  ├─ record_decision() + audit
  └─ ApprovalPlan(finding_id, resource_id, region, playbook)
  │
  ▼
notifier.confirm_decision("⏳ running …")  edits the message, DROPS THE BUTTONS
  │                                       — sent while the finding is already
  │                                       APPROVED, so a replay finds no work
  ▼
HTTP 200 to Slack                         inside the 3s budget
  │
  ▼
execute_approval(plan, …)                 (background task)
  ├─ gateway = gateway_for_approval(plan)              inside try
  │    └─ assume-role mode: sts:AssumeRole as the approver, session policy
  │       scoped to this one resource, 15-minute credentials
  ├─ gateway.execute(playbook, resource_id, dry_run)
  │    │
  │    ├─ dry_run → log, return {"dry_run": True}, stay APPROVED
  │    │
  │    └─ live → snapshot_then_delete_volume:
  │              create_snapshot → wait for completion → delete_volume
  │              (minutes — the reason this half is not in the request)
  │
  ├─ record_remediation(result, detail, timings)
  ├─ CAS APPROVED → REMEDIATED
  └─ audit "remediation_executed"
  │
  ▼
notifier.confirm_decision(outcome)        edits the message a second time
```

A denial takes the short path: one state change, no cloud call, so it resolves
and replies inside the request.

### 8.3 A digest

```
sentinel digest
  ├─ detect_spend_anomaly(repo, ...)
  │    ├─ get_spend_snapshots(since = today - window - 1)
  │    └─ detect_anomaly() → SpendAnomaly | None
  │
  ├─ build_rightsizing_digest(targets, pricing, ...)
  │    └─ per region (failures recorded, not fatal):
  │         └─ per running, non-protected instance:
  │              ├─ get_metric_averages(AWS/EC2, {InstanceId}, CPUUtilization)
  │              ├─ pricing.ec2_instance_monthly() + rightsizing_candidates()
  │              └─ suggest_rightsizing()  → peak-CPU rule
  │    → RightsizingReport(suggestions, regions_failed, instances_examined)
  │
  ├─ compose_digest_sections(report, anomaly, advisor)
  │    ├─ anomaly section leads (time-sensitive)
  │    ├─ right-sizing section (or an explicit "nothing found, N examined")
  │    └─ "Incomplete coverage" section if any region failed
  │
  └─ send_digest() → notifier.send_digest() → audit "digest_sent"
```

---

## 9. The safety model

The system deletes cloud resources. Five independent layers stand between a
detection and a deletion, and **no single failure removes more than one**.

### Layer 1 — Tag protection

`finops:protected=true` on any resource. Checked at detection (stamped onto the
finding), and re-checked at approval time against current tags. The newer
intent wins.

### Layer 2 — The playbook allowlist

Keyed by resource type. No entry, no action, no exceptions. `RDS_INSTANCE` has
no entry at all, so no RDS remediation can execute even if every other check
were bypassed.

### Layer 3 — Notify-only rules

Rule-level, catching what the type-level allowlist cannot: two rules on one
resource type where only one is actionable. This is what stops an `ec2_idle`
finding on a *running* instance from inheriting `terminate_stopped_instance`.

### Layer 4 — Human approval

Nothing is ever remediated automatically. Every playbook execution traces back
to a click or an API call by a named actor, recorded in `decisions` — and that
actor must appear in `SENTINEL_APPROVERS` (the `Authorizer` port), or the
approval is refused and audited as `approve_blocked_unauthorized`. Naming the
actor and permitting the actor are separate questions, and only the second one
stops a click.

With `SENTINEL_ASSUME_ROLE=true` the permission is enforced by AWS rather than
by Sentinel. Sentinel's own role holds no destructive verb at all; each approval
assumes an approver role via `sts:AssumeRole` with an inline session policy
narrowed to the one resource that approval named, so the deletion is authorized
against a principal CloudTrail can attribute to a person, and the session cannot
touch anything else in the account. `adapters/aws/approval_credentials.py`,
policies in `docs/iam-policies.md`.

The limit, stated because "IAM enforces it" implies more than is true: the
actor→role mapping is Sentinel *asserting* an identity from the Slack payload.
AWS enforces what the session may do; it never sees the Slack user. The chain is
as strong as the Slack account plus signature verification in front of it.

### Layer 5 — `DRY_RUN`

Default `true`. Playbooks log exactly what they would do and return
`{"dry_run": True}`. The finding stops at `APPROVED` and never reaches
`REMEDIATED`, so the status itself records that nothing changed.

### Supporting properties

- **Atomic CAS transitions against a literal expected status** — concurrent
  clicks resolve to one winner, and a replayed one (double-click, Slack retry)
  finds the finding out of `NOTIFIED` and updates zero rows.
- **Recovery paths built into destructive playbooks** — volumes are snapshotted
  and the snapshot is *waited on* before deletion.
- **Execution-time re-validation** — the multipart abort re-checks upload age at
  execution, not just detection.
- **Additive-not-destructive S3 remediation** — aborting an incomplete upload
  deletes no object, because none was ever created.
- **Complete audit trail** — every refusal has its own event name
  (`approve_blocked_protected`, `approve_blocked_notify_only`,
  `approve_blocked_no_playbook`, `approve_blocked_resource_gone`,
  `approve_blocked_unauthorized`), so "why didn't it act?" is answerable from
  the database — including for the attempts nobody was permitted to make.

### Trust boundary for LLM output

The advisor sees user-controlled data (tags). Its output is therefore treated
as untrusted display copy: rendered in its own Slack block, never parsed, never
used to build action values, never able to influence a decision. Buttons carry
system-generated finding ids.

---

## 10. Cost estimation

Every rate lives in `adapters/aws/pricing.py` with its source cited. What a
finding reports is **the spend that stops if the resource goes away** — scanners
add nothing and subtract nothing.

Special cases worth knowing:

- **`ec2_stopped`** prices only still-billing EBS volumes. Compute genuinely is
  not billed while stopped. When the real volumes were found in inventory,
  `evidence.cost_basis` says `attached EBS volumes`; otherwise it says
  `assumed root volume` and uses an 8GB gp3 default.
- **`rds_stopped`** prices **storage only**, for the same reason.
- **`rds_idle`** prices compute × engine multiplier, plus storage.
- **`s3_no_lifecycle`** reports `bucket_cost × S3_LIFECYCLE_ADDRESSABLE_FRACTION`
  (default 0.20). A bucket without a policy is not 100% waste, and reporting
  full storage cost as "potential savings" would let one large bucket dominate
  the total and make every number in the tool untrustworthy. The fraction, the
  raw bucket cost, and the raw size all go into `evidence` so the estimate is
  auditable rather than magic.
- **`s3_incomplete_multipart`** is exact — the summed byte size of parts already
  uploaded.

A floor of `$0.01` applies where quantizing would round a real cost to zero,
because zero reads as "free."

---

## 11. Configuration and composition

### Settings

`config.py` defines 34 settings via pydantic-settings, resolved
**environment variable → `.env` → default**. `.env.example` documents every one
with a comment and a safe default, and is verified in lockstep with `config.py`.

Prices are deliberately **not** configurable — one file owns them, with sources.

### The database URL has exactly one owner

```python
def database_url() -> str:
    return f"sqlite:///{settings.sentinel_db_path}"
```

Both `bootstrap.get_repository()` and `alembic/env.py` call this function.
`alembic.ini` sets no `sqlalchemy.url` on purpose — a value there reads as
authoritative and would be silently ignored. This single-owner rule exists
because of a real defect; see [§14](#14-defects-found-by-running-it).

### Region resolution

`AWS_REGIONS` accepts a comma-separated list or the literal `all`. Resolving
`all` needs a live `ec2:DescribeRegions` call, so it happens in `bootstrap`,
not `config`. If that call fails the scan still runs — but only over the home
region, and the failure is logged at **ERROR**, because a silent narrowing to
one region looks exactly like an account with nothing to find.

Opt-in regions the account never enabled are excluded; every call against one
fails with `AuthFailure`, which would turn a full-account scan into a wall of
failed regions.

### Scanner lifecycle

`get_scanners(region)` builds a **fresh set per region**. Never share them: each
stamps its region onto every resource it discovers, and the idle scanners cache
metric series on themselves between `discover()` and `evaluate()`.

`get_scan_targets()` builds everything eagerly on the calling thread, because
boto3 client construction is not thread-safe and `run_scan` discovers regions
in parallel.

---

## 12. Testing strategy

**277 tests. Global coverage 96% (gate 90), `domain/` 100% (gate 95).**

### Layered by what each layer can prove

| Layer | Tooling | What it proves |
|---|---|---|
| Domain | In-memory fakes (`tests/fakes.py`) | Guardrails, state machine, pure rules — no AWS at all |
| Adapters | `moto` | Scanners against a simulated AWS API |
| LLM | `respx` | Every advisor failure mode: timeout, transport error, bad JSON, schema violation |
| Integration | LocalStack | Seed → scan → approve → resource actually gone → audit complete |

### The split coverage gate

Two gates rather than one number:

```bash
pytest tests/ --cov=src/finops_sentinel --cov-fail-under=90
coverage report --include='*/domain/*' --fail-under=95
```

A flat 90% would let `domain/` rot to 85% as long as adapters compensated —
backwards, since the domain is the part that must never break. A flat 95%
global would push toward thin mock-boto3 tests written purely to move a number.

**`ports/` is deliberately excluded** from the domain gate. Its abstract bodies
are `...` under `# pragma: no cover`, so the only statements left are imports
and `def` lines, which execute at class-definition time. It reports 100%
without a single test and would pad the denominator by ~71 statements.

### The swap-proof test

The whole approve-and-remediate flow runs with `FakeNotifier` +
`FakeCloudGateway` + in-memory repository. Zero Slack, zero AWS, zero HTTP. If
it passes, a Telegram adapter cannot break the business logic.

### Conventions learned the hard way

- **Run as `.venv/bin/pytest`, never `python -m pytest`.** The latter puts the
  working directory on `sys.path`; CI's bare `pytest` does not. That difference
  alone broke CI on five modules while everything passed locally.
  `pythonpath = ["."]` now makes them equivalent.
- **Shared doubles live in `tests/fakes.py`, never `conftest.py`.** pytest
  imports conftest itself; importing it from a test module can load it twice
  under two names, producing two distinct copies of the same class.
- **`FakeGatewayBase` raises on every un-stubbed method**, so a scanner that
  reaches for a port it should not touch fails loudly instead of quietly
  receiving an empty list.

---

## 13. Engineering decisions and their trade-offs

### 13.1 Two-pass scanning instead of one

**Chosen because** evaluation sometimes needs inventory a scanner did not
discover — `ec2_stopped` prices itself from volumes another scanner found.
**Cost:** all inventory is held in memory during a scan.

### 13.2 RDS is doubly gated, with no playbook at all

**Chosen because** deleting a database is the highest blast-radius action in
reach. "Delete with final snapshot" deserves its own phase and its own
guardrail tests, not a ride-along in a scanner PR.
**Cost:** RDS waste requires manual action.

### 13.3 S3 remediation is `abort_incomplete_multipart_uploads`, not "apply a lifecycle policy"

The spec called for applying a lifecycle policy. **Changed because** aborting an
incomplete upload deletes no object — none was ever created — so it preserves
the additive-not-destructive intent while being immediate, exactly measurable,
and verifiable end-to-end. A lifecycle rule takes up to 24 hours to act and is
therefore untestable in an integration suite.

### 13.4 Two S3 rules, not three

The third shape (noncurrent versions piling up) needs a full `ListObjectVersions`
walk — unbounded cost on a production bucket. **Folded into `s3_no_lifecycle`
evidence** instead: `versioning: Enabled` plus no `NoncurrentVersionExpiration`
rule is recorded as `noncurrent_versions_accumulating`.

### 13.5 Anomaly detection measures estimated *waste*, not billed *spend*

**Chosen because** there is no billing data in this system — Cost Explorer needs
a real account and bills per request. What is genuinely available daily is the
total `est_monthly_cost_usd` of live findings.
**Every user-facing string says so.** A Cost-Explorer-backed adapter can replace
the input later without the z-score logic changing at all — the port design
paying off, and a better README line than a wrong number.

### 13.6 stdlib `statistics`, not pandas

The spec said "deterministic pandas, in domain." **Changed because** the
import-linter contract is literally named *"Domain is pure Python (pydantic
only)"*, and a rolling mean/stdev/z-score is about twenty lines. Taking a 60MB
dependency to avoid writing them would make the architecture claim false.

### 13.7 The digest re-fetches metrics rather than persisting them

Right-sizing is about instances that are **not** idle, so no finding exists for
them and no finding carries their metrics. The alternatives were a
`metric_summaries` table written on every scan, or one extra CloudWatch pass on
a weekly digest. **Chose the pass:** `GetMetricStatistics` is $0.01/1000
requests; a table is a schema and a migration forever.

### 13.8 A generic `get_metric_averages` instead of one method per service

RDS metrics live in `AWS/RDS` with a different dimension name than EC2. One
method per service does not scale. **Cost:** callers must know their namespace
and dimensions — accepted, and documented in the port with a table of the three
real combinations.

### 13.9 Emitting findings for protected resources

Protected resources still produce findings; they are simply never notified and
never actionable. **Chosen because** visibility and action are different
questions. An operator should be able to see that a protected volume costs
$200/mo and decide to change the tag.

### 13.10 The type-keyed playbook allowlist stays (for now)

Rule-keying would be more precise. **Deferred because** type-keying only truly
breaks when two rules on one type need *different* playbooks, which has not
happened — `NOTIFY_ONLY_RULES` covers the S3 near-miss. The pressure is
recorded in a code comment so the next person hits it prepared.

### 13.11 Anything the free emulator cannot exercise is skipped and recorded, never worked around

LocalStack's RDS is Pro-tier. **Decision:** ship RDS with full moto unit
coverage, no integration test, and record the gap in two places — a
`pytest.mark.skip` carrying the reason (so it appears in test output) and a
README section. This policy is what killed the proposed `ListObjectsV2` bucket
size fallback: an emulator limitation must not leak into production code paths.

---

## 14. Defects found by running it

Recorded because each one argues for verifying against a live environment
rather than trusting a green suite.

**1. `run_scan` isolated failures per region but not per scanner.**
Adding RDS broke `sentinel scan` outright on LocalStack: RDS raised, the region
was marked failed, and with one region configured the whole scan aborted. Every
other scanner's findings vanished. On real AWS a single missing IAM grant does
the same — and "no findings" is indistinguishable from a clean account.
*Fixed:* per-scanner isolation, `ScanResult.scanners_failed`, audited and
surfaced in the CLI.

**2. `get_metric_averages` took a single dimension name/value pair.**
S3's `BucketSizeBytes` is published against `BucketName` **and** `StorageType`,
and CloudWatch matches dimension sets exactly. Every bucket would have read as
"size unknown" against real AWS and no finding would ever have fired. **Neither
moto nor LocalStack publishes that metric**, so no end-to-end test could have
caught it — empty is also what "no data" legitimately looks like.
*Fixed:* the port takes a dimension map; an explicit test pins the two-dimension
call.

**3. The migration silently kept the old CHECK constraint.**
`table_args` *adds to* a reflected table definition rather than replacing it, so
the rebuilt `resources` table carried both the old four-value constraint and the
new six-value one — and the old one still rejected every new row. It ran without
error either way; only dumping the schema revealed it.
*Fixed:* `copy_from` with an explicit columns-only definition.

**4. The digest reported "nothing over-provisioned" when every region had failed.**
`build_rightsizing_digest` swallowed region failures into a log line. With all
three regions unreachable it printed a clean result — the exact lie
`run_scan.regions_failed` exists to prevent, reintroduced one command over.
*Fixed:* `RightsizingReport(suggestions, regions_failed, instances_examined)`,
a red CLI warning, and an "Incomplete coverage" section in the message.

**5. Alembic migrated a different database file than the app used.**
`alembic/env.py` read `SENTINEL_DB_PATH` with `os.getenv` and its own
`.sentinel.db` default, while the app read the same setting through
pydantic-settings — which also reads `.env`. Since the documented setup puts
`SENTINEL_DB_PATH=data/sentinel.db` in `.env` and not the shell, **every
migration ever run landed on the wrong file.** It caused no visible symptom for
two phases because earlier migrations only had to exist for tables
`create_all` already made; the first migration whose table nothing else creates
surfaced it immediately.
*Fixed:* `config.database_url()` is the single owner, called by both.

**6. Host and container silently used two different databases.**
Same class of bug: `SENTINEL_DB_PATH` plus the `./data:/app/data` bind mount
must agree, or every Slack approval fails its finding lookup.

---

## 15. Known limitations

**Cost accuracy.** List prices only. Reserved Instances, Savings Plans, and
enterprise discounts all reduce real spend, and a commitment-covered instance
saves nothing when deleted. Non-us-east-1 regions are under-estimated.

**No RDS end-to-end coverage.** LocalStack RDS is Pro-tier. Unit-tested with
moto; the integration gap is recorded with a skip and a reason.

**`s3_incomplete_multipart` is hard to exercise locally.** moto stamps uploads
with a fixed 2010 date, and LocalStack's seeded upload is minutes old against a
7-day threshold. The age re-check is tested by moving the threshold instead.

**Anomaly detection needs seven days of scan history** before it says anything.
Correctly reports "too little history to judge" rather than "no anomaly" — the
distinction matters, because only one of those is reassuring.

**SQLite.** Single-writer. Fine for one scanner and one API process; a
multi-writer deployment wants Postgres. The repository is a port, so that is an
adapter swap.

**No digest schedule.** `sentinel digest` runs on demand; the weekly cadence is
a Kubernetes CronJob in the next phase.

**Slack signature verification is bypassed when no secret is configured** — a
deliberate local-testing affordance. Set `SLACK_SIGNING_SECRET` in any
environment reachable from the internet.

---

## 16. Extending the system

### Adding a detection rule

1. Write a scanner in `adapters/aws/scanners/` implementing `discover` /
   `evaluate`.
2. Add copy to `domain/summaries._RULE_COPY`.
3. If it is inferred or fractional, add it to `NOTIFY_ONLY_RULES`.
4. If it needs a new `ResourceType`, add the enum member **and** an Alembic
   migration widening the CHECK constraint.
5. Register it in `bootstrap.get_scanners()`.
6. Add thresholds to `config.py` **and** `.env.example`.
7. moto tests in the same commit as the code.

### Adding a remediation playbook

1. Implement it in `Boto3Gateway.execute`'s dispatch dict.
2. Add the `PLAYBOOK_ALLOWLIST` entry — without it the domain refuses.
3. Honour `dry_run`.
4. Prefer additive or reversible operations; if destructive, build the recovery
   path into the playbook and wait for it to complete.
5. Re-validate preconditions at execution time, not just detection time.

### Swapping an adapter

| Swap | Work required |
|---|---|
| Slack → Telegram | One `Notifier` implementation; `bootstrap.get_notifier()` |
| Ollama → hosted LLM | One `Advisor` implementation; one `ADVISOR_PROVIDERS` entry |
| SQLite → Postgres | One `FindingsRepository` implementation; connection string |
| Static → live pricing | One `Pricing` implementation; `bootstrap.get_pricing()` |
| AWS → GCP | One `CloudGateway` + new scanners; domain untouched |

In every case the domain does not change, and the swap-proof test proves it.
