"""Dependency-aware root-cause ranker.

Upstream symptoms (latency, errors) that are explained by an unhealthy downstream dependency
are discounted; intrinsic anomalies (memory, disk, connections, status down, log signatures)
are not, because a dependency cannot cause them.
"""

from __future__ import annotations

from dataclasses import dataclass

from env.types import ServiceStatus


@dataclass
class RankedService:
    """A service with its root-cause score and the reasons."""

    service: str
    score: float
    intrinsic: float
    propagated: float
    explained_by: list[str]
    status: str

    def to_dict(self) -> dict:
        """Compact form for prompts/traces."""
        return {
            "service": self.service,
            "score": round(self.score, 2),
            "intrinsic": round(self.intrinsic, 2),
            "propagated": round(self.propagated, 2),
            "explained_by": self.explained_by,
            "status": self.status,
        }


def rank_services(
    topology: dict[str, list[str]],
    status: dict[str, ServiceStatus],
    metric_scores: dict[str, dict[str, float]],
    log_scores: dict[str, float],
    discount: float = 0.7,
) -> list[RankedService]:
    """Rank services by how likely they are a root cause rather than a victim."""
    services = list(topology)
    intrinsic: dict[str, float] = {}
    propagated: dict[str, float] = {}
    for s in services:
        ms = metric_scores.get(s, {})
        down = 1.0 if status.get(s) and status[s].state != "up" else 0.0
        intrinsic[s] = max(ms.get("intrinsic", 0.0), log_scores.get(s, 0.0), down)
        propagated[s] = ms.get("propagated", 0.0) if not down else 0.0
    out: list[RankedService] = []
    for s in services:
        deps = topology.get(s, [])
        dep_total = [(d, max(intrinsic[d], propagated[d])) for d in deps]
        worst = max((v for _, v in dep_total), default=0.0)
        explained = [d for d, v in dep_total if v >= 0.3]
        residual = max(0.0, propagated[s] - discount * worst)
        score = max(intrinsic[s], residual)
        out.append(
            RankedService(
                s,
                score,
                intrinsic[s],
                propagated[s],
                explained,
                status[s].state if s in status else "?",
            )
        )
    out.sort(key=lambda r: -r.score)
    return out
