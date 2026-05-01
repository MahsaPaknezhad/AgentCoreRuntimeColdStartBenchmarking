FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app
COPY pyproject.toml ./
RUN uv pip install --system --no-cache strands-agents fastapi uvicorn
COPY agent/app.py ./main.py

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
