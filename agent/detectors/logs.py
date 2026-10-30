"""Log signature detector: template extraction + new/surging error templates."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from env.types import LogLine

_NUM = re.compile(r"0x[0-9a-f]+|\d+(?:\.\d+)*")
_UUIDISH = re.compile(r"\b[0-9a-f]{8,}\b")
SEVERITY_WEIGHT = {
    "ERROR": 1.0,
    "FATAL": 1.0,
    "WARN": 0.6,
    "WARNING": 0.6,
    "INFO": 0.2,
    "DEBUG": 0.05,
}

LogBaseline = tuple[Counter, float]  # template counts, duration in seconds


def template_of(msg: str) -> str:
    """Replace numbers, hex ids and IPs with a placeholder."""
    t = _UUIDISH.sub("<*>", msg.lower())
    t = _NUM.sub("<*>", t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class LogSignature:
    """A template that is new or surging in the recent window."""

    service: str
    level: str
    template: str
    recent_count: int
    baseline_count: int
    example: str
    score: float

    def to_dict(self) -> dict:
        """Compact form for prompts/traces."""
        return {
            "service": self.service,
            "level": self.level,
            "template": self.template[:90],
            "recent": self.recent_count,
            "baseline": self.baseline_count,
            "example": self.example[:120],
            "score": round(self.score, 2),
        }


def log_baseline(
    lines: list[LogLine], now: float, window: float, baseline_frac: float = 0.3
) -> LogBaseline:
    """Template counts over the earliest ``baseline_frac`` of the window."""
    t_start = now - window
    t_base_end = t_start + baseline_frac * window
    base: Counter = Counter()
    for line in lines:
        if line.t <= t_base_end:
            base[(line.service, line.level.upper(), template_of(line.msg))] += 1
    return base, baseline_frac * window


def detect_log_signatures(
    lines: list[LogLine],
    now: float,
    window: float,
    baseline: LogBaseline | None = None,
    baseline_frac: float = 0.3,
    recent_frac: float = 0.35,
    top_k: int = 12,
) -> list[LogSignature]:
    """Rank templates whose recent frequency departs from the baseline, weighted by severity.

    With an explicit ``baseline`` (stored pre-incident counts), every line in ``lines`` is
    treated as recent; otherwise the first part of the window is the reference.
    """
    if baseline is None:
        base, base_dur = log_baseline(lines, now, window, baseline_frac)
        t_recent_start = now - recent_frac * window
        recent_dur = recent_frac * window
    else:
        base, base_dur = baseline
        t_recent_start = now - window
        recent_dur = window
    recent: Counter = Counter()
    examples: dict[tuple, str] = {}
    for line in lines:
        if line.t >= t_recent_start:
            key = (line.service, line.level.upper(), template_of(line.msg))
            recent[key] += 1
            examples.setdefault(key, line.msg)
    out: list[LogSignature] = []
    scale = recent_dur / base_dur if base_dur > 0 else 1.0
    for key, rc in recent.items():
        bc = base.get(key, 0)
        expected = bc * scale
        surge = rc / (expected + 1.0)
        if bc == 0 and rc >= 2:
            surge = max(surge, 5.0)
        if surge < 3.0:
            continue
        w = SEVERITY_WEIGHT.get(key[1], 0.3)
        score = w * min(10.0, surge) * (1 + min(rc, 50) / 25)
        if score < 1.0:
            continue
        out.append(LogSignature(key[0], key[1], key[2], rc, bc, examples[key], score))
    out.sort(key=lambda s: -s.score)
    return out[:top_k]


def service_log_scores(sigs: list[LogSignature]) -> dict[str, float]:
    """Per-service log anomaly strength in [0, 1]."""
    scores: dict[str, float] = {}
    for s in sigs:
        scores[s.service] = max(scores.get(s.service, 0.0), min(1.0, s.score / 15.0))
    return scores
