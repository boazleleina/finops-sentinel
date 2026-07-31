# Hexagonal Architecture: What It Actually Buys You (And What It Costs)

*Ports and adapters, explained through a system that deletes AWS resources for a living.*

---

There is a particular kind of dread that comes with opening a codebase where the
business logic is tangled up with the framework. You want to change how a
discount is calculated, and you find the calculation inside a Django view,
reading from a `request` object, writing to an ORM model, three layers deep in
HTTP concerns. The logic is *in there*, somewhere, but you cannot test it
without spinning up a web server and a database.

Hexagonal architecture — also called **ports and adapters** — is one answer to
that. Alistair Cockburn named it in 2005, and the goal he stated is worth
quoting because people usually paraphrase it into something weaker:

> Allow an application to equally be driven by users, programs, automated test
> or batch scripts, and to be developed and tested in isolation from its
> eventual run-time devices and databases.

Note what that is really saying. Not "layers are good." Not "abstract your
database." It says the application should not be able to tell the difference
between a real user and a test harness — because if it can't tell, then testing
it is trivial and swapping its surroundings is safe.

I'll use a real system throughout: [FinOps Sentinel](https://github.com/boazleleina/finops-sentinel),
an agent that scans AWS accounts for wasted spend and deletes resources after a
human approves in Slack. It's a good example precisely because the stakes are
uneven — the business rules decide whether to **delete infrastructure**, and the
surrounding machinery is just AWS SDK calls and HTTP.

---

## The core idea

Draw your application as a hexagon. Inside: your business rules. Outside:
everything else — databases, HTTP APIs, message queues, cloud SDKs, the
filesystem, the clock.

```
                    ┌──────────────┐
      CLI ─────────▶│              │
                    │              │
   HTTP API ───────▶│    DOMAIN    │◀─────── Database
                    │              │
  Scheduler ───────▶│  (the rules) │◀─────── Cloud API
                    │              │
                    └──────────────┘◀─────── Notifications
      driving side                              driven side
```

Everything crossing that boundary goes through a **port**: an interface defined
by the domain, in the domain's vocabulary. An **adapter** is a concrete
implementation of a port.

The direction matters more than the diagram. Ports are declared *by the inside*
and implemented *by the outside*. This is dependency inversion applied
structurally: the domain does not ask "how do I talk to Postgres?" It declares
"I need somewhere to save findings," and something else volunteers.

### Driving vs driven

Two kinds of adapter, and conflating them is the most common source of
confusion:

- **Driving (primary) adapters** call *into* your application. A CLI, an HTTP
  controller, a cron job, a test. They translate the outside world's input into
  a domain call.
- **Driven (secondary) adapters** are called *by* your application. A database
  client, an SDK, an email sender. The domain defines what it needs; these
  supply it.

The asymmetry: a driving adapter *depends on* your domain, while a driven
adapter is *depended upon through an interface your domain owns*. Arrows point
inward on both sides.

---

## What a port actually looks like

Here's the cloud port from FinOps Sentinel. Notice what it is not:

```python
class CloudGateway(ABC):
    """Port for interacting with cloud provider APIs.
    The domain only knows about these abstract operations."""

    @abstractmethod
    def describe_ebs_volumes(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def describe_running_ec2_instances(self) -> list[dict[str, Any]]:
        """Instances in the running state — the candidates for idleness checks."""

    @abstractmethod
    def get_metric_averages(
        self,
        namespace: str,
        dimensions: dict[str, str],
        metric_name: str,
        days: int,
        period_seconds: int = 3600,
    ) -> list[float]: ...

    @abstractmethod
    def execute(self, playbook: str, resource_id: str, dry_run: bool) -> dict[str, Any]:
        """Execute a named remediation playbook against a resource."""
```

It is **not** `call_aws(service, operation, params)`. That would be a
passthrough — technically an interface, but it puts AWS concepts straight back
into the domain and buys nothing. A port describes *capabilities the domain
needs*, not *the API you happen to be wrapping*.

This is the single most common mistake I see. If your `UserRepository` has a
method called `execute_query(sql: str)`, you have not decoupled from the
database. You have written a database driver with extra steps.

