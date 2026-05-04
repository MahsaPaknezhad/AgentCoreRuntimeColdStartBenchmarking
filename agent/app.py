"""Strands agent for AgentCore cold start benchmarking.

POST /invocations — runs a Strands agent, returns timing breakdown
GET  /ping        — returns Healthy
"""

import os
import time
import uuid

_START_TIME = time.time()
_VM_ID = uuid.uuid4().hex  # Unique per process — if two sessions see the same value, same VM
_PID = os.getpid()

from fastapi import FastAPI, Request
from strands import Agent

app = FastAPI()
agent = Agent(system_prompt="You are a helpful assistant. Be concise.")


@app.post("/invocations")
async def invocations(request: Request):
    body = await request.json()
    prompt = body.get("prompt") or body.get("input", {}).get("prompt") or "say hello"

    t0 = time.time()
    result = agent(prompt)
    agent_ms = (time.time() - t0) * 1000

    return {
        "message": str(result),
        "uptime_s": round(time.time() - _START_TIME, 3),
        "agent_ms": round(agent_ms, 1),
        "vm_id": _VM_ID,
        "pid": _PID,
    }


@app.get("/ping")
async def ping():
    return {"status": "Healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
