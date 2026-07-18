# FinOps Sentinel

AWS Cost Optimization Agent designed to identify and remediate wasted resources across your AWS accounts.

## Features

- Scans for unattached EBS volumes, orphaned EIPs, and long-stopped EC2 instances.
- Estimates potential monthly savings.
- Supports tag-based guardrails (e.g., `finops:protected=true`) to exclude resources from automated actions.
- Local-first architecture using LocalStack and `moto` for testing.

## Architecture (Ports & Adapters)

This project strictly adheres to a Hexagonal Architecture (Ports and Adapters) to ensure business logic is completely decoupled from infrastructure (like AWS APIs, Slack, or SQLAlchemy).

- **`domain/`**: The core business logic. Contains pure Python models, the state machine, and rule evaluation. Imports no external frameworks.
- **`ports/`**: The interfaces (Abstract Base Classes) defining how the domain interacts with the outside world (e.g., `CloudGateway`, `FindingsRepository`, `Notifier`).
- **`adapters/`**: The concrete implementations of the ports. This is where the messy infrastructure code lives. For example, AWS scanning logic lives in `adapters/aws/scanners/` and uses `boto3` to fulfill the `Scanner` and `CloudGateway` contracts.
- **`bootstrap.py`**: The Composition Root. This is the only file that knows about both the domain and the concrete adapters. It wires everything together based on your configuration.
