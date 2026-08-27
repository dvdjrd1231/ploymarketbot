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


def _scrub_message(m: dict) -> dict:
    """Scrub the text of one chat message, leaving its structure alone.

    Only `content` carries free text. `tool_calls` and `tool_call_id` are
    protocol fields, and redacting a call id would break the model's ability
    to match a result to the call that asked for it.
    """
    out = dict(m)
    if isinstance(out.get("content"), str):
        out["content"] = scrub(out["content"])
    return out


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
        self.charter_in_force = ""      # 'full' | 'condensed', set by ask()

    @property
    def configured(self) -> bool:
        return bool(self.cfg.llm_provider and self.cfg.llm_endpoint
                    and self.cfg.llm_model)

    def status(self) -> dict:
        from . import doctrine
        return {
            "configured": self.configured,
            "charter": doctrine.status(self.cfg.llm_context_limit),
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
        # The system message is the charter (docs/MASTER-SYSTEM-PROMPT.md),
        # scoped to this role. `doctrine.system_prompt` decides between the
        # verbatim text and the hand-written condensation by measuring both
        # against the configured context window — a charter that crowds out
        # the evidence would defeat the purpose of sending it.
        sys_msg = system
        if not sys_msg:
            from . import doctrine
            sys_msg, self.charter_in_force = doctrine.system_prompt(
                role, context_limit=self.cfg.llm_context_limit)
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

    # -- tool-calling transport ---------------------------------------------
    def chat(self, messages: list, *, tools: list | None = None,
             max_tokens: int = 4096, temperature: float | None = None) -> dict:
        """One multi-turn completion, with optional tool calling.

        Raw and unscrubbed of numerals on purpose. `ask()` above is the
        narrator: it may not emit a figure, because its output is read as
        commentary on measured evidence and a generated number there would be
        indistinguishable from a measurement. This is a different job. The
        agent loop calls this to plan and to write code, where a numeral is a
        line number, an array index or a threshold in a source file — and
        stripping those would produce broken software rather than safe
        software.

        The wire format is OpenAI-compatible function calling, which Ollama,
        LM Studio, llama.cpp, vLLM and the OpenAI API all speak, so the same
        code drives a 7B model on the user's own machine or a frontier model
        behind an endpoint. Prompts are still passed through `secrets.scrub`.

        Returns the raw `message` dict: `content`, and `tool_calls` when the
        model asked for one.
        """
        import time
        if not self.configured:
            return {"error": "no model configured", "content": ""}

        payload = {
            "model": self.cfg.llm_model,
            "messages": [_scrub_message(m) for m in messages],
            "temperature": (self.cfg.llm_temperature if temperature is None
                            else temperature),
            "max_tokens": max_tokens, "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        url = self.cfg.llm_endpoint.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/v1/chat/completions" if "/v1" not in url \
                else "/chat/completions"
        headers = {"Content-Type": "application/json",
                   "User-Agent": "polymarket-quant-bridge-v3/1.0"}
        # An API key is read from the environment and never from the config
        # tree, for the same reason no other credential lives there.
        import os
        key = os.environ.get("PQV3_LLM_API_KEY", "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"

        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers)
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(
                    req, timeout=self.cfg.llm_timeout_secs) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:600]
            except Exception:                                 # noqa: BLE001
                pass
            hint = ""
            if tools and ("tool" in body.lower() or e.code == 400):
                hint = (" — this endpoint may not support tool calling. A "
                        "model without it cannot drive the agent; try a "
                        "tool-capable model (most recent instruct models are)")
            return {"error": f"HTTP {e.code}: {body}{hint}", "content": ""}
        except Exception as e:                                # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}", "content": ""}

        msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
        msg["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        msg.setdefault("content", "")
        return msg

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
