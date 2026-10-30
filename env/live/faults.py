"""In-process fault injection and remediation actions for the live service.

Faults are *real* where it is safe on a laptop (memory actually leaks, a process actually
dies, the DB pool is actually held, files are actually written into a sandbox directory, a
beacon thread actually makes periodic HTTP calls to a local sinkhole) and emulated where the
real thing would damage the host (disk "fullness" is a quota on the sandbox directory).
"""

from __future__ import annotations

import os
import random
import threading
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from env.live.service import State

C2_LOGICAL_DST = "203.0.113.47"
DISK_QUOTA_BYTES = 120 * 1024 * 1024


# ---- faults ------------------------------------------------------------------------------
def inject(st: State, ftype: str, params: dict[str, Any]) -> str:
    """Start a fault inside this process."""
    st.faults[ftype] = {"params": params, "started": st.now()}
    match ftype:
        case "memory_leak":
            threading.Thread(target=_leak_loop, args=(st, params), daemon=True).start()
        case "process_crash":
            st.log("ERROR", "process exited with signal 11 (SIGSEGV)")
            threading.Timer(0.2, lambda: os._exit(1)).start()
        case "db_conn_exhaustion":
            threading.Thread(target=_hold_pool, args=(st,), daemon=True).start()
        case "disk_full":
            threading.Thread(target=_fill_disk, args=(st,), daemon=True).start()
        case "c2_beacon":
            threading.Thread(target=_beacon_loop, args=(st, params), daemon=True).start()
        case "credential_stuffing":
            threading.Thread(target=_stuffing_loop, args=(st, params), daemon=True).start()
        case "bad_config_deploy":
            st.config_version = "v2"
            st.log("INFO", "config reload: version v2 applied (routes=47, upstreams=3)")
        case "latency_injection" | "cache_failure":
            pass
        case _:
            del st.faults[ftype]
            return f"unsupported fault {ftype}"
    return f"injected {ftype}"


def _leak_loop(st: State, params: dict[str, Any]) -> None:
    step_mb = float(params.get("mb_per_s", 6.0))
    cap_mb = float(params.get("cap_mb", 420.0))
    while "memory_leak" in st.faults and len(st.leak) * step_mb < cap_mb:
        st.leak.append(bytearray(int(step_mb * 1024 * 1024)))
        if len(st.leak) % 5 == 0:
            st.log(
                "WARN", f"GC pause {80 + len(st.leak) * 6}ms, heap {len(st.leak) * step_mb:.0f}MB"
            )
        time.sleep(1.0)


def _hold_pool(st: State) -> None:
    while "db_conn_exhaustion" in st.faults:
        if st.pool.acquire(timeout=0.2):
            st.held.append(object())
            if len(st.held) >= st.pool_size:
                st.log("ERROR", f"FATAL: too many connections ({len(st.held)}/{st.pool_size})")
        time.sleep(0.4)


def _fill_disk(st: State) -> None:
    i = 0
    while "disk_full" in st.faults:
        used = sum(f.stat().st_size for f in st.sandbox.glob("fill_*.bin"))
        if used >= DISK_QUOTA_BYTES:
            st.log("ERROR", "write failed: [Errno 28] No space left on device")
            time.sleep(1.0)
            continue
        (st.sandbox / f"fill_{i:04d}.bin").write_bytes(os.urandom(4 * 1024 * 1024))
        i += 1
        if i % 5 == 0:
            st.log("WARN", f"disk usage {used / DISK_QUOTA_BYTES * 100:.0f}% on /var/data")
        time.sleep(0.5)


def _beacon_loop(st: State, params: dict[str, Any]) -> None:
    interval = float(params.get("interval", 4.0))
    jitter = float(params.get("jitter", 0.2))
    rng = random.Random(params.get("seed", 1))
    while "c2_beacon" in st.faults:
        time.sleep(interval * (1 + rng.uniform(-jitter, jitter)))
        if st.egress_blocked or st.isolated:
            st.flow(C2_LOGICAL_DST, 0, blocked=True)
            st.log(
                "WARN",
                f"egress denied by policy: connect {C2_LOGICAL_DST}:443 (implant still active)",
            )
            continue
        st.flow(C2_LOGICAL_DST, 200 + rng.randint(0, 40))
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{st.c2_port}/gate?id={rng.randint(0, 9999)}", timeout=1
            ).read()
        except Exception:  # noqa: BLE001 - sinkhole may be down
            pass


def _stuffing_loop(st: State, params: dict[str, Any]) -> None:
    rng = random.Random(3)
    rate = float(params.get("per_s", 8.0))
    while "credential_stuffing" in st.faults:
        time.sleep(1.0 / rate)
        ip = ".".join(
            str(rng.randint(lo, hi)) for lo, hi in ((11, 220), (0, 255), (0, 255), (1, 254))
        )
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{st.port}/login",
                data=b"user=bob&pw=x",
                headers={"X-Forwarded-For": ip},
            )
            urllib.request.urlopen(req, timeout=1).read()
        except urllib.error.HTTPError:
            pass
        except Exception:  # noqa: BLE001
            pass


# ---- actions -------------------------------------------------------------------------------
def act(st: State, name: str) -> str:
    """Apply an in-process action."""
    match name:
        case "rollback_config":
            st.config_version = "v1"
            st.faults.pop("bad_config_deploy", None)
            st.log("INFO", "config rollback: version v1 applied")
        case "scale_connections":  # bigger pool + idle connections are reaped
            st.faults.pop("db_conn_exhaustion", None)
            st.pool_size *= 2
            st.held.clear()
            st.pool = threading.BoundedSemaphore(st.pool_size)
        case "clear_disk":
            st.faults.pop("disk_full", None)
            for f in st.sandbox.glob("fill_*.bin"):
                f.unlink()
        case "isolate_host":
            st.isolated = True
            st.egress_blocked = True
        case "block_egress":
            st.egress_blocked = True
        case "flush_cache":
            st.cache.clear()
            st.faults.pop("cache_failure", None)
        case "rate_limit_auth":
            st.rate_limited = True
            st.faults.pop("credential_stuffing", None)
        case "unisolate":
            st.isolated = False
        case "unblock_egress":
            st.egress_blocked = False
        case "unrate_limit":
            st.rate_limited = False
        case _:
            return f"unsupported action {name}"
    return f"{name} applied"
