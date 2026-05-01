"""Shared configuration for the cold start benchmark."""

REGION = "us-west-2"
ACCOUNT_ID = "CHANGE_ME"  # Your AWS account ID
ROLE_ARN = "CHANGE_ME"    # IAM role ARN for AgentCore runtimes

# Runtime names
ZIP_RUNTIME_NAME = "coldstart-bench-zip"
DOCKER_RUNTIME_NAME = "coldstart-bench-docker"

# ECR repository for the Docker variant
ECR_REPO_NAME = "coldstart-bench-agent"

# Lifecycle — keep idle timeout short so experiments run faster
IDLE_TIMEOUT_SECONDS = 120   # 2 min idle → session destroyed
MAX_LIFETIME_SECONDS = 300   # 5 min max

# Experiment defaults
DEFAULT_ROUNDS = 5
RESULTS_FILE = "results.json"
