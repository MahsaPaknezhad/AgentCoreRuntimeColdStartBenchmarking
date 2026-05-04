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

## Results

**Metric definitions:**
- **Cold invoke latency**: Wall-clock time for the first request to a freshly provisioned runtime (network + platform overhead + agent execution).
- **Warm invoke latency**: Same, but for the second request on an already-warm runtime.
- **Cold start overhead**: Extra platform overhead unique to the first request: `(cold_invoke − cold_agent_ms) − (warm_invoke − warm_agent_ms)`. Subtracting agent execution time isolates platform overhead, then subtracting warm from cold isolates first-request initialization (container setup, routing, etc.).
- **VM uptime**: Time since the agent process started, as reported by the agent at first invoke. Indicates how long the microVM/container has been running before the first request arrives.

### Experiment 1 — Fresh runtime per round (10 rounds, ap-southeast-2)

Each round creates a new runtime, waits for READY, invokes cold + warm, then deletes it.

#### ZIP deployment

| Metric | Mean | P50 | P90 |
|--------|------|-----|-----|
| Cold start overhead | 3,489 ms | 3,369 ms | 3,815 ms |
| Cold invoke latency | 4,714 ms | 4,677 ms | 4,844 ms |
| Warm invoke latency | 1,313 ms | 1,335 ms | — |
| VM uptime | 3.5 s | 3.5 s | [3.4, 3.9] s |

#### Docker deployment

| Metric | Mean | P50 | P90 |
|--------|------|-----|-----|
| Cold start overhead | 7,359 ms | 7,361 ms | 7,429 ms |
| Cold invoke latency | 8,781 ms | 8,635 ms | 9,040 ms |
| Warm invoke latency | 1,496 ms | 1,342 ms | — |
| VM uptime | 4.6 s | 4.6 s | [3.7, 5.1] s |

**Key observations:**
- Docker cold start overhead is significantly higher than ZIP (~7,359ms vs ~3,489ms)
- Both modes show low warm invoke latency (~1.3–1.5s), confirming the session reuse fix
- ZIP cold invoke (~4.7s) is nearly half of Docker cold invoke (~8.8s)

### Experiment 2 — Pre-existing runtime (10 rounds, ap-southeast-2)

Uses an already-deployed runtime. Each round invokes cold + warm on the existing runtime (no provisioning step). This isolates invoke-time cold start from runtime creation overhead.

#### ZIP deployment

| Metric | Mean | P50 | P90 |
|--------|------|-----|-----|
| Cold start overhead | 3,586 ms | 3,368 ms | 3,946 ms |
| Cold invoke latency | 5,011 ms | 4,862 ms | 5,484 ms |
| Warm invoke latency | 1,412 ms | 1,371 ms | — |
| VM uptime | 3.8 s | 3.6 s | [3.3, 5.0] s |

#### Docker deployment

| Metric | Mean | P50 | P90 |
|--------|------|-----|-----|
| Cold start overhead | 375 ms | 335 ms | 506 ms |
| Cold invoke latency | 1,726 ms | 1,647 ms | 1,949 ms |
| Warm invoke latency | 1,492 ms | 1,463 ms | — |
| VM uptime | 971.6 s | 686.2 s | [38.4, 2285.4] s |

> **Note:** Docker VM uptime values (38s–2,285s) indicate the containers were already running, so uptime reflects total process lifetime rather than boot time.

**Key observations:**
- Docker cold start overhead (~375ms) is dramatically lower than ZIP (~2,776ms) on a pre-existing runtime
- Docker invoke latencies are ~3× lower than ZIP for both cold and warm invocations
- ZIP cold start overhead increased compared to Experiment 1, while Docker's decreased — suggesting the fresh-runtime provisioning in Experiment 1 masked some of the invoke-time cold start for ZIP

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
