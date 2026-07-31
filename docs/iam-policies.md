# IAM for FinOps Sentinel

Two principals, and the whole point is the gap between them.

**Sentinel's own role** finds waste. It can read every resource in the account
and cannot delete any of them. If its credentials leak, the attacker gets an
inventory.

**The approver role** deletes things. Sentinel cannot use it on its own
initiative — it assumes the role only when a named approver clicks Approve, and
narrows the session to the single resource that approval named. AWS, not
Sentinel's approver list, is what authorizes the deletion, and CloudTrail
records a session named after the person who clicked.

Set `SENTINEL_ASSUME_ROLE=true` to turn this on. Off (the default), remediation
runs under Sentinel's own credentials and the approver list is Sentinel's own
gate and nothing more — fine for the LocalStack demo, wrong for an account you
would mind losing.

---

## 1. Sentinel's execution role

Attach to whatever runs the app — an ECS task role, an EC2 instance profile, a
Lambda role. No destructive verbs appear anywhere in it.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadInventory",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeRegions",
        "ec2:DescribeVolumes",
        "ec2:DescribeAddresses",
        "ec2:DescribeInstances",
        "ec2:DescribeInstanceStatus",
        "ec2:DescribeSnapshots",
        "rds:DescribeDBInstances",
        "s3:ListAllMyBuckets",
        "s3:GetBucketLocation",
        "s3:GetBucketTagging",
        "s3:GetBucketVersioning",
        "s3:GetLifecycleConfiguration",
        "s3:ListBucketMultipartUploads",
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics",
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    },
    {
      "Sid": "AssumeApproverRoleOnly",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::123456789012:role/finops-approver"
    }
  ]
}
```

`Describe*` cannot be resource-scoped — EC2 does not support resource-level
permissions on them — so `"Resource": "*"` there is a property of the API, not
a shortcut. The `sts:AssumeRole` statement is scoped to exactly one role ARN.

`sts:GetCallerIdentity` is what lets recorded resource ARNs name the real
account. Denying it costs the account segment of those ARNs (they read
`unknown` and a warning is logged) and nothing else — scanning continues, and
session policies build their ARNs from the approver role's account regardless.

## 2. The approver role

### Trust policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::123456789012:role/finops-sentinel" },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": { "sts:ExternalId": "put-a-real-secret-here" }
      }
    }
  ]
}
```

`ExternalId` is the confused-deputy control: the role cannot be assumed by
anything that does not hold the secret, even if its ARN leaks. Put the same
value in `SENTINEL_APPROVER_EXTERNAL_ID`.

### Permission policy

The outer bound on what any approval can ever do. Sentinel narrows further per
approval; this is the ceiling.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RemediationPlaybooks",
      "Effect": "Allow",
      "Action": [
        "ec2:CreateSnapshot",
        "ec2:CreateTags",
        "ec2:DeleteVolume",
        "ec2:DeleteSnapshot",
        "ec2:ReleaseAddress",
        "ec2:TerminateInstances",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ReadWhatThePlaybooksCheck",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeVolumes",
        "ec2:DescribeSnapshots",
        "ec2:DescribeAddresses",
        "ec2:DescribeInstances",
        "ec2:DescribeInstanceStatus",
        "s3:ListBucketMultipartUploads"
      ],
      "Resource": "*"
    },
    {
      "Sid": "NeverTouchProtected",
      "Effect": "Deny",
      "Action": "*",
      "Resource": "*",
      "Condition": {
        "StringEquals": { "aws:ResourceTag/finops:protected": "true" }
      }
    }
  ]
}
```

That `Deny` is worth the four lines. Sentinel already refuses protected
resources twice — at scan time and again at approval — but both checks are
Sentinel's own code reading a tag it fetched earlier. This one is evaluated by
IAM at call time, so a bug in the guardrail, or a tag added in the seconds
between the check and the call, still cannot delete a protected resource.

## 3. What Sentinel adds per approval

Every `AssumeRole` call carries an inline **session policy**, which STS
intersects with the role's permissions — it can only narrow. Approving the
deletion of `vol-0abc` in `eu-west-1` sends:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadWhatThePlaybookMustCheck",
      "Effect": "Allow",
      "Action": ["ec2:DescribeVolumes", "ec2:DescribeSnapshots"],
      "Resource": "*"
    },
    {
      "Sid": "ActOnExactlyThisResource",
      "Effect": "Allow",
      "Action": ["ec2:CreateSnapshot", "ec2:CreateTags", "ec2:DeleteVolume"],
      "Resource": [
        "arn:aws:ec2:eu-west-1:123456789012:volume/vol-0abc",
        "arn:aws:ec2:eu-west-1::snapshot/*"
      ]
    }
  ]
}
```

The approver's role may be written broadly; the session that runs this deletion
can touch one volume, for fifteen minutes, and cannot terminate an instance or
release an address at all. `snapshot/*` is there because the snapshot the
playbook is about to create cannot be named before it exists.

The mapping is in `adapters/aws/approval_credentials.py`, keyed by playbook. A
playbook with no entry gets no credentials — adding one without deciding what
it may touch fails closed.

## 4. Configuring approvers

```env
SENTINEL_ASSUME_ROLE=true
SENTINEL_APPROVERS=U024BE7LH=arn:aws:iam::123456789012:role/finops-approver,U01ABCDEF=arn:aws:iam::123456789012:role/finops-approver
SENTINEL_APPROVER_EXTERNAL_ID=put-a-real-secret-here
SENTINEL_SESSION_DURATION_SECONDS=900
```

Use the channel's **stable user id** (`U024BE7LH`), not a display name.
Usernames change and display names are user-controlled, and this string decides
who gets AWS credentials.

One role shared by the approver group, not a role per human — a role per person
turns onboarding into an IAM pull request, and the session name already carries
the individual. Split roles only when two groups should be able to delete
genuinely different things.

An approver with no role ARN cannot approve while `SENTINEL_ASSUME_ROLE` is on:
the refusal happens at the guardrail (audited as `approve_blocked_unauthorized`)
rather than after the state change, so a configuration mistake does not strand a
finding in `FAILED`.

## 5. What this does and does not prove

It proves the deletion was authorized by AWS against a principal that names a
person, that Sentinel could not have performed it unprompted, and that the
session could not have touched anything else.

It does not prove the human authenticated to AWS. Sentinel *asserts* the
identity from the Slack payload; the chain's strength is the Slack account plus
signature verification in front of it. Real end-user authentication means
federating Slack identity to an IdP and having AWS mint the session — more
machinery than this system carries, and worth saying plainly rather than
letting "IAM enforces it" imply more than it does.

## 6. Testing it

- **Mechanics** — LocalStack, via
  `tests/integration/test_localstack_e2e.py::test_assume_role_path_runs_the_playbook_under_temporary_credentials`.
  Proves Sentinel assumes a role and remediates with the returned session.
- **Refusals** — unit tests with a fake STS
  (`tests/unit/test_approval_credentials.py`): an unmapped actor, a denied
  `AssumeRole`, an unknown playbook, and the exact contents of the session
  policy.
- **Enforcement** — a real AWS account, once. LocalStack Community evaluates no
  IAM policies: it issues a session for any role ARN and permits whatever that
  session asks. A "denied" assertion there passes the deletion, which is a green
  test for something that never ran. Verify by hand against a sandbox account:
  approve a finding with an approver role that lacks `ec2:DeleteVolume`, and
  confirm the finding lands in `FAILED` with `AccessDenied` in the audit detail.