A good test: **could you implement this port against a completely different
technology without the signature feeling absurd?** `describe_ebs_volumes()`
against GCP would be weird — but `get_metric_averages(namespace, dimensions,
...)` maps fine to Cloud Monitoring, and `execute(playbook, ...)` maps to
anything. Where the abstraction leaks, it leaks deliberately.

---

## Advantage 1: Tests that prove something

This is the payoff that justifies everything else.

FinOps Sentinel deletes cloud resources. The logic that decides *whether* a
deletion is allowed is the most safety-critical code in the system. Here is a
chunk of it:

```python
def approve_finding(
    finding_id: str,
    repo: FindingsRepository,
    gateway_for_region: Callable[[str], CloudGateway],
    actor: str,
    channel: str,
    dry_run: bool,
) -> bool:
    finding = repo.get_finding_by_id(finding_id)
    if finding is None:
        return False

    resource = repo.get_resource_by_id(finding.resource_ref)

    if resource.lifecycle == ResourceLifecycle.DELETED:
        _audit(repo, "approve_blocked_resource_gone", finding.id, {...})
        return False

    if finding.protected or rules.is_protected(resource.current_tags):
        _audit(repo, "approve_blocked_protected", finding.id, {...})
        return False

    if not rules.is_remediable(finding.rule):
        _audit(repo, "approve_blocked_notify_only", finding.id, {...})
        return False

    playbook = rules.PLAYBOOK_ALLOWLIST.get(resource.resource_type)
    if playbook is None:
        _audit(repo, "approve_blocked_no_playbook", finding.id, {...})
        return False

    if not repo.transition_finding(finding.id, finding.status, FindingStatus.APPROVED):
        return False  # lost the race — someone else already decided

    gateway = gateway_for_region(resource.region)
    result = gateway.execute(playbook, resource.resource_id, dry_run)
    ...
```

Every collaborator is a port. `repo` is a `FindingsRepository`.
`gateway_for_region` returns a `CloudGateway`. There is no `import boto3` in
this file, and there cannot be — CI forbids it.

So the entire approve-and-remediate flow can be tested like this:

```python
def test_approving_a_protected_finding_is_refused_and_audited(repository):
    resource = make_resource(tags={"finops:protected": "true"})
    repository.upsert_resource(resource)
    repository.save_finding(make_finding(status=FindingStatus.NOTIFIED))
    gateway = FakeCloudGateway()

    result = approve_finding(
        "f-mock", repository, resolver(gateway),
        actor="tester", channel="test", dry_run=False,
    )

    assert result is False
    assert gateway.executed == []          # nothing was deleted
    assert "approve_blocked_protected" in [e.event for e in repository.get_audit_events()]
```

**Zero AWS. Zero HTTP. Zero Slack. Runs in milliseconds.**

That test is not a mock-heavy simulation of the real thing — it *is* the real
thing. The same function runs in production; only the adapters differ. Which
means the test genuinely proves that a protected resource cannot be deleted.

In the tangled version of this codebase, proving that would require standing up
LocalStack, seeding a volume, tagging it, running a scan, and hoping nothing
else interfered. You'd write it once, it'd be slow and flaky, and eventually
someone would delete it.

The project's domain layer sits at **100% coverage** — not because anyone
chased a number, but because pure functions over fakes are cheap to cover
exhaustively.

---

## Advantage 2: Swapping infrastructure stops being scary

FinOps Sentinel runs against LocalStack (a local AWS emulator) in development
and real AWS in production. The code difference between those two environments
is **zero**. One environment variable:

```python
self.client = boto3.client(
    "ec2",
    region_name=region,
    endpoint_url=endpoint_url,     # None for real AWS, localhost:4566 for LocalStack
    ...
)
```

More interestingly, the LLM backend is swappable at runtime:

```python
# bootstrap.py — the ONLY file that knows which concrete adapters exist
ADVISOR_PROVIDERS: dict[str, Callable[[], Advisor]] = {
    "ollama": _build_ollama_advisor,
    "template": TemplateAdvisor,
}

def get_advisor() -> Advisor:
    return ADVISOR_PROVIDERS[settings.advisor_provider]()
```

