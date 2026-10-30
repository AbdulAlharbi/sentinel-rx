"""Operational (non-security) faults."""

from __future__ import annotations

from typing import TYPE_CHECKING

from env.types import Action
from faults.base import Fault

if TYPE_CHECKING:
    from env.sim.world import World


def _key(name: str, target: str) -> str:
    return f"{name}({target})"


class MemoryLeak(Fault):
    """Process memory grows until the service thrashes (high latency, errors)."""

    type = "memory_leak"
    description = "A code path leaks heap; RSS climbs linearly until the service thrashes."
    symptoms = (
        "mem rises monotonically; later latency_p95 and error_rate climb; GC warnings in logs."
    )

    def correct_sequences(self) -> list[list[str]]:
        """Restart clears the leaked heap."""
        return [[_key("restart_service", self.target)]]

    def tick(self, world: World, tick: int) -> None:
        """Grow memory, degrade once above the pressure threshold."""
        if not self.is_active(tick):
            return
        rate = float(self.params.get("rate", 1.2))
        base = world.baseline(self.target)["mem"]
        mem = min(97.0, base + rate * (tick - self.start))
        eff = world.effects[self.target]
        eff.mem_set = mem
        if mem > 80:
            pressure = (mem - 80) / 17.0
            eff.latency_mult *= 1.0 + 3.0 * pressure
            eff.error_add += 0.25 * pressure
            eff.cpu_add += 20 * pressure
            if tick % 2 == 0:
                self.log(
                    world,
                    tick,
                    "WARN",
                    f"GC pause {int(200 + 900 * pressure)}ms, heap {mem:.0f}% used",
                )
        elif tick % 5 == 0:
            self.log(world, tick, "INFO", f"heap usage {mem:.0f}%")

    def on_action(self, action: Action, world: World, tick: int) -> str | None:
        """Restart resolves."""
        if (
            action.name == "restart_service"
            and action.target == self.target
            and self.is_active(tick)
        ):
            self.resolve(tick, "restart cleared leaked memory")
            return "memory leak cleared by restart"
        return None


class DbConnExhaustion(Fault):
    """Connection pool of the database fills up; clients get refused."""

    type = "db_conn_exhaustion"
    description = "Connection pool saturates (idle connections not released)."
    symptoms = (
        "db conn_count climbs to the pool limit; 'too many connections' errors on dependents."
    )

    def correct_sequences(self) -> list[list[str]]:
        """Scale the pool or restart the database to drop idle connections."""
        return [[_key("scale_connections", self.target)], [_key("restart_service", self.target)]]

    def tick(self, world: World, tick: int) -> None:
        """Ramp connections up to the pool limit."""
        if not self.is_active(tick):
            return
        limit = world.pool_size[self.target]
        base = world.baseline(self.target)["conn_count"]
        ramp = float(self.params.get("ramp", 8.0))
        conn = min(limit, base + ramp * (tick - self.start))
        eff = world.effects[self.target]
        eff.conn_set = conn
        frac = conn / limit
        if frac >= 0.95:
            eff.error_add += 0.55
            eff.latency_mult *= 2.5
            self.log(world, tick, "ERROR", f"FATAL: too many connections ({int(conn)}/{limit})")
        elif frac > 0.8:
            eff.latency_mult *= 1.4
            self.log(world, tick, "WARN", f"connection pool {int(conn)}/{limit} in use")

    def on_action(self, action: Action, world: World, tick: int) -> str | None:
        """Scaling or restarting the DB resolves."""
        if action.target != self.target or not self.is_active(tick):
            return None
        if action.name == "scale_connections":
            self.resolve(tick, "pool scaled")
            return "connection pool scaled; refusals stopped"
        if action.name == "restart_service":
            self.resolve(tick, "restart dropped idle connections")
            return "idle connections dropped by restart"
        return None


class DiskFull(Fault):
    """A runaway log/tmp writer fills the data volume."""

    type = "disk_full"
    description = "Disk usage climbs to 100%; writes fail."
    symptoms = "disk_used reaches ~100%; 'No space left on device' errors; error_rate climbs."

    def correct_sequences(self) -> list[list[str]]:
        """Clearing the disk resolves."""
        return [[_key("clear_disk", self.target)]]

    def tick(self, world: World, tick: int) -> None:
        """Fill the disk then fail writes."""
        if not self.is_active(tick):
            return
        base = world.baseline(self.target)["disk_used"]
        rate = float(self.params.get("rate", 4.0))
        disk = min(100.0, base + rate * (tick - self.start))
        eff = world.effects[self.target]
        eff.disk_set = disk
        if disk >= 99.5:
            eff.error_add += 0.6
            eff.latency_mult *= 1.5
            self.log(world, tick, "ERROR", "write failed: [Errno 28] No space left on device")
        elif disk > 90 and tick % 2 == 0:
            self.log(world, tick, "WARN", f"disk usage {disk:.0f}% on /var/data")

    def on_action(self, action: Action, world: World, tick: int) -> str | None:
        """Clearing disk resolves."""
        if action.name == "clear_disk" and action.target == self.target and self.is_active(tick):
            self.resolve(tick, "disk cleared")
            return "disk space reclaimed"
        return None


