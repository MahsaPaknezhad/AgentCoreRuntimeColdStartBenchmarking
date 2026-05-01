"""Minimal echo agent for AgentCore cold start benchmarking.

Satisfies the AgentCore contract:
  POST /invocations  — echoes the input back
  GET  /ping         — returns Healthy
"""

import json
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

_START_TIME = time.time()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            self._respond(200, {"status": "Healthy"})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/invocations":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            self._respond(200, {
                "echo": body,
                "uptime_s": round(time.time() - _START_TIME, 3),
            })
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass  # silence per-request logs


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8080), Handler)
    print("Agent listening on :8080")
    server.serve_forever()