Adding a hosted LLM means writing one adapter and adding one dict entry. No
caller changes, because no caller has ever seen anything but the `Advisor`
interface.

The honest version of this claim: **you still have to write the adapter.**
Hexagonal architecture doesn't make a Postgres migration free. What it does is
bound the work — the change is confined to one file plus a wiring line, and
you know that before you start, which is most of the value.

---

## Advantage 3: The domain becomes readable as a description of the business

When infrastructure is stripped out, what remains reads like documentation:

```python
NOTIFY_ONLY_RULES: frozenset[str] = frozenset(
    {"ec2_idle", "rds_idle", "rds_stopped", "s3_no_lifecycle"}
)

PLAYBOOK_ALLOWLIST: dict[ResourceType, str] = {
    ResourceType.EBS_VOLUME:   "snapshot_then_delete_volume",
    ResourceType.ELASTIC_IP:   "release_eip",
    ResourceType.EC2_INSTANCE: "terminate_stopped_instance",
    ResourceType.EBS_SNAPSHOT: "delete_ebs_snapshot",
    ResourceType.S3_BUCKET:    "abort_incomplete_multipart_uploads",
}
```

You do not need to know Python well to review that. A colleague can look at it
and say "wait, why can RDS not be remediated?" — which is exactly the
conversation worth having, and exactly the conversation that never happens when
the rule is buried in a service class behind three layers of DI configuration.

The state machine gets the same treatment — it lives next to the enum it
governs, as data:

```python
TRANSITIONS: dict[FindingStatus, set[FindingStatus]] = {
    FindingStatus.OPEN:     {FindingStatus.NOTIFIED},
    FindingStatus.NOTIFIED: {FindingStatus.APPROVED, FindingStatus.DENIED,
                             FindingStatus.EXPIRED},
    FindingStatus.APPROVED: {FindingStatus.REMEDIATED, FindingStatus.FAILED},
    # DENIED / REMEDIATED / FAILED / EXPIRED are terminal in v1
}
```

---

## Advantage 4: You can enforce it mechanically

Architecture that relies on discipline decays. Architecture that fails the build
does not.

