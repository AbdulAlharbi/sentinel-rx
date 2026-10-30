"""Ollama LLM backend with a content-addressed record/replay cache.

Every call is keyed by ``sha256(model, system, prompt, options)``. In *record* mode a cache
miss calls Ollama and stores the response under ``cache/`` (committed to git); in *replay*
mode a miss is an error, so the whole benchmark reproduces byte-for-byte with no LLM
installed. Tokens and latency are logged per call.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CACHE_DIR = Path(__file__).resolve().parents[1] / "cache"
DEFAULT_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


class ReplayMissError(RuntimeError):
    """Raised in replay mode when a prompt is not in the cache."""


@dataclass
class LLMResponse:
    """One completion."""

    text: str
    tokens_in: int
    tokens_out: int
    seconds: float
    cached: bool
    key: str


@dataclass
class Usage:
    """Accumulated cost of one agent run."""

    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0.0
    cache_hits: int = 0
    keys: list[str] = field(default_factory=list)

    def add(self, r: LLMResponse) -> None:
        """Account one response."""
        self.calls += 1
        self.tokens_in += r.tokens_in
        self.tokens_out += r.tokens_out
        self.seconds += r.seconds
        self.cache_hits += int(r.cached)
        self.keys.append(r.key)


class LLMClient:
    """Thin, deterministic Ollama client (temperature 0, fixed seed) with caching."""

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        replay: bool = False,
        cache_dir: Path = CACHE_DIR,
        host: str = DEFAULT_HOST,
        num_predict: int = 500,
        num_ctx: int = 8192,
        timeout: float = 600.0,
    ) -> None:
        self.model = model
        self.replay = replay
        self.cache_dir = cache_dir / re.sub(r"[^A-Za-z0-9_.-]", "_", model)
        self.host = host.rstrip("/")
        self.options: dict[str, Any] = {
            "temperature": 0,
            "seed": 0,
            "num_predict": num_predict,
            "num_ctx": num_ctx,
        }
        self.timeout = timeout
        self.usage = Usage()

    def key_for(self, prompt: str, system: str) -> str:
        """Content hash of a request."""
        payload = json.dumps(
            {"model": self.model, "system": system, "prompt": prompt, "options": self.options},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def complete(self, prompt: str, system: str = "", json_mode: bool = True) -> LLMResponse:
        """Return the (cached or fresh) completion for a prompt."""
        key = self.key_for(prompt, system)
        path = self.cache_dir / f"{key}.json"
        if path.exists():
            rec = json.loads(path.read_text())
            r = LLMResponse(
                rec["response"], rec["tokens_in"], rec["tokens_out"], rec["seconds"], True, key
            )
            self.usage.add(r)
            return r
        if self.replay:
            raise ReplayMissError(f"replay mode: no cached response for {self.model} key={key}")
        rec = self._call_ollama(prompt, system, json_mode)
        rec.update(
            {"model": self.model, "system": system, "prompt": prompt, "created": time.time()}
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=1))
        os.replace(tmp, path)
        r = LLMResponse(
            rec["response"], rec["tokens_in"], rec["tokens_out"], rec["seconds"], False, key
        )
        self.usage.add(r)
        return r

    def _call_ollama(self, prompt: str, system: str, json_mode: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": self.options,
        }
        if json_mode:
            body["format"] = "json"
        req = urllib.request.Request(
            f"{self.host}/api/generate",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.load(resp)
        except urllib.error.URLError as e:  # pragma: no cover - needs a down server
            raise RuntimeError(
                f"Ollama not reachable at {self.host}: {e}. Use --replay or start `ollama serve`."
            ) from e
        return {
            "response": data.get("response", ""),
            "tokens_in": int(data.get("prompt_eval_count", 0)),
            "tokens_out": int(data.get("eval_count", 0)),
            "seconds": round(time.perf_counter() - t0, 3),
        }


def parse_json(text: str) -> Any:
    """Parse JSON leniently (strip code fences, trailing junk)."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            return json.loads(t[start : end + 1])
        raise
