"""LiveEnv: the same five services as real local processes behind the same Env interface.

Everything stays on localhost; all child processes and the sandbox directory are removed on
exit (atexit + SIGINT/SIGTERM handlers). The C2 "server" is a local sinkhole; flow records
carry the logical destination (203.0.113.47) so the detectors see what they would see in
production.
"""

from __future__ import annotations

import atexit
import json
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from env.base import Env
from env.sim.world import TOPOLOGY
from env.types import (
    ACTION_ALLOWLIST,
    Action,
    ActionResult,
    Flow,
    LogLine,
    MetricSample,
    ServiceStatus,
)

ROLES = ("gateway", "api", "db", "cache", "worker")
SAMPLE_INTERVAL = 2.0
ACTION_SETTLE_S = 4.0
ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(url: str, data: dict | None = None, timeout: float = 2.0) -> Any:
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


class _Sinkhole(BaseHTTPRequestHandler):
    """Accepts beacon check-ins and update polls; counts them."""

    hits: dict[str, int] = {"gate": 0, "updates": 0}

    def log_message(self, *args: Any) -> None:  # noqa: D102
        pass

    def do_GET(self) -> None:  # noqa: N802, D102
        key = "gate" if self.path.startswith("/gate") else "updates"
        _Sinkhole.hits[key] += 1
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LiveEnv(Env):
    """Real processes, real telemetry files, real (sandboxed) faults."""

    def __init__(
        self, workdir: Path | None = None, load_rps: float = 6.0, verbose: bool = False
    ) -> None:
        self.workdir = workdir or (ROOT / "env" / "live" / "_sandbox")
        self.load_rps = load_rps
        self.verbose = verbose
        self.procs: dict[str, subprocess.Popen] = {}
        self.ports: dict[str, int] = {}
        self.injected: dict[str, list[dict[str, Any]]] = {}
        self.contained: set[str] = set()
        self.egress_blocked: set[str] = set()
        self.beacon_reinfections = 0
        self.restart_before_containment = 0
        self._metrics: list[MetricSample] = []
        self._stop = threading.Event()
        self.action_log: list[dict[str, Any]] = []
        self._started = False
        atexit.register(self.close)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._on_signal)
        self.reset()

    # ---- lifecycle ------------------------------------------------------------------------
    def _on_signal(self, signum, frame) -> None:  # noqa: ANN001
        self.close()
        sys.exit(130)

    def reset(self) -> None:
        """(Re)start the sinkhole, all services, the load generator and the monitor."""
        self.close()
        shutil.rmtree(self.workdir, ignore_errors=True)
        (self.workdir / "logs").mkdir(parents=True)
        self.flows_path = self.workdir / "flows.jsonl"
        self.flows_path.touch()
        self.t0 = time.time()
        self._stop = threading.Event()
        self._metrics = []
        self.injected = {}
        self.contained = set()
        self.egress_blocked = set()
        self.sink_port = _free_port()
        self.sink = ThreadingHTTPServer(("127.0.0.1", self.sink_port), _Sinkhole)
        self.sink.daemon_threads = True
        threading.Thread(target=self.sink.serve_forever, daemon=True).start()
        self.ports = {r: _free_port() for r in ROLES}
        for role in ("db", "cache", "api", "worker", "gateway"):
            self._launch(role)
        self._wait_healthy()
        self._started = True
        threading.Thread(target=self._load_loop, daemon=True).start()
        threading.Thread(target=self._monitor_loop, daemon=True).start()

    def _launch(self, role: str, inject: list[dict[str, Any]] | None = None) -> None:
        deps = ",".join(f"{d}={self.ports[d]}" for d in TOPOLOGY[role])
        cmd = [
            sys.executable,
            "-m",
            "env.live.service",
            "--name",
            role,
            "--role",
            role,
            "--port",
            str(self.ports[role]),
            "--deps",
            deps,
            "--sandbox",
            str(self.workdir / "sandbox" / role),
            "--flows",
            str(self.flows_path),
            "--logs",
            str(self.workdir / "logs"),
            "--c2-port",
            str(self.sink_port),
            "--updates-port",
            str(self.sink_port),
            "--t0",
            str(self.t0),
        ]
        if role in self.egress_blocked:
            cmd.append("--egress-blocked")
        if inject:
            cmd += ["--inject", json.dumps(inject)]
        self.procs[role] = subprocess.Popen(
            cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def _wait_healthy(self, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        for role in ROLES:
            while time.time() < deadline:
                try:
                    _http(f"http://127.0.0.1:{self.ports[role]}/health", timeout=0.5)
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(0.2)

    def close(self) -> None:
        """Stop everything and delete the sandbox."""
        self._stop.set()
        for p in self.procs.values():
            if p.poll() is None:
                p.terminate()
        for p in self.procs.values():
            try:
                p.wait(timeout=3)
            except Exception:  # noqa: BLE001
                p.kill()
        self.procs = {}
        if getattr(self, "sink", None):
            self.sink.shutdown()
            self.sink.server_close()
            self.sink = None
        if self._started:
            shutil.rmtree(self.workdir, ignore_errors=True)
            self._started = False

    # ---- background threads ---------------------------------------------------------------
    def _load_loop(self) -> None:
        while not self._stop.is_set():
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.ports['gateway']}/work", timeout=3
                ).read()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0 / self.load_rps)

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            t = self.now()
            for role in ROLES:
                self._record_flow(t, "monitor", role, 120)
                try:
                    m = _http(f"http://127.0.0.1:{self.ports[role]}/metrics", timeout=1.0)
                    self._metrics.append(
                        MetricSample(round(t, 3), role, {k: float(v) for k, v in m.items()})
                    )
                except Exception:  # noqa: BLE001
                    self._metrics.append(
                        MetricSample(
                            round(t, 3),
                            role,
                            {
                                "latency_p50": 0.0,
                                "latency_p95": 0.0,
                                "error_rate": 1.0,
                                "cpu": 0.0,
                                "mem": 0.0,
                                "conn_count": 0.0,
                                "disk_used": 0.0,
                                "queue_depth": 0.0,
                            },
                        )
                    )
            time.sleep(SAMPLE_INTERVAL)

    def _record_flow(self, t: float, src: str, dst: str, nbytes: int) -> None:
        with self.flows_path.open("a") as fh:
            fh.write(
                json.dumps(
                    {"t": round(t, 3), "src": src, "dst": dst, "nbytes": nbytes, "blocked": False}
                )
                + "\n"
            )

    # ---- Env interface --------------------------------------------------------------------
    def now(self) -> float:
        """Seconds since start."""
        return time.time() - self.t0

    def list_services(self) -> list[str]:
        """Service names."""
        return list(ROLES)

    def get_topology(self) -> dict[str, list[str]]:
        """Dependency graph (same as the simulator)."""
        return {k: list(v) for k, v in TOPOLOGY.items()}

    def _admin(self, role: str) -> dict[str, Any] | None:
        try:
            return _http(f"http://127.0.0.1:{self.ports[role]}/admin/status", timeout=1.0)
        except Exception:  # noqa: BLE001
            return None

    def get_status(self) -> dict[str, ServiceStatus]:
        """Per-service status from the admin endpoint (down if unreachable)."""
        out = {}
        for role in ROLES:
            st = self._admin(role)
            if st is None:
                out[role] = ServiceStatus(role, "down")
            else:
                state = "isolated" if st["isolated"] else "up"
                out[role] = ServiceStatus(
                    role,
                    state,
                    st["egress_blocked"],
                    st["rate_limited"],
                    st["config_version"],
                    st["pool_size"],
                )
        return out

    def get_metrics(self, window: float) -> list[MetricSample]:
        """Samples in the window."""
        cutoff = self.now() - window
        return [m for m in self._metrics if m.t >= cutoff]

    def get_logs(
        self, service: str | None, window: float, grep: str | None = None
    ) -> list[LogLine]:
        """Read the services' JSONL logs."""
        cutoff = self.now() - window
        out: list[LogLine] = []
        for path in sorted((self.workdir / "logs").glob("*.log")):
            name = path.stem
            if service and name != service:
                continue
            for raw in path.read_text().splitlines():
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if d["t"] >= cutoff and (not grep or grep.lower() in d["msg"].lower()):
                    out.append(LogLine(d["t"], name, d["level"], d["msg"]))
        out.sort(key=lambda line: line.t)
        return out

    def get_flows(self, window: float) -> list[Flow]:
        """Read the shared flows file."""
        cutoff = self.now() - window
        out = []
        for raw in self.flows_path.read_text().splitlines():
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if d["t"] >= cutoff:
                out.append(Flow(d["t"], d["src"], d["dst"], d["nbytes"], d.get("blocked", False)))
        out.sort(key=lambda f: f.t)
        return out

    def external_allowlist(self) -> set[str]:
        """Known-good external destinations."""
        return {"updates.internal"}

    def snapshot(self) -> dict[str, Any]:
        """Observable snapshot."""
        return {
            "t": self.now(),
            "status": {k: v.to_dict() for k, v in self.get_status().items()},
            "health": self.health_score(),
        }

    def wait(self, seconds: float) -> None:
        """Real time passes."""
        time.sleep(seconds)

    # ---- faults & actions -----------------------------------------------------------------
    def inject(self, ftype: str, target: str, params: dict[str, Any] | None = None) -> str:
        """Inject a fault into a running service."""
        spec = {"type": ftype, "params": params or {}}
        self.injected.setdefault(target, []).append(spec)
        try:
            r = _http(f"http://127.0.0.1:{self.ports[target]}/admin/inject", spec)
            return r.get("msg", "")
        except (urllib.error.URLError, ConnectionError, OSError):
            return "injected (process exited)"

    def apply_action(self, action: Action) -> ActionResult:
        """Apply an allowlisted action to a real process."""
        t = self.now()
        if action.name not in ACTION_ALLOWLIST or action.target not in ROLES:
            return ActionResult(action, False, "not allowlisted", t)
        target = action.target
        harmful = None
        msg = ""
        if action.name == "restart_service":
            beacon = any(s["type"] == "c2_beacon" for s in self.injected.get(target, []))
            reinject = []
            if beacon and target not in self.contained:
                harmful = "restarted a beaconing host before containment (implant re-executes)"
                self.restart_before_containment += 1
                self.beacon_reinfections += 1
                reinject = [s for s in self.injected[target] if s["type"] == "c2_beacon"]
                msg = "restarted; implant re-established on boot"
            else:
                self.injected.pop(target, None)
                self.egress_blocked.discard(target)  # rebuilt host rejoins the network
                msg = "restarted clean"
            p = self.procs.get(target)
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    p.kill()
            self._launch(target, inject=reinject or None)
            self._wait_healthy(timeout=5)
        else:
            try:
                msg = _http(
                    f"http://127.0.0.1:{self.ports[target]}/admin/action", {"name": action.name}
                ).get("msg", "")
            except Exception as e:  # noqa: BLE001
                return ActionResult(action, False, f"service unreachable: {e}", t)
            if action.name in ("isolate_host", "block_egress"):
                self.contained.add(target)
                self.egress_blocked.add(target)
            resolved = {
                "rollback_config": "bad_config_deploy",
                "scale_connections": "db_conn_exhaustion",
                "clear_disk": "disk_full",
                "flush_cache": "cache_failure",
                "rate_limit_auth": "credential_stuffing",
            }.get(action.name)
            if resolved and target in self.injected:
                self.injected[target] = [s for s in self.injected[target] if s["type"] != resolved]
        self.action_log.append(
            {"t": t, "action": action.to_dict(), "notes": [msg], "harmful": harmful}
        )
        time.sleep(ACTION_SETTLE_S)
        return ActionResult(action, True, msg, t)

    def undo_action(self, action: Action) -> bool:
        """Inverse of containment / rate limiting."""
        inverse = {
            "isolate_host": "unisolate",
            "block_egress": "unblock_egress",
            "rate_limit_auth": "unrate_limit",
        }.get(action.name)
        if inverse is None:
            return False
        try:
            _http(f"http://127.0.0.1:{self.ports[action.target]}/admin/action", {"name": inverse})
        except Exception:  # noqa: BLE001
            return False
        self.contained.discard(action.target)
        self.egress_blocked.discard(action.target)
        return True

    # ---- ground truth (demo narration only) -----------------------------------------------
    def gt_summary(self) -> dict[str, Any]:
        """What is still injected, per service (from the admin endpoints + env bookkeeping)."""
        active = {}
        for role in ROLES:
            st = self._admin(role)
            active[role] = sorted(st["faults"]) if st else ["down"]
            if (
                any(
                    s["type"] in ("latency_injection", "memory_leak")
                    for s in self.injected.get(role, [])
                )
                and st
            ):
                active[role] = sorted(set(active[role]) | {s["type"] for s in self.injected[role]})
        return {
            "active_faults": active,
            "all_faults_resolved": all(not v for v in active.values()),
            "restart_before_containment": self.restart_before_containment,
            "sinkhole_hits": dict(_Sinkhole.hits),
            "actions": list(self.action_log),
        }
