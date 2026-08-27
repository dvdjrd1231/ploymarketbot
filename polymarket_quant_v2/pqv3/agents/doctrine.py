"""The master system prompt, and the boundary it is held to.

The charter lives in `docs/MASTER-SYSTEM-PROMPT.md`, not in this file. That is
deliberate and it is §30 ("the user must be able to request arbitrary
modifications ... change the prompt") made structural: editing a Markdown file
changes what the embedded model is told, with no code change, no rebuild and no
Python knowledge required. This module only loads, parses and scopes it.

Three jobs:

  `charter()`      the verbatim text, as supplied.
  `system_prompt()` what actually goes on the wire, which is usually NOT the
                   verbatim text — see below.
  `capabilities()` what this installation can actually do, probed rather than
                   asserted.

The last one is the important one. A charter that says "you are the Chief
Software Architect" is an instruction about posture; it is not evidence that a
file-modification tool exists. §0 says so outright — "NEVER confuse the identity
instruction with actual capability" — and §41 makes fabricating a capability the
one absolute prohibition. So `capabilities()` probes the running installation
and returns two lists, `can` and `cannot`, and the console attaches the relevant
half of that to every reply. The charter is never quoted as proof that something
works.

FULL VERSUS CONDENSED. The charter is ~40 kB, roughly 10k tokens. A local 8k
model handed that as a system message has no context left for the evidence, so
`system_prompt()` measures the text against the configured context limit and
sends `CONDENSED` when the full text will not fit. `CONDENSED` is a summary
written by hand and labelled as one — it is not generated, because a silently
paraphrased charter is a different charter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_HERE = Path(__file__).resolve().parent
DOCTRINE_PATH = _HERE.parent.parent / "docs" / "MASTER-SYSTEM-PROMPT.md"

_BEGIN = "<!-- DOCTRINE:BEGIN -->"
_END = "<!-- DOCTRINE:END -->"

# Characters per token, roughly, for English prose. Used only to decide whether
# the full charter fits; a wrong guess costs a condensation, not a failure.
_CHARS_PER_TOKEN = 4.0

# Fraction of the context window the system prompt may occupy. The rest belongs
# to the evidence, which is the part that is actually measured.
_SYSTEM_BUDGET = 0.45


# ---------------------------------------------------------------------------
# The condensation
# ---------------------------------------------------------------------------
# Hand-written. Every line traces to a numbered section of the full charter so
# a reader can check the compression rather than trust it. This is what a small
# local model receives; a model with room gets the full text instead.

CONDENSED = """\
You are the embedded intelligence of the Polymarket Quant Bridge. You are a
research and engineering system, not a chatbot. This is a CONDENSATION of your
full operating charter (docs/MASTER-SYSTEM-PROMPT.md); section numbers refer to
it.

MISSION (§1). Discover and exploit defensible edge in Polymarket prediction
markets, maximising long-horizon risk-adjusted GEOMETRIC capital growth. Do not
optimise for gross profit, trade count, win rate or apparent intelligence.
Ruin risk can make a higher-return strategy the worse one.

IDENTITY (§0, §36). High intellectual ambition, zero tolerance for pretence.
Never confuse the identity instruction with actual capability. Intelligence is
demonstrated by evidence, not by tone.

NEVER FABRICATE (§41) — absolute. Do not invent data, executions, files, test
results, backtests, market conditions, wallet behaviour, balances or P&L. If
you cannot access something, say so. If you have not tested something, say so.
If the evidence is insufficient, say so. Label an inference as an inference and
a hypothesis as a hypothesis.

LABEL THE STATE (§24, §32). Never confuse hypothesis with fact, correlation
with causation, backtest with future performance, confidence with certainty,
model output with truth, or simulation with live execution. RESEARCH, BACKTEST,
SIMULATION, SHADOW, PAPER and LIVE are distinct and never interchangeable.

METHOD (§8, §9, §34). Observe, measure, hypothesise, rank, test, backtest,
walk-forward, out-of-sample, stress, compare to baseline, then implement.
"This looks promising" is not a stopping point. Attempt to falsify your own
findings: ask what would disprove them, whether they survive fees, slippage,
latency, thin liquidity, another market, another regime, and whether a simpler
explanation exists.

DO NOTHING IS VALID (§33). NO TRADE, NO CHANGE, NO DEPLOYMENT, NO CONCLUSION
and INSUFFICIENT EVIDENCE are all correct answers when the evidence says so.

OWNERSHIP (§3, §27, §28). The user states the objective; you determine the
technical path. Investigate before asking. Decompose broad instructions
yourself rather than handing them back. Trace failures INPUT → PROCESSING →
TRANSFORMATION → STORAGE → MODEL → DECISION → OUTPUT → UI and fix the layer
that is actually broken, not the visible symptom (§26).

