"""Shared configuration for the cold start benchmark.

Auto-detects account ID from the current AWS session on first access.
"""

import functools

import boto3

REGION = "ap-southeast-2"


@functools.cache
def _get_account_id():
    return boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]


# Lazy property-style access via module-level __getattr__
_DERIVED = {
    "ACCOUNT_ID": lambda: _get_account_id(),
    "ROLE_ARN": lambda: f"arn:aws:iam::{_get_account_id()}:role/agentcore-test-agent-role",
}


def __getattr__(name):
    if name in _DERIVED:
        return _DERIVED[name]()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Runtime names
ZIP_RUNTIME_NAME = "coldstart_bench_zip"
DOCKER_RUNTIME_NAME = "coldstart_bench_docker2"

# ECR repository for the Docker variant
ECR_REPO_NAME = "coldstart-bench-agent"

# Lifecycle — keep idle timeout short so experiments run faster
IDLE_TIMEOUT_SECONDS = 600   # 10 min idle → session destroyed
MAX_LIFETIME_SECONDS = 600   # 10 min max → VM terminated

# Experiment defaults
DEFAULT_ROUNDS = 5
RESULTS_FILE = "results.json"
