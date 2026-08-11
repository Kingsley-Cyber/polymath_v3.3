#!/usr/bin/env python3
"""Polymath vLLM lane controller — native systemd unit, RunPod semantics.

Replaces the docker-era :8085 controller for the NATIVE vllm-qwen unit.
Auth: X-Api-Key or Bearer matching controller-key.txt. Owner VRAM cap is
enforced at /up: refuses to start if serve.sh requests more than the cap.

  GET  /status  -> unit state, health, VRAM, configured utilization, cap
  POST /up      -> cap guard, reset-failed, systemctl start
  POST /down    -> systemctl stop (frees the whole card)
"""
import json
import re
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8086
KEY_FILE = "/home/kingsley/vllm-server/controller-key.txt"
SERVE_SH = "/home/kingsley/vllm-server/serve.sh"
UNIT = "vllm-qwen"
CAP_UTILIZATION = 0.70  # owner cap: 70GB max of the ~98GB card
NVSMI = "/usr/lib/wsl/lib/nvidia-smi"

with open(KEY_FILE) as fh:
    KEY = fh.read().strip()


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def unit_state():
    r = run(["systemctl", "show", UNIT, "-p", "ActiveState", "-p", "SubState"])
    return dict(
        line.split("=", 1) for line in r.stdout.strip().splitlines() if "=" in line
    )


def vram():
    r = run([NVSMI, "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"])
    try:
        used, total = [int(x.strip()) for x in r.stdout.strip().split(",")]
        return {"used_gb": round(used / 1024, 1), "total_gb": round(total / 1024, 1)}
    except Exception:
        return {}


def engine_health():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def configured_utilization():
    try:
        m = re.search(r"--gpu-memory-utilization\s+([0-9.]+)",
                      open(SERVE_SH).read())
        return float(m.group(1)) if m else None
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    def _authed(self):
        supplied = self.headers.get("X-Api-Key", "")
        bearer = self.headers.get("Authorization", "")
        if bearer.startswith("Bearer "):
            supplied = supplied or bearer[7:]
        return supplied.strip() == KEY

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if self.path.rstrip("/") in ("", "/status"):
            return self._send(200, {
                "controller": "native-vllm-qwen",
                "unit": unit_state(),
                "engine_healthy": engine_health(),
                "vram": vram(),
                "gpu_memory_utilization": configured_utilization(),
                "owner_cap_utilization": CAP_UTILIZATION,
            })
        return self._send(404, {"error": "unknown path"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        path = self.path.rstrip("/")
        if path == "/up":
            util = configured_utilization()
            if util is not None and util > CAP_UTILIZATION:
                return self._send(409, {
                    "error": "vram_cap_exceeded",
                    "configured": util,
                    "owner_cap": CAP_UTILIZATION,
                    "fix": f"set --gpu-memory-utilization <= {CAP_UTILIZATION} in serve.sh",
                })
            run(["sudo", "-n", "systemctl", "reset-failed", UNIT])
            r = run(["sudo", "-n", "systemctl", "start", UNIT])
            return self._send(200 if r.returncode == 0 else 500, {
                "action": "start", "rc": r.returncode,
                "stderr": r.stderr.strip()[-300:], "unit": unit_state(),
            })
        if path == "/down":
            r = run(["sudo", "-n", "systemctl", "stop", UNIT])
            return self._send(200 if r.returncode == 0 else 500, {
                "action": "stop", "rc": r.returncode,
                "stderr": r.stderr.strip()[-300:],
                "unit": unit_state(), "vram": vram(),
            })
        return self._send(404, {"error": "unknown path"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