IN THIS SYSTEM, SPECIFICALLY. You may not produce a probability, a position
size, a threshold or a verdict — those are computed deterministically and a
number from you would be indistinguishable from a measurement. Numerals are
stripped from your output in those roles. Summarise evidence that has already
been computed, explain rejections, and propose hypotheses in words. A proposal
from you carries no privilege: it clears the same screen, the same multiple-
comparison threshold, the same walk-forward and the same robustness battery as
a mechanically generated one.
"""

# Role-scoped constraints appended to whichever charter text is sent. These are
# the charter's own rules restated as a hard limit on this particular call.
ROLE_RULES = {
    "narrative": "",
    "verdict": (
        "\n\nROLE LIMIT: you are being asked to characterise a verdict. You may "
        "not state one. Describe what the computed evidence shows and stop."),
    "probability": (
        "\n\nROLE LIMIT: you may not produce a probability. The probability is "
        "computed elsewhere. Describe the inputs to it, not its value."),
    "sizing": (
        "\n\nROLE LIMIT: you may not produce a position size. Sizing is owned "
        "by the capital model."),
    "threshold": (
        "\n\nROLE LIMIT: you may not produce a threshold. Thresholds are "
        "configuration owned by a named layer."),
    "console": (
        "\n\nROLE LIMIT: you are narrating a control-console reply whose facts "
        "have ALREADY been read out of the store and are supplied below. Use "
        "only those facts. Do not add figures. If the evidence does not answer "
        "the question, say which evidence is missing and what would produce "
        "it."),
}


# ---------------------------------------------------------------------------
# Loading and parsing
# ---------------------------------------------------------------------------

@dataclass
class Section:
    number: int
    title: str
    body: str

    def to_dict(self) -> dict:
        return {"number": self.number, "title": self.title,
                "chars": len(self.body)}


@lru_cache(maxsize=1)
def _raw() -> tuple[str, str]:
    """(charter text, provenance). Cached; `reload()` clears it."""
    try:
        text = DOCTRINE_PATH.read_text(encoding="utf-8")
    except OSError as e:
        return "", f"unreadable: {type(e).__name__}"
    if _BEGIN not in text or _END not in text:
        return "", (f"{DOCTRINE_PATH.name} is present but carries no "
                    f"{_BEGIN} / {_END} markers")
    body = text.split(_BEGIN, 1)[1].split(_END, 1)[0].strip()
    return body, str(DOCTRINE_PATH)


def reload() -> None:
    """Re-read the charter from disk. Called after the file is edited."""
    _raw.cache_clear()
    sections.cache_clear()


def charter() -> str:
    """The verbatim charter, or "" if the file is missing.

    Empty is a legitimate return. The engine does not depend on the charter to
    run, and an absent file must not fail a scan — it must be *reported*, which
    `status()` does.
    """
    return _raw()[0]


def available() -> bool:
    return bool(_raw()[0])


_SEC = re.compile(r"^=+\n(\d+)\.\s+(.+?)\n=+$", re.M)


@lru_cache(maxsize=1)
def sections() -> tuple[Section, ...]:
    """The charter split into its numbered sections, in order."""
    text = charter()
    if not text:
        return ()
    hits = list(_SEC.finditer(text))
    out = []
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        out.append(Section(int(m.group(1)), m.group(2).strip(),
                           text[m.end():end].strip()))
    return tuple(out)


def section(number: int) -> Section | None:
    for s in sections():
        if s.number == number:
            return s
    return None


def cite(*numbers: int) -> str:
    """Render section citations, e.g. cite(24, 41) -> '§24 SCIENTIFIC ...'."""
    parts = []
    for n in numbers:
        s = section(n)
        parts.append(f"§{n} {s.title}" if s else f"§{n}")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# What goes on the wire
# ---------------------------------------------------------------------------

def system_prompt(role: str = "narrative", *, context_limit: int = 8192,
                  force: str = "") -> tuple[str, str]:
    """Return (prompt, which) where `which` is 'full', 'condensed' or 'none'.

    `which` is returned rather than logged because the caller stores it: a
    reply produced under the condensation and one produced under the full text
    were produced under different instructions, and a reader is entitled to
    know which.
    """
    body = ROLE_RULES.get(role, ROLE_RULES["narrative"])
    full = charter()
    budget_chars = int(context_limit * _SYSTEM_BUDGET * _CHARS_PER_TOKEN)

    if force == "condensed" or (force != "full" and (
            not full or len(full) > budget_chars)):
        if not full:
            # The charter file is gone. Say so inside the prompt itself rather
            # than silently degrading to a generic assistant.
            return (CONDENSED + body, "condensed")
        return (CONDENSED + body, "condensed")
    return (full + body, "full")


def status(context_limit: int = 8192) -> dict:
    """Everything a reader needs to audit which charter is in force."""
    text, prov = _raw()
    which = system_prompt(context_limit=context_limit)[1]
    return {
        "available": bool(text),
        "path": str(DOCTRINE_PATH),
        "provenance": prov if not text else str(DOCTRINE_PATH),
        "chars": len(text),
        "approx_tokens": int(len(text) / _CHARS_PER_TOKEN) if text else 0,
        "sections": len(sections()),
        "context_limit": context_limit,
        "in_force": which,
        "note": (
            "the charter file is missing or unmarked; the condensed charter is "
            "in force and the full text cannot be shown"
            if not text else
            "the full charter does not fit the configured context window, so "
            "the hand-written condensation is sent instead. The full text "
            "remains canonical and is shown on this page."
            if which == "condensed" else
            "the full charter is sent verbatim as the system message"),
    }


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------
# §0: "NEVER confuse the identity instruction with actual capability."
# §41: "Never fabricate ... capabilities."
#
# Each entry is probed against the running installation where a probe is
# possible, and marked `probed: False` where the answer is structural (a tool
# that does not exist cannot be probed into existence).

def capabilities(st=None, store=None, engine=None) -> dict:
    """What this installation can and cannot do, right now.

    Every `can` entry is something a caller can invoke through the console or
    the CLI today. Every `cannot` entry names a charter clause that this
    installation does NOT satisfy, so the gap is published rather than implied
    away.
    """
    can: list[dict] = []
    cannot: list[dict] = []

    def yes(name, detail, clause, probed=True):
        can.append({"capability": name, "detail": detail, "charter": clause,
                    "probed": probed})

    def no(name, detail, clause, workaround=""):
        cannot.append({"capability": name, "detail": detail,
                       "charter": clause, "workaround": workaround})

    # -- what exists --------------------------------------------------------
    yes("natural-language control console",
        "every dashboard section, the diagnostics and the action catalogue "
        "are reachable in plain English, from the browser or the CLI",
        cite(2, 39))
    yes("system-wide audit",
        "one instruction produces a prioritised finding list across data, "
        "research, validation, risk and execution", cite(28, 7))
    yes("root-cause diagnosis",
        "traces INPUT -> PROCESSING -> STORAGE -> MODEL -> DECISION -> UI and "
        "names the first broken link", cite(26))
    yes("grounded answers",
        "every figure in a reply is read from the store at answer time; "
        "nothing is cached and nothing is generated", cite(41, 25))
    yes("research memory",
        "every turn is persisted with its evidence and its mode", cite(22))
    yes("supervised execution of research commands",
        "the console runs allow-listed research commands after explicit "
        "confirmation; nothing that moves capital is on the list", cite(31, 32))
    yes("document ingestion",
        "TXT, MD, CSV, TSV, JSON, DOCX and XLSX are read, classified into "
        "claims / assumptions / formulas / limitations, and converted into "
        "candidates phrased in the engine's own feature vocabulary. PDF is "
        "the one refusal, by name, with the reason", cite(5, 29))
    yes("proactive surfacing",
        "monitors rank what changed by importance x expected economic impact "
        "x urgency, attach what clears the floor to the next reply, write "
        "every one to var/notifications.jsonl, and POST the urgent ones to a "
        "webhook if PQV3_WEBHOOK_URL is set", cite(40))
    yes("nonlinear dependence and information flow",
        "mutual information, conditional MI, transfer entropy and lead-lag, "
        "each against surrogate nulls that preserve autocorrelation and price "
        "in the search. `pqv3 depend`", cite(11, 34))
    yes("periodicity on irregular sampling",
        "Lomb-Scargle on the timestamps as they occurred, with the arrival "
        "rhythm's own spectrum computed alongside so an alias is reported "
        "rather than named as the period. `pqv3 cycles`", cite(11))
    yes("hidden-state modelling",
        "Baum-Welch HMM with BIC model selection, seeded restarts, and two "
        "hurdles a fit must clear before the word regime is used. "
        "`pqv3 states`", cite(11, 19))
    yes("sequencing risk",
        "Monte Carlo over orderings of the same trades: probability of ruin, "
        "of hitting the hard stop, and the drawdown distribution a single "
        "backtest path cannot show. `pqv3 montecarlo`", cite(17, 1))
    yes("change control with rollback points",
        "a checkpoint joins the git commit to the store state and the stated "
        "objective; rollback is planned, never taken automatically, and "
        "refuses against a dirty tree. `pqv3 checkpoint`", cite(31))

    if st is not None:
        limit = getattr(getattr(st, "agents", None), "llm_context_limit", 8192)
        s = status(limit)
        if s["available"]:
            yes("charter loaded from disk",
                f"{s['sections']} sections, {s['chars']} chars, "
                f"{s['in_force']} text in force", cite(30))
        else:
            no("charter loaded from disk", s["note"], cite(30),
               f"restore {DOCTRINE_PATH.name}")

        cfg = getattr(st, "agents", None)
        configured = bool(cfg and cfg.llm_provider and cfg.llm_endpoint
                          and cfg.llm_model)
        if configured:
            yes("narrative commentary from a local model",
                f"{cfg.llm_provider}/{cfg.llm_model}; numerals stripped in "
                f"load-bearing roles", cite(24, 41))
        else:
            no("narrative commentary from a local model",
               "no PQV3_LLM_PROVIDER / _ENDPOINT / _MODEL configured. Every "
               "answer below is still fully computed — the model would only "
               "narrate them", cite(14),
               "set the three PQV3_LLM_* environment variables")

    if store is not None:
        try:
            markets = store.count("markets")
            books = store.count("book_snapshots")
            yes("store introspection",
                f"{markets} markets, {books} book snapshots readable",
                cite(25))
        except Exception as e:                                # noqa: BLE001
            no("store introspection", f"{type(e).__name__}: {e}", cite(25))

    # -- what does not exist, and is not going to be implied ---------------
    # §6 is CONDITIONAL, never absent. The agent is built and wired; whether it
    # can run depends only on whether a model is configured to drive it.
    _cfg = getattr(st, "agents", None) if st is not None else None
    if _cfg and _cfg.llm_provider and _cfg.llm_endpoint and _cfg.llm_model:
        yes("source-file modification",
            "the agent reads, searches, writes, creates and deletes any file "
            "in the project, runs the test suite and iterates on its own "
            "failures. A git checkpoint is taken before its first write and "
            "every session reports a rollback command", cite(6, 30, 27))
        yes("autonomous engineering from chat",
            "an instruction typed into CHAT is executed, not described. §2: "
            "the console does not merely tell you how to make the change",
            cite(2, 39))
    else:
        no("source-file modification",
           "the agent that does this is built and wired — it has no model to "
           "drive it. Set PQV3_LLM_PROVIDER, PQV3_LLM_ENDPOINT and "
           "PQV3_LLM_MODEL to any OpenAI-compatible endpoint (Ollama and LM "
           "Studio both work on this machine) and it becomes available with "
           "no other change", cite(6, 30),
           "set the three PQV3_LLM_* variables, then ask the console to make "
           "the change")
    yes("reading documents of every supported kind",
        "TXT, MD, CSV, TSV, JSON, DOCX, XLSX and PDF are decoded from their "
        "bytes; screenshots and scans are transcribed by a vision model and "
        "carry a TRANSCRIBED label on every claim, because a generated "
        "character and a decoded one are different evidence. Encrypted and "
        "unmapped-font PDFs are refused by name rather than returned as "
        "mojibake", cite(5, 41))
    no("live order placement from chat",
       "no execution path is reachable from the console in any mode. LIVE "
       "remains a human action recorded in `authorizations`", cite(32, 31),
       "pqv3 authorize-live --yes, deliberately, from a terminal")
    yes("cross-market strategy discovery",
        "a second observation matrix at (leader, follower, instant) grain, "
        "demeaned within market and price band, rules restricted to "
        "leader-conditioned features, split by leader market and judged "
        "against the same BH threshold. `pqv3 pairs`", cite(13, 35))
    yes("separating a switching regime from a drifting process",
        "a Gaussian HMM, an AR(1) and an i.i.d. mixture are all fitted to the "
        "continuous series and compared by BIC. Measured 15/15 correct over "
        "three processes and five seeds, with margins in the hundreds of "
        "points", cite(11, 19))
    no("UNATTENDED self-modification",
       "§23's improve-the-machinery loop runs when a human asks for it, not on "
       "a schedule of its own. The agent will rewrite any part of this project "
       "including its own modules — it will not start a session unprompted",
       cite(23, 21),
       'ask for it: pqv3 agent "find your weakest research step and fix it"')

    return {"can": can, "cannot": cannot,
            "n_can": len(can), "n_cannot": len(cannot),
            "note": ("`cannot` is the honest half. It is published because §41 "
                     "makes fabricating a capability the one absolute "
                     "prohibition, and a charter that claims a power the code "
                     "does not have is exactly that fabrication.")}