class LatencyInjection(Fault):
    """Degraded network path / stuck connections add latency without errors."""

    type = "latency_injection"
    description = "Network degradation on the host adds hundreds of ms to every call."
    symptoms = (
        "latency_p50/p95 jump on the target and its dependents; retransmit warnings; no errors."
    )

    def correct_sequences(self) -> list[list[str]]:
        """Restart re-establishes connections on a clean path."""
        return [[_key("restart_service", self.target)]]

    def tick(self, world: World, tick: int) -> None:
        """Add latency."""
        if not self.is_active(tick):
            return
        add = float(self.params.get("ms", 350.0))
        eff = world.effects[self.target]
        eff.latency_add += add
        if tick % 3 == 0:
            self.log(
                world,
                tick,
                "WARN",
                f"upstream response time exceeded {int(add)}ms (tcp retransmits)",
            )

    def on_action(self, action: Action, world: World, tick: int) -> str | None:
        """Restart resolves."""
        if (
            action.name == "restart_service"
            and action.target == self.target
            and self.is_active(tick)
        ):
            self.resolve(tick, "restart re-established connections")
            return "latency returned to baseline after restart"
        return None


class BadConfigDeploy(Fault):
    """A bad configuration push breaks a fraction of requests immediately."""

    type = "bad_config_deploy"
    description = "A config push with an invalid route table causes an immediate 5xx spike."
    symptoms = "step change in error_rate right after a 'config reload' log; restart does not help."

    def correct_sequences(self) -> list[list[str]]:
        """Only a rollback resolves."""
        return [[_key("rollback_config", self.target)]]

    def tick(self, world: World, tick: int) -> None:
        """Errors from the bad config."""
        if not self.is_active(tick):
            return
        if tick == self.start:
            world.config_version[self.target] = "v2"
            self.log(
                world, tick, "INFO", "config reload: version v2 applied (routes=47, upstreams=3)"
            )
        err = float(self.params.get("error", 0.4))
        world.effects[self.target].error_add += err
        if tick % 2 == 0:
            self.log(
                world, tick, "ERROR", "handler error: no route matched for upstream 'orders-v2'"
            )

    def on_action(self, action: Action, world: World, tick: int) -> str | None:
        """Rollback resolves; restart just re-applies the bad config."""
        if action.target != self.target or not self.is_active(tick):
            return None
        if action.name == "rollback_config":
            self.resolve(tick, "config rolled back")
            world.config_version[self.target] = "v1"
            return "config rolled back to v1"
        if action.name == "restart_service":
            self.log(
                world, tick, "INFO", "config reload: version v2 applied (routes=47, upstreams=3)"
            )
            return "restart re-applied config v2; errors persist"
        return None


class ProcessCrash(Fault):
    """The process dies and is not restarted by any supervisor."""

    type = "process_crash"
    description = "The service process exits (segfault); nothing restarts it."
    symptoms = "status down, cpu/conn drop to 0, dependents fail with connection refused."

    def correct_sequences(self) -> list[list[str]]:
        """Restart resolves."""
        return [[_key("restart_service", self.target)]]

    def tick(self, world: World, tick: int) -> None:
        """Mark the service down."""
        if not self.is_active(tick):
            return
        if tick == self.start:
            self.log(world, tick, "ERROR", "process exited with signal 11 (SIGSEGV)")
        world.effects[self.target].down = True

    def on_action(self, action: Action, world: World, tick: int) -> str | None:
        """Restart resolves."""
        if (
            action.name == "restart_service"
            and action.target == self.target
            and self.is_active(tick)
        ):
            self.resolve(tick, "process restarted")
            return "process restarted"
        return None


class CacheFailure(Fault):
    """Cache node becomes corrupt/evicting; everything falls through to the database."""

    type = "cache_failure"
    description = "Cache keys corrupted; hit ratio collapses and the DB absorbs the load."
    symptoms = "cache error_rate up, api latency up, db cpu/conn up; 'WRONGTYPE'/eviction logs."

    def correct_sequences(self) -> list[list[str]]:
        """Flushing or restarting the cache resolves."""
        return [[_key("flush_cache", self.target)], [_key("restart_service", self.target)]]

    def tick(self, world: World, tick: int) -> None:
        """Cache errors plus extra DB load."""
        if not self.is_active(tick):
            return
        eff = world.effects[self.target]
        eff.error_add += 0.35
        eff.latency_mult *= 3.0
        self.log(
            world,
            tick,
            "ERROR",
            "WRONGTYPE operation against a key holding the wrong kind of value",
        )
        for dep in world.dependents_of(self.target):
            world.effects[dep].latency_add += 120.0
            self.log(
                world,
                tick,
                "WARN",
                f"cache miss ratio 0.{self.rng.integers(80, 99)} (fallthrough to db)",
                dep,
            )
        if "db" in world.services:
            world.effects["db"].cpu_add += 25.0
            world.effects["db"].conn_add += 15.0

    def on_action(self, action: Action, world: World, tick: int) -> str | None:
        """Flush or restart resolves."""
        if action.target != self.target or not self.is_active(tick):
            return None
        if action.name in ("flush_cache", "restart_service"):
            self.resolve(tick, f"{action.name} cleared corrupt keys")
            return "cache rebuilt"
        return None
