# Alembic in Practice: Structure, Revisions, and the Traps SQLite Sets

*What those hex filenames mean, why migrations silently applied to the wrong database for two months, and how to widen a CHECK constraint without quietly breaking it.*

---

Alembic is the migration tool for SQLAlchemy. Most tutorials get you as far as
`alembic revision --autogenerate` and stop, which leaves you unprepared for the
two things that actually go wrong: SQLite's refusal to alter constraints, and
configuration that points migrations at a different database than your
application uses.

This post covers both, using real migrations from
[FinOps Sentinel](https://github.com/boazleleina/finops-sentinel) — an AWS cost
agent with four migrations, one of which took three attempts to get right.

---

## The layout

```
alembic.ini                     # config; script location, logging
alembic/
├── env.py                      # runs on every alembic command
├── script.py.mako              # template for new revision files
└── versions/
    ├── c447645246ac_initial_schema.py
    ├── 08872b80af2e_notifications_and_remediations.py
    ├── a1c9f4d27b13_add_rds_and_s3_resource_types.py
    └── b7e2f81c4a90_add_spend_snapshots.py
```

Plus one table Alembic creates in your database:

```sql
CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
```

One row. One value. That is the entirety of Alembic's state — which database is
at which revision. Everything else is derived.

---

## Why the filenames look like keyboard mashing

This is the most common first question, and the answer explains the design.

```
c447645246ac_initial_schema.py
b7e2f81c4a90_add_spend_snapshots.py
```

The hex prefix is the **revision ID**: a 12-character slice of a UUID, generated
by `alembic revision`. The filename is `<revision_id>_<your message slugified>.py`.

Inside each file:

```python
revision: str = "b7e2f81c4a90"
down_revision: Union[str, None] = "a1c9f4d27b13"
```

Those two fields form a **linked list**:

```
c447645246ac  initial_schema                  (down_revision = None)  ← root
      ↓
08872b80af2e  notifications_and_remediations
      ↓
a1c9f4d27b13  add_rds_and_s3_resource_types
      ↓
b7e2f81c4a90  add_spend_snapshots                                     ← head
```

**Order comes from the pointers, never from filenames.** Look at the project's
list again: `b7e2f81c4a90` sorts alphabetically *before* `c447645246ac`, yet
`c447645246ac` is the first migration and `b7e2f81c4a90` is the newest. If
Alembic sorted by filename, everything would run backwards.

### Why not 0001, 0002, 0003?

Because sequential numbers collide the moment two people work in parallel.

```
main
 ├── feature/billing   → 0004_add_invoices.py
 └── feature/auth      → 0004_add_sessions.py
```

Both are valid. Both are `0004`. Merging produces a conflict where the numbering
lies about order, and worse, the conflict is in the *filename* — git resolves it
by keeping both, and now your migration history has two fourth steps.

With hex IDs there is no collision. And if both branches set `down_revision` to
the same parent, Alembic detects a genuine branch:

```
$ alembic upgrade head
ERROR: Multiple head revisions are present
```

It refuses to guess, and you resolve it explicitly with `alembic merge`. The
scary-looking IDs are what make that detection possible.

### If you want readable filenames

Add to `alembic.ini`:

```ini
file_template = %%(year)d%%(month).2d%%(day).2d_%%(rev)s_%%(slug)s
```

Giving `20260728_b7e2f81c4a90_add_spend_snapshots.py` — date-sortable, revision
ID still present because Alembic needs it. Existing files keep their names; only
new ones pick it up.

---

## The configuration trap that cost two months

This is the bug I most want to warn people about, because it produces **no
error message at all**.

The default `env.py` reads the database URL from `alembic.ini`. FinOps Sentinel
needed it configurable, so early on it did the obvious thing:

```python
# alembic/env.py — the version with the bug
def get_url():
    db_path = os.getenv("SENTINEL_DB_PATH", ".sentinel.db")
    return f"sqlite:///{db_path}"
```

Reasonable. Except the application reads the same setting through
pydantic-settings:

```python
class Settings(BaseSettings):
    sentinel_db_path: str = ".sentinel.db"
    model_config = SettingsConfigDict(env_file=".env")   # ← reads .env too
```

**`os.getenv` does not read `.env`. pydantic-settings does.**

The documented setup puts `SENTINEL_DB_PATH=data/sentinel.db` in `.env`, not in
the shell. So:

| Component | Reads from | Resolves to |
|---|---|---|
| The app | env var → `.env` → default | `data/sentinel.db` |
| Alembic | env var → default | `.sentinel.db` |

Every migration ever run landed on `.sentinel.db`. The application used
`data/sentinel.db`. Two files, silently, for two months.

### Why nothing broke for so long

The application also calls `Base.metadata.create_all()` in tests, and the first
three migrations only created tables that SQLAlchemy would have created anyway.
The schemas *happened* to agree.

Then came a migration whose table nothing else creates — `spend_snapshots`. The
next scan died:

```
alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade a1c9f4d27b13 -> b7e2f81c4a90

sentinel scan
OperationalError: (sqlite3.OperationalError) no such table: spend_snapshots
```

Alembic reported success. The app said the table didn't exist. Both were telling
the truth about different files.

### The fix: one owner for the URL

```python
# config.py — the single source of truth
def database_url() -> str:
    """The one place the findings database URL is built.

    Both the application (bootstrap.get_repository) and Alembic (alembic/env.py)
    call this. They used to build it independently, and drifted.
    """
    return f"sqlite:///{settings.sentinel_db_path}"
```

```python
# alembic/env.py
from finops_sentinel.config import database_url

def get_url():
    return database_url()
```

```python
# bootstrap.py
def get_repository() -> FindingsRepository:
    return SqlAlchemyRepository(db_url=database_url())
```

And `alembic.ini` is deliberately emptied:

```ini
# Deliberately empty. env.py overrides this with the application's own
# Settings.sentinel_db_path. A URL here would be dead config that reads as
# authoritative — which is exactly how this drifted before.
sqlalchemy.url =
```

**Takeaway: if your app and your migrations resolve the database path
separately, they will eventually disagree, and nothing will tell you.** Give the
URL exactly one owner and have both call it.

The project now pins this with tests:

```python
def test_the_app_and_alembic_build_the_url_the_same_way():
    assert get_repository().db_url == database_url()

def test_alembic_ini_does_not_hardcode_a_database():
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "alembic.ini")
    assert parser.get("alembic", "sqlalchemy.url", fallback="").strip() == ""

def test_alembic_env_does_not_read_the_environment_itself():
    env_py = (REPO_ROOT / "alembic" / "env.py").read_text()
    assert "database_url" in env_py
    assert "os.getenv(" not in env_py
```

---

## Autogenerate, and where to stop trusting it

```bash
alembic revision --autogenerate -m "add spend snapshots"
```

Alembic compares your SQLAlchemy models against the live database and writes the
diff. It needs `target_metadata` wired up in `env.py`:

```python
from finops_sentinel.adapters.persistence.sqlalchemy_repo import Base
target_metadata = Base.metadata
```

**It detects** new/dropped tables and columns, nullability changes, most index
and unique-constraint changes, and some type changes.

**It misses** table and column *renames* (it sees a drop plus an add — and will
happily destroy your data), CHECK constraint changes, server defaults in many
cases, and anything requiring data transformation.

The generated file is a **draft**. The comment Alembic itself writes says so:

```python
def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
```

Always read it. For anything beyond adding a column, expect to hand-write.

---

## Trap: SQLite cannot ALTER a constraint

Adding two values to an enum required widening a CHECK constraint:

```python
class ResourceType(StrEnum):
    EBS_VOLUME = "ebs_volume"
    ELASTIC_IP = "elastic_ip"
    EC2_INSTANCE = "ec2_instance"
    EBS_SNAPSHOT = "ebs_snapshot"
    RDS_INSTANCE = "rds_instance"    # new
    S3_BUCKET = "s3_bucket"          # new
```

On Postgres: `ALTER TABLE ... DROP CONSTRAINT ... ADD CONSTRAINT ...`. Done.

On SQLite: **there is no ALTER for constraints.** The only route is create a new
table, copy the data, drop the old, rename the new. Alembic wraps this in
`batch_alter_table`:

```python
with op.batch_alter_table("resources", recreate="always") as batch_op:
    ...
```

### Attempt 1: silently kept the old constraint

The first version passed `table_args` and ran without error:

```python
with op.batch_alter_table(
    "resources",
    recreate="always",
    table_args=(
        sa.CheckConstraint(_resource_type_check(NEW_TYPES), name="check_resource_type"),
    ),
):
    pass
```

`alembic upgrade head` printed success. Inserting an `rds_instance` row still
failed.

**Why:** `table_args` *adds to* a reflected table definition rather than
replacing it. Alembic reflected the live table — including the old four-value
CHECK — then appended the new six-value one. The rebuilt table had **both**, and
the old one still rejected every new row.

Only dumping the schema revealed it:

```bash
sqlite3 data/sentinel.db ".schema resources"
# CONSTRAINT check_resource_type CHECK (resource_type IN ('ebs_volume', ...))   ← old
# CONSTRAINT check_resource_type CHECK (resource_type IN ('ebs_volume', ..., 's3_bucket'))
```

### Attempt 2: `copy_from` to suppress reflection

The fix is `copy_from` — an explicit table definition that tells Alembic *not*
to reflect:

```python
def _columns_only_resources() -> sa.Table:
    """The `resources` table as columns and primary key, with NO constraints.

    Handed to batch_alter_table as `copy_from`, which suppresses reflection of
    the live table. That suppression is the point: `table_args` adds to a
    reflected definition rather than replacing it.
    """
    return sa.Table(
        "resources",
        sa.MetaData(),
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_arn", sa.String(), nullable=False),
        sa.Column("region", sa.String(), nullable=False),
        sa.Column("current_tags", sa.String(), nullable=False),
        sa.Column("lifecycle", sa.String(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
```

### Attempt 3: the indexes were gone

`copy_from` suppresses **index** reflection too. The rebuilt table lost its
unique index on `resource_id` — which the upsert logic depends on for
correctness. So it gets recreated by hand:

```python
def _rebuild_resources(resource_types: tuple[str, ...]) -> None:
    with op.batch_alter_table(
        "resources",
        recreate="always",
        copy_from=_columns_only_resources(),
        table_args=(
            sa.CheckConstraint("lifecycle IN ('active', 'deleted')",
                               name="check_resource_lifecycle"),
            sa.CheckConstraint(_resource_type_check(resource_types),
                               name="check_resource_type"),
        ),
    ):
        pass  # the rebuild itself is the migration

    # copy_from also suppresses index reflection, so re-create by hand.
    bind = op.get_bind()
    existing = {index["name"] for index in sa.inspect(bind).get_indexes("resources")}
    if "ix_resources_resource_id" not in existing:
        op.create_index(op.f("ix_resources_resource_id"), "resources",
                        ["resource_id"], unique=True)
```

**Rule for SQLite batch migrations: whatever the batch block omits is silently
absent from the rebuilt table.** Every column, every constraint, every index has
to be re-declared. And "it ran without error" proves nothing — dump the schema.

---

## Downgrades that refuse to run

Most `downgrade()` functions are written on autopilot as the inverse of
`upgrade()`. Sometimes the honest inverse is *refusing*:

```python
def downgrade() -> None:
    bind = op.get_bind()
    stranded = bind.execute(
        sa.text("SELECT COUNT(*) FROM resources "
                "WHERE resource_type IN ('rds_instance', 's3_bucket')")
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            f"Cannot downgrade: {stranded} resource(s) use rds_instance or s3_bucket, "
            "which the previous CHECK constraint forbids. Delete those resources and "
            "the findings referencing them first if this downgrade is intended."
        )
    _rebuild_resources(OLD_RESOURCE_TYPES)
```

Narrowing the constraint with those rows present means deleting them — which
also orphans every finding referencing them. A downgrade that silently destroys
data to succeed is worse than one that stops and explains.

The docstring says so up front, so nobody is surprised at 2am:

> `downgrade()` restores the four-value constraint and will FAIL if any
> rds_instance or s3_bucket row exists by then — deliberately.

---

## A simple migration, for contrast

Not every migration is a fight. Adding a table is exactly what you'd hope:

```python
def upgrade() -> None:
    op.create_table(
        "spend_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        # Stored as a string, matching SafeNumeric: SQLite has no Decimal, and
        # a float column would quietly round money.
        sa.Column("total_estimated_monthly_usd", sa.String(), nullable=False),
        sa.Column("open_findings", sa.Integer(), nullable=False),
        sa.Column("active_resources", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        op.f("ix_spend_snapshots_snapshot_date"),
        "spend_snapshots", ["snapshot_date"], unique=True,
    )
```

Note the docstring convention this project uses — the *why*, not the *what*:

> `snapshot_date` is UNIQUE on purpose. The repository upserts by date, so two
> scans on the same day collapse to one row — otherwise a day that happened to
> be scanned five times would weigh five times as much in the trailing mean, and
> the z-score would measure scan cadence rather than spend.

Six months later, that paragraph is worth more than the code.

---

## Keeping constraints from drifting

CHECK constraints duplicating enum values is a drift hazard. This project
generates them:

```python
def _in_clause(column: str, values: type[StrEnum]) -> str:
    """Render `column IN ('a', 'b', ...)` straight from a StrEnum.

    Generated rather than hand-written so the constraint cannot drift from the
    enum: adding a ResourceType now updates this automatically, and only the
    Alembic migration has to be written by hand.
    """
    members = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({members})"


class ResourceModel(Base):
    __tablename__ = "resources"
    ...
    __table_args__ = (
        CheckConstraint(_in_clause("lifecycle", ResourceLifecycle),
                        name="check_resource_lifecycle"),
        CheckConstraint(_in_clause("resource_type", ResourceType),
                        name="check_resource_type"),
    )
```

The model side can never drift from the enum. Only the migration is hand-written
— which is unavoidable, since a migration must describe a *transition* between
two states, and only one of them exists in your code.

---

## Testing migrations

A green `alembic upgrade head` proves almost nothing (see attempt 1). Two things
worth doing:

**1. Round-trip against a copy of a real database.**

```bash
cp data/sentinel.db /tmp/mig-test.db
SENTINEL_DB_PATH=/tmp/mig-test.db alembic upgrade head
SENTINEL_DB_PATH=/tmp/mig-test.db alembic downgrade -1
SENTINEL_DB_PATH=/tmp/mig-test.db alembic upgrade head

sqlite3 /tmp/mig-test.db "select count(*) from resources; select count(*) from findings;"
# 274 / 187 — same as before
```

Real data, real row counts, and it catches data loss a fresh database never
would.

**2. Assert the error message is useful.**

An un-migrated database should not produce a raw SQL traceback:

```python
def upsert_resource(self, resource: Resource) -> None:
    try:
        self._upsert_resource(resource)
    except IntegrityError as exc:
        if "check_resource_type" not in str(exc):
            raise
        raise RuntimeError(
            f"The findings database does not allow resource type "
            f"'{resource.resource_type}'. It was created by an older "
            f"version of FinOps Sentinel. Run `alembic upgrade head` "
            f"against {self.db_url} and scan again."
        ) from exc
```

Failing hard is right — a half-written inventory is worse than a stopped run.
But the traceback told nobody what to do. Now it names the type, the database,
and the command.

---

## Command reference

```bash
alembic revision -m "message"                  # empty migration, hand-written
alembic revision --autogenerate -m "message"   # diff models vs database

alembic upgrade head          # apply everything outstanding
alembic upgrade +1            # one step forward
alembic downgrade -1          # one step back
alembic downgrade base        # all the way down

alembic current               # what revision is this database at?
alembic history --verbose     # the full chain
alembic heads                 # >1 means you have a branch to merge
alembic merge -m "merge" a1b2 c3d4

alembic upgrade head --sql    # print SQL instead of executing (offline mode)
```

`alembic current` is the one to reach for when something is confusing. It tells
you where Alembic thinks the database is — and if that disagrees with the
database you *expect*, you have the bug from earlier in this post.

---

## Checklist

- **Never edit a migration that has been applied anywhere but your laptop.**
  Write a new one. The old revision ID is a foreign key in every database that
  ran it.
- **Read autogenerated migrations before committing.** Especially for renames,
  which autogenerate turns into drop-plus-add.
- **Write the "why" in the docstring.** The SQL says what; only you can say why.
- **Test on a copy of real data.** Fresh databases hide data-loss bugs.
- **Dump the schema after non-trivial migrations.** "It ran" is not "it worked."
- **Give the database URL one owner** that both the app and Alembic call.
- **Make downgrades honest** — including refusing, when the honest inverse is
  data loss.

Migrations are the one part of a codebase where a mistake is not just wrong but
*permanently* wrong, replicated to every environment that ran it. Worth the
extra ten minutes.

---

*Every example here is from [github.com/boazleleina/finops-sentinel](https://github.com/boazleleina/finops-sentinel)*
