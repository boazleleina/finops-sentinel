# Local Development Instructions

This project is built to run locally against [LocalStack](https://localstack.cloud/), so you can test all AWS interactions safely and for free without touching real infrastructure.

### 1. Initial Setup (One-time)
When you first clone this repository, you need to set up your Python virtual environment and install the required packages:

```bash
# Create the virtual environment
python3 -m venv .venv

# Activate it
source .venv/bin/activate

# Install the dependencies
pip install boto3 pydantic-settings
```

*(Note: If you don't have the AWS CLI installed on your Mac, you can install it via Homebrew using `brew install awscli`)*

### 2. Starting Your Environment (Daily Routine)
When you sit down to work, start your Docker environment and activate your Python virtual environment:

```bash
# Start the LocalStack fake cloud
docker compose --profile dev up -d

# Activate your python environment
source .venv/bin/activate
```

### 3. Seeding Fake Resources & Testing
We use a script to populate LocalStack with fake "leaky" resources (unattached volumes, stopped EC2s, etc.) so we have things for the agent to detect.

```bash
# Run the seed script to create dummy resources
python scripts/seed_localstack.py
```

You can verify the dummy resources exist by querying LocalStack directly using the AWS CLI. Because LocalStack needs *some* credentials, we pass dummy `test` variables inline:

```bash
AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test aws --endpoint-url=http://localhost:4566 ec2 describe-volumes --region us-east-1
```

If you just want to verify the snapshots that were created by our setup script (scripts/seed_localstack.py), we can filter them by their description (the script gave them descriptions starting with "Old snapshot").

Try running this command to filter out all the auto-created ones:

```bash
AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test aws --endpoint-url=http://localhost:4566 ec2 describe-snapshots --owner-ids self --region us-east-1 --filters Name=description,Values="Old snapshot*"
```

*(Tip: When viewing output, if it pauses with a `:` at the bottom, press `q` on your keyboard to exit).*

### 4. Shutting Down
When you are done for the day, you should shut down LocalStack to conserve your laptop's battery and CPU:

```bash
docker compose --profile dev down
```
