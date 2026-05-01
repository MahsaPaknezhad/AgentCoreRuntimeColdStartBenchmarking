"""Shared configuration for the cold start benchmark.

Auto-detects account ID from the current AWS session.
"""

import boto3

REGION = "ap-southeast-2"

# Auto-detect from current AWS credentials
_sts = boto3.client("sts", region_name=REGION)
_identity = _sts.get_caller_identity()
ACCOUNT_ID = _identity["Account"]

# Existing role that trusts bedrock-agentcore.amazonaws.com in ap-southeast-2
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/agentcore-test-agent-role"

# Runtime names
ZIP_RUNTIME_NAME = "coldstart_bench_zip"
DOCKER_RUNTIME_NAME = "coldstart_bench_docker"

# ECR repository for the Docker variant
ECR_REPO_NAME = "coldstart-bench-agent"

# Lifecycle — keep idle timeout short so experiments run faster
IDLE_TIMEOUT_SECONDS = 120   # 2 min idle → session destroyed
MAX_LIFETIME_SECONDS = 300   # 5 min max

# Experiment defaults
DEFAULT_ROUNDS = 5
RESULTS_FILE = "results.json"
