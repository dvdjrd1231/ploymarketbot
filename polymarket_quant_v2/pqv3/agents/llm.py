"""Optional local LLM. Narrative only — it may never produce a number.

The brief is explicit on both halves of this, and they are load-bearing:

    "Do not allow an LLM to perform arithmetic that Rust can perform
     deterministically."
    "Do not allow an LLM to invent market data."

So this module is deliberately crippled in one direction. It can summarise
evidence, propose a hypothesis in words, and explain a rejection. It cannot
return a probability, a size, a threshold or a verdict — `ask()` scrubs
numerals out of the roles where a number would be load-bearing, and every
caller receives text that is stored as commentary rather than consumed as a
value.

**Nothing here is required.** With no provider configured, every function
returns a `LLMResult` with `available=False` and the quantitative engine runs
exactly as it did before. That is the correct default: an intelligence layer
that stops the engine when it is absent is not a layer, it is a dependency.

**No secret ever reaches a prompt.** Every prompt is passed through
`secrets.scrub` on the way out. The signing boundary has nothing to hand over
anyway, so this is belt and braces, but a prompt is exactly the kind of string
that ends up in someone's logs.

Transport is stdlib `urllib` against an OpenAI-compatible `/chat/completions`
endpoint, which is what Ollama, llama.cpp, LM Studio and vLLM all expose.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from ..config import AgentConfig, Settings
from ..secrets import scrub

# Roles where a number would be acted on. Numerals are stripped from LLM output
# in these roles so a hallucinated figure cannot be mistaken for a measurement.
NUMERIC_FORBIDDEN_ROLES = ("verdict", "probability", "sizing", "threshold")

_NUM = re.compile(r"\b\d+(?:\.\d+)?%?\b")


@dataclass
class LLMResult:
    available: bool = False
    text: str = ""
    role: str = "narrative"
    model: str = ""
    elapsed_ms: int = 0
    error: str = ""
    numbers_stripped: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class LocalLLM:
    """A thin, optional client. Never on the critical path."""

    def __init__(self, st: Settings) -> None:
        self.cfg: AgentConfig = st.agents
        self.st = st

    @property
    def configured(self) -> bool:
        return bool(self.cfg.llm_provider and self.cfg.llm_endpoint
                    and self.cfg.llm_model)

    def status(self) -> dict:
        return {
            "configured": self.configured,
            "provider": self.cfg.llm_provider or None,
            "endpoint": self.cfg.llm_endpoint or None,
            "model": self.cfg.llm_model or None,
            "role": "narrative only",
            "note": ("no local model configured; the quantitative engine runs "
                     "unchanged. Set PQV3_LLM_PROVIDER, PQV3_LLM_ENDPOINT and "
                     "PQV3_LLM_MODEL to enable commentary.")
            if not self.configured else
            ("commentary only. This model cannot produce a probability, a "
             "size, a threshold or a verdict; numerals are stripped from those "
             "roles before the text is stored."),
        }

    # -- core ---------------------------------------------------------------
    def ask(self, prompt: str, *, role: str = "narrative",
            system: str = "") -> LLMResult:
        r = LLMResult(role=role, model=self.cfg.llm_model)
        if not self.configured:
            r.note = "no local model configured"
            return r

        import time
        sys_msg = system or (
            "You are a research assistant inside a quantitative trading system. "
            "You explain and summarise evidence that has ALREADY been computed. "
            "You never estimate probabilities, sizes or thresholds, and you "
            "never invent market data. If the evidence given to you does not "
            "support a statement, say so plainly.")
        body = json.dumps({
            "model": self.cfg.llm_model,
            "messages": [{"role": "system", "content": sys_msg},
                         {"role": "user", "content": scrub(prompt)}],
            "temperature": self.cfg.llm_temperature,
            "max_tokens": 600, "stream": False,
        }).encode()

        t0 = time.perf_counter()
        url = self.cfg.llm_endpoint.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/v1/chat/completions" if "/v1" not in url \
                else "/chat/completions"
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "polymarket-quant-bridge-v3/1.0"})
        try:
            with urllib.request.urlopen(
                    req, timeout=self.cfg.llm_timeout_secs) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            text = (data.get("choices") or [{}])[0].get(
                "message", {}).get("content", "") or ""
        except Exception as e:                                # noqa: BLE001
            r.error = f"{type(e).__name__}: {e}"
            r.note = ("the local model was unreachable. This is not an error "
                      "condition for the engine — commentary is optional.")
            return r

        r.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        r.available = True
        if role in NUMERIC_FORBIDDEN_ROLES:
            stripped, n = _NUM.subn("[number withheld]", text)
            r.numbers_stripped = n
            r.text = scrub(stripped)
            if n:
                r.note = (f"{n} numeral(s) removed: an LLM may not supply a "
                          f"figure in the '{role}' role, because a figure here "
                          f"would be acted on.")
        else:
            r.text = scrub(text)
        return r

    # -- the three things it is actually for --------------------------------
    def explain_decision(self, decision) -> LLMResult:
        """Turn a decision record into prose. Reads only computed values."""
        d = decision.to_dict() if hasattr(decision, "to_dict") else decision
        gates = (d.get("gates") or {}).get("results", [])
        blocked = [f"{g['gate']}: {g['reason']}" for g in gates
                   if not g.get("passed")]
        prompt = (
            "Explain this trading decision to an experienced but non-technical "
            "reader in under 150 words. Use ONLY the facts below; do not add "
            "figures of your own.\n\n"
            f"Action: {d.get('action')}\n"
            f"Market: {d.get('market_id')}\n"
            f"Market price: {d.get('market_probability')}\n"
            f"Our estimate: {d.get('fair_probability')}\n"
            f"Confidence: {d.get('confidence')}\n"
            f"Blocking gate: {d.get('blocking_gate') or 'none'}\n"
            f"Reasons against:\n- " + "\n- ".join(blocked[:8]))
        return self.ask(prompt, role="narrative")

    def summarise_news(self, items: list) -> LLMResult:
        """Summarise captured headlines. Explicitly NOT a direction call."""
        if not items:
            return LLMResult(note="no items")
        lines = [f"- [{i.get('source_class')}/{i.get('confirmation')}] "
                 f"{i.get('title')}" for i in items[:15]]
        prompt = (
            "Summarise what these headlines collectively report, in under 120 "
            "words. Note explicitly where they disagree or where a claim rests "
            "on a single unconfirmed source. Do NOT say which way any market "
            "should move — that is decided elsewhere from measured data.\n\n"
            + "\n".join(lines))
        return self.ask(prompt, role="narrative")

    def propose_hypotheses(self, context: dict) -> LLMResult:
        """Suggest hypotheses in words. They enter the ordinary pass.

        An LLM proposal carries no privilege whatsoever: it becomes a candidate
        that must clear the same in-sample screen, the same BH threshold over
        the same denominator, the same walk-forward and the same robustness
        battery as a mechanically generated one.
        """
        prompt = (
            "Given the measured context below, propose up to five testable "
            "hypotheses about when trades in this market are more likely to be "
            "profitable. Each must be phrased as a condition on an observable "
            "quantity, and must be falsifiable. Do not assert that any of them "
            "is true.\n\n" + json.dumps(scrub(context), default=str)[:3000])
        r = self.ask(prompt, role="narrative")
        if r.available:
            r.note = ("proposals carry no privilege: each must clear the same "
                      "screen, BH threshold, walk-forward and robustness "
                      "battery as a mechanically generated hypothesis.")
        return r
