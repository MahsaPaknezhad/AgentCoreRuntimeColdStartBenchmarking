# AgentCore Runtime Cold Start Benchmarking

Measures cold start latency for Amazon Bedrock AgentCore Runtime across **ZIP** (direct code deployment) and **Docker** (container) deployment modes.

## What it does

1. **Deploys** a minimal echo agent as both ZIP and Docker runtimes to AgentCore
2. **Runs** repeated cold start experiments (invoke with fresh session ID → wait for idle timeout → repeat)
3. **Reports** P50/P90/P99/mean/min/max latency for each deployment mode

## Prerequisites

- Python 3.13+ (installed via [pyenv](https://github.com/pyenv/pyenv))
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- Docker Desktop with buildx (for the Docker variant)
- AWS credentials configured (`aws configure`) with permissions for:
  - `bedrock-agentcore:*` (create/invoke/delete runtimes)
  - `s3:PutObject` / `s3:CreateBucket` (ZIP upload)
  - `ecr:CreateRepository` / `ecr:GetAuthorizationToken` (Docker push)
- An IAM role that trusts `bedrock-agentcore.amazonaws.com` in your target region.
  See [IAM docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html).

## Setup

```bash
git clone <this-repo>
cd AgentCoreRuntimeColdStartBenchmarking

# Create venv with Python 3.13 and install dependencies
uv venv --python 3.13
uv pip install -e .
```

## Configuration

Edit `config.py` to set your region and IAM role. Account ID is auto-detected from your AWS credentials.

```python
REGION = "ap-southeast-2"          # region where your role's trust policy allows AgentCore
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/your-agentcore-role"
```

The idle timeout defaults to 120s. Lower values mean faster experiments but the minimum AgentCore allows is 120s.

## Running the experiment

### Step 1: Deploy both runtimes

```bash
.venv/bin/python deploy.py
```

This will:
- Create an S3 bucket and upload the ZIP package for the ZIP runtime
- Build an ARM64 Docker image and push it to ECR for the Docker runtime
- Create both AgentCore runtimes and wait until they are READY
- Save runtime ARNs to `runtime_arns.json`

You can deploy only one mode: `.venv/bin/python deploy.py --mode zip` or `--mode docker`.

### Step 2: Run the cold start benchmark

```bash
.venv/bin/python experiment.py --rounds 5 --wait 150
```

| Flag | Default | Description |
|------|---------|-------------|
| `--rounds` | 5 | Number of cold start invocations per mode |
| `--wait` | 150 | Seconds to wait between rounds (must exceed idle timeout) |
| `--mode` | both | Run only `zip` or `docker` |

Each round invokes the runtime with a unique session ID (guaranteeing a new microVM), reads the full response, and records the end-to-end latency. Between rounds it waits for the idle timeout to expire so the next invocation is a true cold start.

Results are appended to `results.json`.

### Step 3: View the report

```bash
.venv/bin/python report.py
```

```
Mode       Rounds  P50 (ms)  P90 (ms)  P99 (ms)  Mean (ms)  Min (ms)  Max (ms)
------------------------------------------------------------------------------
zip             5      1311      1336      1339       1304      1253      1340
docker          5       377       502       513        420       355       514
```

For machine-readable output: `.venv/bin/python report.py --json`

### Makefile shortcuts

```bash
make deploy              # deploy both runtimes
make experiment ROUNDS=5 # run benchmark
make report              # print results
make clean               # delete runtimes and runtime_arns.json
make all                 # deploy → experiment → report
```

## Cleanup

```bash
.venv/bin/python deploy.py --teardown
```

This deletes both AgentCore runtimes. The S3 bucket and ECR repository are left in place (they cost nothing when empty).

## How cold start is measured

Each round:
1. Generate a fresh `runtimeSessionId` (guarantees a new session → new microVM)
2. Record `time.monotonic()` before `invoke_agent_runtime()`
3. Read the full response body
4. Record elapsed time — this is the **end-to-end cold start latency** including network round-trip

## Project structure

```
├── agent/app.py       # Minimal echo agent (/invocations + /ping, stdlib only)
├── config.py          # Region, role, runtime names, timeouts (auto-detects account)
├── deploy.py          # Deploys ZIP + Docker runtimes, teardown
├── experiment.py      # Runs N cold start rounds per mode
├── report.py          # Prints P50/P90/P99 table from results.json
├── Dockerfile         # ARM64 container for Docker mode
├── pyproject.toml     # Python project config (uv/pip)
├── Makefile           # Convenience targets
└── .gitignore
```

## Notes

- The echo agent is intentionally minimal (stdlib only, no model calls). Real agents with frameworks like Strands or LangGraph and Bedrock model invocations will have significantly higher cold starts.
- Docker mode tends to be faster because the container image is pre-built. ZIP mode decompresses and sets up the runtime environment on each new session.
- The `--wait` flag should be at least `IDLE_TIMEOUT_SECONDS + 30` to guarantee sessions are fully destroyed between rounds.
