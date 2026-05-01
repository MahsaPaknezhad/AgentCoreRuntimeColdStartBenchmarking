# AgentCore Runtime Cold Start Benchmarking

Measures cold start latency for Amazon Bedrock AgentCore Runtime across **ZIP** (direct code deployment) and **Docker** (container) deployment modes.

## What it does

1. **Creates** a fresh runtime each round, waits for READY, invokes cold + warm, then deletes it
2. **Computes** cold start overhead = (cold invoke − agent time) − (warm invoke − agent time)
3. **Reports** P50/P90/mean/min/max latency for each deployment mode

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

### Step 1: Deploy both runtimes (optional, for manual testing)

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
.venv/bin/python experiment.py --rounds 5
```

| Flag | Default | Description |
|------|---------|-------------|
| `--rounds` | 5 | Number of cold start rounds per mode |
| `--mode` | both | Run only `zip` or `docker` |

Each round creates a brand-new runtime, waits for READY, performs a cold invoke (first request) and a warm invoke (second request), computes the cold start overhead, then deletes the runtime. This guarantees every cold invoke hits a freshly provisioned environment.

Results are saved to `results.json`.

### Step 3: View the report

```bash
.venv/bin/python report.py
```

For machine-readable output: `.venv/bin/python report.py --json`

## Results (10 rounds, ap-southeast-2)

### ZIP deployment

| Metric | Mean | P50 | P90 |
|--------|------|-----|-----|
| Provisioning (create → READY) | 20,646 ms | 20,637 ms | 20,697 ms |
| Cold start overhead | 334 ms | 536 ms | 1,052 ms |
| Cold invoke latency | 5,363 ms | 5,241 ms | 6,839 ms |
| Warm invoke latency | 5,046 ms | 5,133 ms | — |
| App boot time (uptime_s) | 4.0 s | 3.8 s | — |

### Docker deployment

| Metric | Mean | P50 | P90 |
|--------|------|-----|-----|
| Provisioning (create → READY) | 10,792 ms | 10,762 ms | 10,857 ms |
| Cold start overhead | 4,527 ms | 6,011 ms | 7,042 ms |
| Cold invoke latency | 8,251 ms | 8,579 ms | 8,825 ms |
| Warm invoke latency | 3,857 ms | 1,788 ms | — |
| App boot time (uptime_s) | 4.0 s | 4.2 s | — |

**Key observations:**
- ZIP provisioning takes ~2× longer than Docker (~20.6s vs ~10.8s)
- ZIP cold start overhead is significantly lower than Docker (~334ms vs ~4,527ms)
- Docker warm invoke latency is much lower than cold, indicating substantial first-request overhead in the container runtime

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
1. Create a fresh runtime and wait for READY (timed as `create_ms`)
2. Cold invoke — first request to the brand-new runtime (timed as `cold_invoke_ms`)
3. Warm invoke — second request to the same runtime (timed as `warm_invoke_ms`)
4. Cold start overhead = (cold_invoke − cold_agent_ms) − (warm_invoke − warm_agent_ms)
5. Delete the runtime

The agent reports its own processing time (`agent_ms`) and uptime since process start (`uptime_s`), allowing the benchmark to isolate platform overhead from agent execution time.

## Project structure

```
├── agent/app.py       # Strands agent with timing instrumentation (/invocations + /ping)
├── config.py          # Region, role, runtime names, timeouts (auto-detects account)
├── deploy.py          # Deploys ZIP + Docker runtimes, teardown
├── experiment.py      # Runs N cold start rounds per mode (create → invoke → delete)
├── report.py          # Prints P50/P90 summary from results.json
├── Dockerfile         # ARM64 container for Docker mode
├── pyproject.toml     # Python project config (uv/pip)
├── Makefile           # Convenience targets
└── .gitignore
```

## Notes

- The agent uses [Strands Agents](https://github.com/strands-agents/strands-agents-python) with a Bedrock model call. Real-world cold starts will vary depending on model, framework, and dependencies.
- Docker mode tends to be faster because the container image is pre-built. ZIP mode decompresses and sets up the runtime environment on each cold start.
