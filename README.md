# AgentCore Runtime Cold Start Benchmarking

Measures cold start latency for Amazon Bedrock AgentCore Runtime across **ZIP** and **Docker** deployment modes.

## What it does

1. **Deploys** a minimal echo agent as both ZIP and Docker runtimes
2. **Runs** repeated cold start experiments (invoke → wait for idle timeout → invoke again)
3. **Reports** P50/P90/P99 latency for each deployment mode

## Prerequisites

- Python 3.11+
- AWS credentials with AgentCore permissions
- Docker (for building the Docker variant)
- An IAM role for AgentCore runtimes (see [IAM docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html))

## Quick start

```bash
# Install dependencies
pip install -r requirements.txt

# Edit config.py with your account ID, region, and role ARN
vim config.py

# Deploy both runtimes
python deploy.py

# Run the experiment (default: 5 rounds)
python experiment.py --rounds 5

# Print the report
python report.py
```

Or use the Makefile:

```bash
make deploy
make experiment ROUNDS=5
make report
make clean        # tear down runtimes
```

## How cold start is measured

Each round:
1. Generate a fresh `runtimeSessionId` (guarantees a new session → new VM)
2. Record `time.monotonic()` before `invoke_agent_runtime()`
3. Read the full response
4. Record elapsed time — this is the **end-to-end cold start latency**

Between rounds the script waits for the idle session timeout so the next invocation is guaranteed cold.

## Output

Results are saved to `results.json` and printed as a table:

```
Mode     | Rounds | P50 (ms) | P90 (ms) | P99 (ms) | Mean (ms)
---------|--------|----------|----------|----------|----------
zip      |     5  |   6823   |   8912   |   9456   |   7234
docker   |     5  |   3412   |   4567   |   5012   |   3678
```