FinOps Sentinel uses [import-linter](https://import-linter.readthedocs.io/) with
two contracts in `pyproject.toml`:

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
forbidden_modules = [
    "boto3", "botocore", "fastapi", "sqlalchemy", "alembic",
    "slack_sdk", "typer", "rich", "uvicorn", "httpx",
]
```

`lint-imports` runs in CI alongside ruff and mypy. Import boto3 into the domain
and the build goes red with a named contract violation.

This had a real consequence. The spec for the anomaly-detection feature said
"deterministic pandas, in domain." But the second contract is *literally named*
"Domain is pure Python (pydantic only)." Adding pandas would have made that name
a lie. A rolling mean/stdev/z-score turned out to be about twenty lines of
`statistics`:

```python
values = [float(s.total_estimated_monthly_usd) for s in baseline]
mean  = statistics.fmean(values)
stdev = statistics.stdev(values)
if stdev == 0:
    return None
z_score = (value - mean) / stdev
```

A 60MB dependency avoided, and the architecture claim stayed true. **The
enforcement changed the design** — that's the point of enforcement.

---

## Now the costs

Every article about hexagonal architecture ends here with a shrug about
"boilerplate." That undersells the real problems.

### Cost 1: Indirection genuinely hurts navigation

To follow one operation you may open four files: the port, the adapter, the
domain service, and the composition root. In a tangled codebase you'd open one.

`grep` and "go to definition" both get worse. Jump to the definition of
`gateway.execute(...)` and you land on an abstract method with a `...` body.
The code you actually wanted is somewhere else entirely, and your editor cannot
tell you where because the binding happens at runtime.

This is a real, permanent tax. Good naming and a documented composition root
reduce it; nothing eliminates it.

### Cost 2: It is genuine overkill for small systems

A CRUD app with one database and no meaningful business rules gets nothing from
this. You will write ports and adapters that add a layer of indirection over a
thing that was never going to change, to make testable a domain that is three
lines of validation.

Rough heuristic: **hexagonal architecture pays off when your business rules are
more complex and longer-lived than your infrastructure.** Rules about deleting
cloud resources safely: yes. Rendering a form: no.

### Cost 3: The abstraction can leak, and pretending otherwise makes it worse

Look again:

```python
def describe_ebs_volumes(self) -> list[dict[str, Any]]: ...
```

That returns raw AWS response dicts. Not a domain type — an `EbsVolume` model
would be cleaner in theory. But scanners need provider-specific fields, and
modelling every AWS attribute would mean a domain model that changes whenever
AWS adds a field.

So the port leaks. Deliberately, and documented, but it leaks. The port's method
names are also AWS-shaped — `describe_ebs_volumes`, `describe_rds_instances`.
Porting to GCP means new methods, not just a new adapter.

The mistake to avoid is pretending the leak isn't there. A leak you've named and
bounded is a trade-off. A leak you've papered over is a bug waiting for the
person who trusts your abstraction.

### Cost 4: Wrong ports are expensive to fix

This one bit the project, and it's the most instructive.

The original metric port took a single dimension pair:

```python
def get_instance_metric_averages(
    self, dimension_name: str, dimension_value: str, metric_name: str, days: int,
) -> list[float]: ...
```

Perfectly reasonable — until S3. CloudWatch publishes `BucketSizeBytes` against
**two** dimensions (`BucketName` and `StorageType`), and CloudWatch matches
dimension sets *exactly*. A query naming only one returns nothing.

So every bucket would have read as "size unknown" and no S3 finding would ever
have fired in production. Worse: **neither moto nor LocalStack publishes that
metric**, so no end-to-end test could have caught it — empty is also what "no
data" legitimately looks like.

The fix changed the port, which changed the adapter, all callers, and every test
fake:

```python
def get_metric_averages(
    self,
    namespace: str,
    dimensions: dict[str, str],       # a MAP, because CloudWatch matches sets
    metric_name: str,
    days: int,
    period_seconds: int = 3600,
) -> list[float]: ...
```

A port is a contract with multiple implementers. Changing it is a breaking
change by definition. The lesson isn't "design ports perfectly up front" — it's
that **ports should be introduced when you understand the capability**, and
speculative ports for capabilities you haven't built yet are the expensive kind.

### Cost 5: Everyone must understand it, or it erodes

One developer adding `import boto3` to a domain module in a hurry undoes the
property. With enforcement, CI catches it. Without enforcement, it decays in
about a quarter, and then you have all the indirection costs and none of the
benefits — the worst possible position.

---

## When to reach for it

**Good fit:**

- Business rules that are complex, safety-critical, or long-lived
- Multiple entry points (CLI + HTTP + scheduler) driving the same logic
- Infrastructure you expect to swap, or need to fake in tests
- Compliance or audit requirements — the audit trail lives in the domain, so it
  cannot be bypassed by a new adapter
- Long-lived systems where frameworks will outlive their welcome

**Poor fit:**

- CRUD with thin logic
- Prototypes and spikes
- Small teams shipping fast against a single stack they will not change
- Systems where the database schema effectively *is* the domain model

**A middle path that works:** start with a clean domain module and no ports.
Introduce a port the first time you need to fake something in a test, or the
first time a second implementation appears. Ports that emerge from real pressure
are better shaped than ports designed in advance.

---

## The test that tells you it's working

Here's the one-question diagnostic. Can you write this test?

> Run your most important business operation end to end, with no database, no
> network, and no framework — and have it exercise the same code that runs in
> production.

For FinOps Sentinel, that's the approve-and-remediate flow with a fake gateway,
an in-memory repository, and a fake notifier. It passes. Which means replacing
Slack with Telegram, or SQLite with Postgres, cannot break the approval logic —
because the approval logic never knew about either.

If you can write that test, the architecture is doing its job. If you can't, the
ports are in the wrong place, and no amount of interface-shaped boilerplate will
fix it.

---

*Source for every example: [github.com/boazleleina/finops-sentinel](https://github.com/boazleleina/finops-sentinel)*
