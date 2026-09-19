"""LLM providers for the meta-improver.  stdlib only (urllib), like the rest of
the project's live tooling, so it runs on a bare CRC login node.

  OpenAIChat : real calls; reads OPENAI_API_KEY from the environment.  On CRC the
               key lives in a run.env file - source it, do not paste it anywhere.
  MockMetaLLM: deterministic offline stand-in used by tests and the replay demo.
               It is NOT evidence about the method: it applies a fixed
               verifier -> patch table.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Protocol

from .harness import RULE_BOUNDS
from .memory import LESSONS


class LLM(Protocol):
    provider: str
    model: str

    def complete(self, system: str, user: str) -> str: ...


class LLMError(RuntimeError):
    pass


class OpenAIChat:
    provider = "openai"

    def __init__(self, model: str = "gpt-4o-2024-08-06", api_key_env: str = "OPENAI_API_KEY",
                 base_url: str = "https://api.openai.com/v1", temperature: float = 0.0,
                 retries: int = 3, timeout: int = 180):
        self.model, self.api_key_env, self.base_url = model, api_key_env, base_url.rstrip("/")
        self.temperature, self.retries, self.timeout = temperature, retries, timeout

    def complete(self, system: str, user: str) -> str:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise LLMError(f"{self.api_key_env} is not set (on CRC: source the run.env that holds the key)")
        body = json.dumps({
            "model": self.model, "temperature": self.temperature,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }).encode()
        last = None
        for attempt in range(self.retries):
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=body, method="POST",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode())
                return data["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code}: {exc.read().decode(errors='ignore')[:200]}"
                if exc.code not in (429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(2 ** attempt)
        raise LLMError(f"meta-improver call failed: {last}")


class MockMetaLLM:
    """Deterministic: patch for the worst not-yet-addressed verifier."""
    provider = "mock"
    model = "mock-v1"

    def complete(self, system: str, user: str) -> str:
        data = json.loads(user)
        mode = data["harness"]["mode"]
        harness = data["harness"]
        vector = data["aggregate_reward_vector"]
        present = " ".join([harness.get("prompt_notes", ""), harness.get("decision_policy_text", "")])
        for name, reward in sorted(vector.items(), key=lambda kv: (kv[1], kv[0])):
            if reward >= 0:
                break
            evidence = [e for r in data["runs"] for v in r["verifier_results"]
                        if v["verifier"] == name for e in v["evidence"][:1]] or [f"{name} reward {reward}"]
            lesson = LESSONS.get(name, "")
            if mode == "rule" and name == "validation_argmax_anchoring":
                cur = (harness.get("rule_config") or {}).get("max_stagnation", 15)
                new = max(RULE_BOUNDS["max_stagnation"][0] + 3, int(cur) // 2)
                if new != cur:
                    return json.dumps({
                        "target_component": "decision_policy",
                        "proposed_patch": {"op": "set", "values": {"max_stagnation": new}},
                        "expected_effect": "draft a fresh node sooner when the incumbent stagnates, so a "
                                           "phantom best node cannot monopolise the budget",
                        "evidence": evidence[:3]})
            if lesson and lesson not in present:
                target = "decision_policy" if (mode == "agent" and name == "validation_argmax_anchoring") else "prompt"
                return json.dumps({
                    "target_component": target,
                    "proposed_patch": {"op": "append", "text": lesson},
                    "expected_effect": f"raise {name} reward by making the agent apply this check",
                    "evidence": evidence[:3]})
        return json.dumps({"no_change": True, "reason": "no negative verifier reward left to address"})
