"""A real local HTTP microservice (stdlib only); one script plays every role.

Faults and actions live in :mod:`env.live.faults`. Memory pressure slows handlers in
proportion to the leaked bytes instead of swapping the laptop (see docs/LIMITATIONS.md).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import resource
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from env.live.faults import DISK_QUOTA_BYTES, act, inject

MEM_NOMINAL_MB = 1024.0


class State:
    """Mutable service state shared by the handler threads."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.name = args.name
        self.role = args.role
        self.port = args.port
        self.deps: dict[str, int] = dict(kv.split("=") for kv in args.deps.split(",") if kv)  # type: ignore[misc]
        self.deps = {k: int(v) for k, v in self.deps.items()}
        self.sandbox = Path(args.sandbox)
        self.sandbox.mkdir(parents=True, exist_ok=True)
        self.c2_port = args.c2_port
        self.updates_port = args.updates_port
        self.flows_path = Path(args.flows)
        self.log_path = Path(args.logs) / f"{self.name}.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.t0 = args.t0
        self.lock = threading.Lock()
        self.latencies: deque[tuple[float, float]] = deque(maxlen=2000)
        self.requests: deque[tuple[float, bool]] = deque(maxlen=2000)
        self.faults: dict[str, dict[str, Any]] = {}
        self.leak: list[bytearray] = []
        self.config_version = "v1"
        self.isolated = False
        self.egress_blocked = args.egress_blocked
        self.rate_limited = False
        self.pool_size = 8 if self.role == "db" else 64
        self.pool = threading.BoundedSemaphore(self.pool_size)
        self.held: list[Any] = []
        self.active_conn = 0
        self.cache: dict[str, str] = {}
        self.queue_depth = 0
        self.cpu_prev = (time.time(), os.times())
        self.cpu_pct = 0.0
        self.started = time.time()
        self.db_path = self.sandbox / "db.sqlite3"
        if self.role == "db":
            con = sqlite3.connect(self.db_path)
            con.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, body TEXT)")
            con.execute("INSERT INTO orders (body) VALUES ('seed')")
            con.commit()
            con.close()
        if args.inject:
            for spec in json.loads(args.inject):
                inject(self, spec["type"], spec.get("params", {}))

    # ---- telemetry ----------------------------------------------------------------------
    def now(self) -> float:
        """Seconds since the environment started."""
        return time.time() - self.t0

    def log(self, level: str, msg: str) -> None:
        """Append a structured log line."""
        with self.lock:
            with self.log_path.open("a") as fh:
                fh.write(json.dumps({"t": round(self.now(), 3), "level": level, "msg": msg}) + "\n")

    def flow(self, dst: str, nbytes: int, blocked: bool = False) -> None:
        """Record an outbound connection attempt."""
        with self.lock:
            with self.flows_path.open("a") as fh:
                fh.write(
                    json.dumps(
                        {
                            "t": round(self.now(), 3),
                            "src": self.name,
                            "dst": dst,
                            "nbytes": nbytes,
                            "blocked": blocked,
                        }
                    )
                    + "\n"
                )

    def record(self, latency_ms: float, ok: bool) -> None:
        """Record one served request."""
        with self.lock:
            self.latencies.append((time.time(), latency_ms))
            self.requests.append((time.time(), ok))

    def metrics(self) -> dict[str, float]:
        """Current metrics snapshot."""
        now = time.time()
        t_prev, times_prev = self.cpu_prev
        times_now = os.times()
        dt = now - t_prev
        if dt >= 1.0:
            cpu = (
                (times_now.user + times_now.system - times_prev.user - times_prev.system) / dt * 100
            )
            self.cpu_pct = max(0.0, min(100.0, cpu))
            self.cpu_prev = (now, times_now)
        with self.lock:
            lats = sorted(ms for t, ms in self.latencies if now - t <= 10.0)
            recent = [ok for t, ok in self.requests if now - t <= 10.0]
        p50 = lats[len(lats) // 2] if lats else 0.0
        p95 = lats[int(len(lats) * 0.95)] if lats else 0.0
        err = (1 - sum(recent) / len(recent)) if recent else 0.0
        rss_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            rss_bytes *= 1024
        disk = sum(f.stat().st_size for f in self.sandbox.glob("fill_*.bin"))
        return {
            "latency_p50": round(p50, 2),
            "latency_p95": round(p95, 2),
            "error_rate": round(err, 4),
            "cpu": round(self.cpu_pct, 2),
            "mem": round(rss_bytes / (MEM_NOMINAL_MB * 1024 * 1024) * 100, 2),
            "conn_count": float(self.active_conn + len(self.held)),
            "disk_used": round(disk / DISK_QUOTA_BYTES * 100, 2),
            "queue_depth": float(self.queue_depth),
        }

    # ---- outbound -------------------------------------------------------------------------
    def call(self, dep: str, path: str, timeout: float = 2.0) -> tuple[int, bytes]:
        """HTTP GET a dependency, recording the flow."""
        port = self.deps.get(dep)
        if port is None:
            raise RuntimeError(f"unknown dependency {dep}")
        if self.isolated:
            self.flow(dep, 0, blocked=True)
            raise RuntimeError("host isolated")
        self.flow(dep, 300)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


# ---- HTTP --------------------------------------------------------------------------------
def make_handler(st: State):
    """Build the request handler bound to the state."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # silence default logging
            pass

        def _send(self, code: int, obj: Any) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b""
            if self.path.startswith("/admin/"):
                payload = json.loads(raw or b"{}")
                if self.path == "/admin/inject":
                    return self._send(
                        200, {"msg": inject(st, payload["type"], payload.get("params", {}))}
                    )
                if self.path == "/admin/action":
                    return self._send(200, {"msg": act(st, payload["name"])})
                return self._send(404, {})
            if self.path == "/login":
                ip = self.headers.get("X-Forwarded-For", "127.0.0.1")
                if st.rate_limited:
                    st.log("INFO", f"login rate limited for {ip}")
                    return self._send(429, {"error": "rate limited"})
                st.log(
                    "WARN",
                    f"authentication failed for user{random.randint(1000, 99999)} from {ip} "
                    "(invalid password)",
                )
                return self._send(401, {"error": "invalid credentials"})
            return self._send(404, {})

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/metrics":
                return self._send(200, st.metrics())
            if self.path == "/admin/status":
                return self._send(
                    200,
                    {
                        "name": st.name,
                        "faults": sorted(st.faults),
                        "config_version": st.config_version,
                        "isolated": st.isolated,
                        "egress_blocked": st.egress_blocked,
                        "rate_limited": st.rate_limited,
                        "pool_size": st.pool_size,
                    },
                )
            if st.isolated:
                return self._send(503, {"error": "host isolated"})
            if self.path == "/health":
                return self._send(200, {"status": "ok"})
            t0 = time.perf_counter()
            code, obj = self._work()
            ms = (time.perf_counter() - t0) * 1000
            st.record(ms, code < 500)
            if code < 500 and random.random() < 0.3:
                st.log("INFO", f"{self.path} {code} {ms:.0f}ms")
            return self._send(code, obj)

        def _work(self) -> tuple[int, Any]:
            if "latency_injection" in st.faults:
                time.sleep(float(st.faults["latency_injection"]["params"].get("ms", 300)) / 1000)
            if st.leak:
                time.sleep(min(1.5, len(st.leak) * 0.012))
                if len(st.leak) > 40 and random.random() < 0.3:
                    return 503, {"error": "out of memory handling request"}
            if "bad_config_deploy" in st.faults and random.random() < 0.4:
                st.log("ERROR", "handler error: no route matched for upstream 'orders-v2'")
                return 500, {"error": "no route matched"}
            match st.role:
                case "gateway":
                    try:
                        code, body = st.call("api", "/work")
                    except Exception as e:  # noqa: BLE001
                        st.log("ERROR", f"upstream api unreachable: {e}")
                        return 502, {"error": "api unreachable"}
                    return (code if code >= 500 else 200), {"via": "gateway"}
                case "api":
                    try:
                        c_code, _ = st.call("cache", "/get?k=order:1")
                    except Exception:  # noqa: BLE001
                        c_code = 500
                    if c_code >= 500:
                        st.log("WARN", "cache miss ratio 0.95 (fallthrough to db)")
                    try:
                        d_code, _ = st.call("db", "/query")
                    except Exception as e:  # noqa: BLE001
                        st.log("ERROR", f"db unreachable: {e}")
                        return 502, {"error": "db unreachable"}
                    if d_code >= 500:
                        st.log("ERROR", "db query failed: too many connections")
                        return 503, {"error": "db error"}
                    return 200, {"order": 1}
                case "db":
                    if "disk_full" in st.faults and st.metrics()["disk_used"] >= 99.0:
                        return 507, {"error": "No space left on device"}
                    if not st.pool.acquire(timeout=0.05):
                        return 503, {"error": "too many connections"}
                    st.active_conn += 1
                    try:
                        con = sqlite3.connect(st.db_path, timeout=1)
                        con.execute("SELECT count(*) FROM orders").fetchone()
                        con.close()
                        return 200, {"rows": 1}
                    finally:
                        st.active_conn -= 1
                        st.pool.release()
                case "cache":
                    if "cache_failure" in st.faults and random.random() < 0.35:
                        st.log(
                            "ERROR",
                            "WRONGTYPE operation against a key holding the wrong kind of value",
                        )
                        return 500, {"error": "WRONGTYPE"}
                    return 200, {"v": st.cache.get("order:1", "")}
                case _:
                    return 200, {"queue_depth": st.queue_depth}

    return Handler


def _worker_loop(st: State) -> None:
    while True:
        st.queue_depth = max(0, st.queue_depth + random.randint(0, 2))
        t0 = time.perf_counter()
        ok = True
        try:
            code, _ = st.call("db", "/query")
            ok = code < 500
            st.call("cache", "/get?k=job")
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            st.queue_depth = max(0, st.queue_depth - 2)
            if random.random() < 0.3:
                st.log(
                    "INFO",
                    f"job {random.randint(1000, 99999)} completed in "
                    f"{(time.perf_counter() - t0) * 1000:.0f}ms",
                )
        st.record((time.perf_counter() - t0) * 1000, ok)
        time.sleep(0.5)


def _update_poll_loop(st: State) -> None:
    time.sleep(random.uniform(1, 20))
    while True:
        if not st.egress_blocked and not st.isolated:
            st.flow("updates.internal", 850)
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{st.updates_port}/updates", timeout=1
                ).read()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(60.0)


def main() -> None:
    """Entry point."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--role", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--deps", default="")
    ap.add_argument("--sandbox", required=True)
    ap.add_argument("--flows", required=True)
    ap.add_argument("--logs", required=True)
    ap.add_argument("--c2-port", type=int, default=0)
    ap.add_argument("--updates-port", type=int, default=0)
    ap.add_argument("--t0", type=float, required=True)
    ap.add_argument("--egress-blocked", action="store_true")
    ap.add_argument("--inject", default="")
    args = ap.parse_args()
    st = State(args)
    if st.role == "worker":
        threading.Thread(target=_worker_loop, args=(st,), daemon=True).start()
    threading.Thread(target=_update_poll_loop, args=(st,), daemon=True).start()
    st.log("INFO", f"service started on port {st.port}")
    server = ThreadingHTTPServer(("127.0.0.1", st.port), make_handler(st))
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
