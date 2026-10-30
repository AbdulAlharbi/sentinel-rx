"""Named agent configurations: the full agent, baselines and ablations."""

from __future__ import annotations

from agent.base import Agent

BASELINES = ("runbook", "single_shot", "llm_only", "do_nothing", "random")
ABLATIONS = ("no_verify", "no_loop", "no_beacon", "no_safety")
ALL_CONFIGS = ("agent",) + BASELINES + ABLATIONS

DESCRIPTIONS: dict[str, str] = {
    "agent": "Sentinel-RX: detectors + LLM hypothesis loop + safety layer + verification gate",
    "runbook": "static if-symptom-then-action rules, no LLM",
    "do_nothing": "never acts; claims recovery iff observable health >= 0.9 (sanity floor)",
    "random": "1-3 random allowlisted actions per iteration; claims recovery when health >= 0.9",
    "single_shot": "one LLM call on a telemetry summary, acts, no loop, no verification",
    "llm_only": "the full loop but WITHOUT detectors (raw telemetry summary in the prompt)",
    "no_verify": "agent minus the verification gate (claims recovery after acting)",
    "no_loop": "agent minus the hypothesis loop (one iteration only)",
    "no_beacon": "agent minus the beacon detector",
    "no_safety": "agent minus the safety layer (no allowlist/ordering/rollback)",
}


def build_agent(
    config: str,
    model: str = "qwen2.5:7b",
    replay: bool = False,
    dry_run: bool = False,
    seed: int = 0,
) -> Agent:
    """Instantiate the agent for a config name."""
    if config == "runbook":
        from agent.runbook import RunbookAgent

        return RunbookAgent()
    if config == "do_nothing":
        from agent.baselines import DoNothingAgent

        return DoNothingAgent()
    if config == "random":
        from agent.baselines import RandomAgent

        return RandomAgent(seed=seed)
    from agent.llm import LLMClient

    llm = LLMClient(model=model, replay=replay)
    if config == "single_shot":
        from agent.baselines import SingleShotAgent

        return SingleShotAgent(llm)
    from agent.loop import SentinelAgent

    flags = {
        "agent": {},
        "llm_only": {"use_detectors": False},
        "no_verify": {"verify": False},
        "no_loop": {"max_iterations": 1},
        "no_beacon": {"use_beacon": False},
        "no_safety": {"safety": False},
    }
    if config not in flags:
        raise KeyError(f"unknown config {config!r}; choose from {ALL_CONFIGS}")
    return SentinelAgent(llm, name=config, dry_run=dry_run, **flags[config])
