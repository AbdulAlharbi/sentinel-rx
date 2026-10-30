"""Agent interface: anything that takes an Env and returns a RunTrace."""

from __future__ import annotations

from abc import ABC, abstractmethod

from agent.trace import RunTrace
from env.base import Env


class Agent(ABC):
    """An incident-recovery agent."""

    name: str = "abstract"

    @abstractmethod
    def run(self, env: Env, budget: int = 8) -> RunTrace:
        """Diagnose and recover the system within ``budget`` loop iterations."""

    @property
    def model(self) -> str:
        """LLM model name, if any."""
        return ""
