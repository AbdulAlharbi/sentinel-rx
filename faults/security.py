"""Security faults hidden inside operational incidents, plus the held-out unseen fault."""

from __future__ import annotations

from typing import TYPE_CHECKING

from env.types import CONTAINMENT_ACTIONS, Action
from faults.base import Fault

if TYPE_CHECKING:
    from env.sim.world import World


def _key(name: str, target: str) -> str:
    return f"{name}({target})"


class C2Beacon(Fault):
    """A compromised service beacons to a command-and-control server at a jittered interval."""

    type = "c2_beacon"
    security = True
    description = (
        "Malware on the host phones home every `interval` seconds (+/- `jitter`). Restarting the "
        "service before containment destroys forensic evidence and the implant re-establishes."
    )
    symptoms = (
        "small, periodic outbound flows to an unknown external IP; slight cpu increase; no obvious "
        "errors. Hidden among health checks and update polls."
    )

    def __init__(self, spec, rng) -> None:
        super().__init__(spec, rng)
        self.interval = float(self.params.get("interval", 45.0))
        self.jitter = float(self.params.get("jitter", 0.2))
        self.dst = str(self.params.get("dst", "203.0.113.47"))
        self.next_t: float | None = None
        self.contained = False
        self.restart_before_containment = 0
        self.reinfections = 0

    def correct_sequences(self) -> list[list[str]]:
        """Contain (block egress or isolate) first, then restart to eradicate."""
        t = self.target
        return [
            [_key("block_egress", t), _key("restart_service", t)],
            [_key("isolate_host", t), _key("restart_service", t)],
        ]

    def harmful_actions(self, world: World) -> dict[str, str]:
        """Restarting before containment is harmful while the implant is live."""
        if self.resolved_at is None and not self.contained:
            return {
                _key(
                    "restart_service", self.target
                ): "restarted a beaconing host before containment "
                "(evidence destroyed, implant re-established)"
            }
        return {}

    def tick(self, world: World, tick: int) -> None:
        """Emit beacon flows for this tick's time span unless egress is contained."""
        if not self.is_active(tick):
            return
        t0 = world.time_of(tick)
        t1 = t0 + world.tick_seconds
        if self.next_t is None:
            self.next_t = t0 + float(self.rng.uniform(0, self.interval))
        world.effects[self.target].cpu_add += 3.0
        while self.next_t < t1:
            t_ev = round(self.next_t, 3)
            if self.contained:
                # the implant keeps trying; the firewall / host agent records denied attempts
                self.flow(world, t_ev, self.target, self.dst, 0, blocked=True)
                self.log(
                    world,
                    tick,
                    "WARN",
                    f"egress denied by policy: connect {self.dst}:443 (implant still active)",
                )
            elif world.status_state(self.target) == "up":
                self.flow(world, t_ev, self.target, self.dst, int(self.rng.integers(180, 240)))
            self.next_t += self.interval * (
                1.0 + float(self.rng.uniform(-self.jitter, self.jitter))
            )

    def on_action(self, action: Action, world: World, tick: int) -> str | None:
        """Containment then restart resolves; restart first is harmful and re-infects."""
        if action.target != self.target or self.resolved_at is not None:
            return None
        if action.name in CONTAINMENT_ACTIONS:
            self.contained = True
            return "beacon traffic contained (egress cut)"
        if action.name == "restart_service":
            if self.contained:
                self.resolve(tick, "host rebuilt after containment")
                return "implant removed after containment"
            self.restart_before_containment += 1
            self.reinfections += 1
            self.interval = max(10.0, self.interval * float(self.rng.uniform(0.7, 1.3)))
            self.next_t = None
            self.log(world, tick, "INFO", "service started (pid rotated)")
            return "restart without containment: implant re-established with a new interval"
        return None

    def to_dict(self):
        """Serialisable summary."""
        d = super().to_dict()
        d.update({"contained": self.contained, "reinfections": self.reinfections, "dst": self.dst})
        return d


class CredentialStuffing(Fault):
    """Distributed burst of failed logins against the auth endpoint."""

    type = "credential_stuffing"
    security = True
    description = "A botnet replays leaked credentials against /login from many source IPs."
    symptoms = (
        "burst of 'authentication failed' logs from many IPs, cpu up, latency up on the target."
    )

    def correct_sequences(self) -> list[list[str]]:
        """Rate-limiting the auth endpoint resolves."""
        return [[_key("rate_limit_auth", self.target)]]

    def tick(self, world: World, tick: int) -> None:
        """Auth-failure burst."""
        if not self.is_active(tick):
            return
        if world.rate_limited.get(self.target):
            return
        eff = world.effects[self.target]
        eff.cpu_add += 30.0
        eff.latency_mult *= 1.8
        eff.error_add += 0.2  # auth backend saturated: legitimate logins time out
        n = int(self.params.get("burst", 6))
        for _ in range(n):
            octets = self.rng.integers([11, 0, 0, 1], [220, 255, 255, 254])
            ip = ".".join(str(int(o)) for o in octets)
            user = f"user{self.rng.integers(1000, 99999)}"
            self.log(
                world,
                tick,
                "WARN",
                f"authentication failed for {user} from {ip} (invalid password)",
            )
            self.flow(
                world,
                world.time_of(tick) + float(self.rng.uniform(0, world.tick_seconds)),
                ip,
                self.target,
                410,
            )

    def on_action(self, action: Action, world: World, tick: int) -> str | None:
        """Rate limit resolves."""
        if (
            action.name == "rate_limit_auth"
            and action.target == self.target
            and self.is_active(tick)
        ):
            self.resolve(tick, "auth rate limited")
            return "login attempts throttled; attack ineffective"
        return None


class DnsFailure(Fault):
    """Held-out unseen fault: a resolver config push breaks name resolution on one host."""

    type = "dns_failure"
    description = (
        "resolv.conf pushed with a dead nameserver; the host cannot resolve its dependencies."
    )
    symptoms = (
        "error_rate spikes on the target; dependencies look healthy but receive less traffic; "
        "'Name or service not known' in logs."
    )

    def correct_sequences(self) -> list[list[str]]:
        """Roll back the resolver config, or restart to re-read the previous resolver cache."""
        return [[_key("rollback_config", self.target)], [_key("restart_service", self.target)]]

    def tick(self, world: World, tick: int) -> None:
        """Resolution failures."""
        if not self.is_active(tick):
            return
        if tick == self.start:
            self.log(world, tick, "INFO", "config reload: resolver updated (nameserver 10.9.0.53)")
        eff = world.effects[self.target]
        eff.error_add += 0.7
        eff.latency_add += 2000.0
        for dep in world.topology.get(self.target, []):
            world.effects[dep].conn_add -= 0.5 * world.baseline(dep)["conn_count"]
            if tick % 2 == 0:
                self.log(
                    world,
                    tick,
                    "ERROR",
                    f"getaddrinfo failed for {dep}.internal: Name or service not known",
                )

    def on_action(self, action: Action, world: World, tick: int) -> str | None:
        """Rollback or restart resolves."""
        if action.target != self.target or not self.is_active(tick):
            return None
        if action.name in ("rollback_config", "restart_service"):
            self.resolve(tick, f"{action.name} restored resolution")
            return "name resolution restored"
        return None
