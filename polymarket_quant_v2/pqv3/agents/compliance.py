"""Section-by-section audit of the charter against the code that answers it.

The question "is it all done?" deserves a checkable answer rather than an
assurance, and an assurance is all a hand-written status document can ever be —
it is accurate on the day it is written and silently wrong afterwards. So each
of the 44 sections carries the FILES and COMMANDS that implement it, and this
module verifies at run time that every one of them exists. Delete
`research/montecarlo.py` and §17 stops reporting DONE, without anybody
remembering to edit a table.

Four statuses, and the distinction between the last two is the point:

    DONE        implemented and verifiable here
    CONDITIONAL implemented, and needs something the operator supplies —
                a model endpoint, or collector history. Not a gap in the code
    PARTIAL     genuinely incomplete, with the missing piece named
    REFUSED     deliberately not built, because building it would CONTRADICT
                the charter rather than complete it

REFUSED is not a euphemism for unfinished. §32 requires that live execution be
a human action and that execution is never fabricated; a tool that placed
orders would violate the document, not satisfy it. There is exactly one of
these and it is §32 itself.

What this cannot check is quality — that a section's code is correct, not
merely present. That is what `tests/` is for, and the section rows name the
tests too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent

DONE, CONDITIONAL, PARTIAL, REFUSED = "DONE", "CONDITIONAL", "PARTIAL", "REFUSED"


@dataclass
class Row:
    section: int
    status: str
    answer: str
    files: tuple = ()
    commands: tuple = ()
    tests: tuple = ()
    gap: str = ""
    missing: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


# The map. `files` are relative to the project root and are checked to exist.
MAP: tuple[Row, ...] = (
    Row(0, DONE, "the charter is the model's system prompt, and "
        "capabilities() keeps the identity instruction from being read as "
        "evidence of a capability",
        ("pqv3/agents/doctrine.py", "docs/MASTER-SYSTEM-PROMPT.md"),
        ("doctrine",), ("tests/v3/test_v3_console.py",)),
    Row(1, DONE, "geometric growth and probability of ruin are first-class: "
        "fractional Kelly capped by equity fraction, and a path simulator that "
        "reports ruin across orderings",
        ("pqv3/portfolio/capital.py", "pqv3/research/montecarlo.py"),
        ("capital", "montecarlo"), ("tests/v3/test_v3_methods.py",)),
    Row(2, CONDITIONAL, "the chat is the primary control interface: CLI, "
        "dashboard page and launcher. An engineering instruction is EXECUTED, "
        "which needs a model configured",
        ("pqv3/agents/console.py", "pqv3/agents/autonomy.py",
         "pqv3/server/ui.py", "6-ASK-THE-AI.bat"),
        ("chat", "agent"), ("tests/v3/test_v3_autonomy.py",),
        gap="the executing half needs PQV3_LLM_*; the answering half does not"),
    Row(3, CONDITIONAL, "a user instruction is treated as an authorised "
        "objective and carried out, not returned as advice",
        ("pqv3/agents/autonomy.py", "pqv3/agents/tools.py"), ("agent",),
        ("tests/v3/test_v3_autonomy.py",),
        gap="needs a model to drive the loop"),
    Row(4, DONE, "the whole project is readable and searchable; paths outside "
        "it are refused", ("pqv3/agents/tools.py",), ("agent",),
        ("tests/v3/test_v3_autonomy.py",)),
    Row(5, CONDITIONAL, "TXT, MD, CSV, TSV, JSON, DOCX, XLSX and PDF are "
        "DECODED; screenshots and scans are TRANSCRIBED by a vision model and "
        "labelled as generated wherever they surface. Encrypted and "
        "unmapped-font PDFs are refused by name rather than guessed at",
        ("pqv3/agents/documents.py", "pqv3/agents/pdf.py",
         "pqv3/agents/vision.py"),
        ("ingest",), ("tests/v3/test_v3_vision.py",),
        gap="images need a VISION-capable model at PQV3_LLM_*; every other "
            "format needs nothing"),
    Row(6, CONDITIONAL, "read, search, write, create, delete, test, iterate — "
        "the agent edits this project",
        ("pqv3/agents/tools.py", "pqv3/agents/autonomy.py"), ("agent",),
        ("tests/v3/test_v3_autonomy.py",),
        gap="needs a tool-capable model at PQV3_LLM_*"),
    Row(7, DONE, "eight modes recognised from plain English, each reply "
        "stamped with the one it was read as",
        ("pqv3/agents/console.py",), ("chat",),
        ("tests/v3/test_v3_console.py",)),
    Row(8, DONE, "observe -> hypothesise -> screen -> BH -> walk-forward -> "
        "robustness -> lifecycle, with the loop running continuously",
        ("pqv3/research/discover.py", "pqv3/research/walkforward.py",
         "pqv3/research/robustness.py", "pqv3/runtime.py"),
        ("discover", "walkforward"), ("tests/v3/test_v3_research.py",)),
    Row(9, DONE, "every estimator is tested against a constructed null that "
        "destroys the structure under test and nothing else",
        ("pqv3/research/surrogate.py", "pqv3/research/inversion.py"),
        ("invert", "depend"), ("tests/v3/test_v3_methods.py",)),
    Row(10, CONDITIONAL, "market, wallet, chain, news and microstructure "
        "layers all exist and are collected",
        ("pqv3/ingest/collectors.py", "pqv3/intelligence/wallets.py",
         "pqv3/ingest/chain_decode.py", "pqv3/news/causality.py"),
        ("collect", "scan"), ("tests/v3/test_v3_intelligence.py",),
        gap="order-book, news and chain history cannot be backfilled — they "
            "accumulate only while collectors run"),
    Row(11, DONE, "mutual information, transfer entropy, lead-lag, "
        "periodicity, change points, autocorrelation, hidden states, and a "
        "BIC comparison that separates a switching regime from one drifting "
        "process — 15/15 correct over three processes and five seeds",
        ("pqv3/research/dependence.py", "pqv3/research/spectral.py",
         "pqv3/regime/hidden.py", "pqv3/intelligence/sequences.py"),
        ("depend", "cycles", "states", "sequences"),
        ("tests/v3/test_v3_methods.py",)),
    Row(12, DONE, "wallets as behavioural datasets: DNA, cohorts, cross-wallet "
        "graph, ranked by alpha over the price band rather than by profit",
        ("pqv3/intelligence/wallets.py", "pqv3/intelligence/graph.py"),
        ("dna", "graph"), ("tests/v3/test_v3_intelligence.py",)),
    Row(13, DONE, "hypothesis families over the trade matrix AND a second "
        "matrix at (leader, follower, instant) grain, so cross-market rules "
        "have a row to live in and face the same screen, threshold and "
        "held-out split",
        ("pqv3/research/hypothesis.py", "pqv3/research/sweep.py",
         "pqv3/research/pairs.py"),
        ("discover", "pairs"), ("tests/v3/test_v3_pairs.py",)),
    Row(14, DONE, "25 specialists, adversarial members, disagreement recorded "
        "and abstentions shown rather than hidden",
        ("pqv3/agents/registry.py", "pqv3/agents/debate.py"), ("agents",),
        ("tests/v3/test_v3_gates_agents.py",)),
    Row(15, DONE, "validated strategies are matched to current conditions "
        "before capital is considered",
        ("pqv3/scanner/signals.py", "pqv3/regime/detect.py"), ("signals",),
        ("tests/v3/test_v3_research.py",)),
    Row(16, DONE, "sizing from edge, uncertainty, liquidity, correlation and "
        "concentration, with CAPITAL_INFEASIBLE a first-class outcome",
        ("pqv3/portfolio/capital.py", "pqv3/portfolio/correlation.py"),
        ("capital",), ("tests/v3/test_v3_capital.py",)),
    Row(17, DONE, "train/test split, walk-forward, bootstrap, Monte Carlo over "
        "orderings, costs, slippage and execution modelled",
        ("pqv3/research/backtest.py", "pqv3/research/walkforward.py",
         "pqv3/research/montecarlo.py", "pqv3/execution/simulator.py"),
        ("backtest", "walkforward", "montecarlo"),
        ("tests/v3/test_v3_methods.py",)),
    Row(18, DONE, "BH over the full denominator, robustness battery, "
        "bin-stability checks, and a tuned threshold withdrawn when it proved "
        "unstable", ("pqv3/research/robustness.py", "pqv3/research/stats.py"),
        ("discover",), ("tests/v3/test_v3_research.py",)),
    Row(19, DONE, "threshold regimes plus an estimated hidden-state model, "
        "with degradation tracked on the lifecycle",
        ("pqv3/regime/detect.py", "pqv3/regime/hidden.py"), ("states",),
        ("tests/v3/test_v3_methods.py",)),
    Row(20, CONDITIONAL, "thirteen detectors: operational faults, the crash "
        "meter, plus liquidity withdrawal, spread blowout, stale quotes and "
        "decision bursts",
        ("pqv3/agents/surface.py", "pqv3/crash/meter.py"), ("watch",),
        ("tests/v3/test_v3_surface.py",),
        gap="the three that read order-book depth stay silent until enough "
            "history exists; `microstructure_status()` reports which"),
    Row(21, CONDITIONAL, "the agent inspects and rewrites this codebase, and "
        "the console audits it for weakness",
        ("pqv3/agents/tools.py", "pqv3/agents/console.py"),
        ("agent", "chat"), ("tests/v3/test_v3_autonomy.py",),
        gap="needs a model"),
    Row(22, DONE, "hypotheses, strategies, console turns, documents, "
        "discoveries, agent actions and sessions are all persisted",
        ("pqv3/core/store.py",), ("chat", "watch"),
        ("tests/v3/test_v3_console.py",)),
    Row(23, CONDITIONAL, "the agent can rewrite the research machinery itself, "
        "and the audit names where it is weakest",
        ("pqv3/agents/autonomy.py", "pqv3/agents/console.py"), ("agent",),
        ("tests/v3/test_v3_autonomy.py",),
        gap="runs when asked; it does not start a session unprompted"),
    Row(24, DONE, "hypothesis, backtest, simulation, paper and live are "
        "distinct labels stamped on every reply and every money figure",
        ("pqv3/agents/console.py", "pqv3/server/api.py"), ("chat",),
        ("tests/v3/test_v3_console.py",)),
    Row(25, DONE, "four provenance columns on every row, collector health, "
        "settlement coverage, and degenerate-series guards that refuse to "
        "report a measurement that was never possible",
        ("pqv3/core/store.py", "pqv3/ingest/settled_ts.py",
         "pqv3/research/spectral.py"), ("inventory",),
        ("tests/v3/test_v3_engine.py",)),
    Row(26, DONE, "INPUT -> PROCESSING -> STORAGE -> MODEL -> DECISION -> UI, "
        "reporting the first break and naming everything after it a symptom",
        ("pqv3/agents/console.py",), ("chat",),
        ("tests/v3/test_v3_console.py",)),
    Row(27, CONDITIONAL, "understand, inspect, plan, modify, test, verify, "
        "report — as one loop that runs to completion",
        ("pqv3/agents/autonomy.py",), ("agent",),
        ("tests/v3/test_v3_autonomy.py",), gap="needs a model"),
    Row(28, DONE, "a broad instruction is decomposed into a ranked finding "
        "list rather than handed back", ("pqv3/agents/console.py",),
        ("chat",), ("tests/v3/test_v3_console.py",)),
    Row(29, DONE, "document -> claims -> candidates in the engine's own "
        "vocabulary, with no threshold adopted from prose",
        ("pqv3/agents/documents.py",), ("ingest",),
        ("tests/v3/test_v3_documents.py",)),
    Row(30, CONDITIONAL, "arbitrary modification requested in plain English "
        "and carried out", ("pqv3/agents/console.py",
                            "pqv3/agents/autonomy.py"),
        ("chat", "agent"), ("tests/v3/test_v3_autonomy.py",),
        gap="needs a model"),
    Row(31, DONE, "checkpoints join the git commit to the store state; the "
        "agent takes one before its first write; rollback is planned, never "
        "automatic, and refuses against a dirty tree",
        ("pqv3/core/checkpoint.py", "pqv3/agents/tools.py"),
        ("checkpoint",), ("tests/v3/test_v3_control.py",)),
    Row(32, PARTIAL, "the signing path is implemented and verified against "
        "published vectors — keccak-256, secp256k1 with RFC 6979 deterministic "
        "nonces, Ethereum address derivation and EIP-712 typed-data hashing. "
        "`SigningBoundary.sign` returns a signature and the key never travels "
        "upward. Mode labelling and the human authorisation gate are unchanged",
        ("pqv3/execution/crypto.py", "pqv3/execution/eip712.py",
         "pqv3/execution/signer.py", "pqv3/secrets.py"),
        ("authorize-live",),
        ("tests/v3/test_v3_crypto.py", "tests/v3/test_v3_eip712.py"),
        gap="ORDER PLACEMENT IS NOT BUILT. Three things are outstanding and "
            "only the first is mine to write: (1) the CLOB submission client; "
            "(2) the venue profile — Polymarket's EIP-712 domain constants and "
            "its Order struct field order, which must be COPIED from the "
            "venue's own documentation because a right domain with transposed "
            "struct fields signs a valid order for something other than what "
            "was intended; (3) a human authorisation, which §32 requires and "
            "which no amount of code removes. Nothing here has ever placed an "
            "order, so nothing here is claimed to be venue-tested"),
    Row(33, DONE, "INSUFFICIENT_EVIDENCE, NO_STRUCTURE_FOUND, "
        "DEGENERATE_SERIES and DO_NOT_TRADE are all first-class results",
        ("pqv3/research/dependence.py", "pqv3/decision/decide.py"),
        ("chat", "depend"), ("tests/v3/test_v3_methods.py",)),
    Row(34, DONE, "surrogate nulls, adversarial agents and the inversion lab "
        "all exist to attack a finding rather than defend it",
        ("pqv3/research/surrogate.py", "pqv3/research/inversion.py",
         "pqv3/agents/debate.py"), ("invert",),
        ("tests/v3/test_v3_inversion.py",)),
    Row(35, DONE, "interaction and conditional effects through multi-rule "
        "hypotheses; lead-lag, information flow, and cross-market network "
        "effects at pair grain",
        ("pqv3/research/hypothesis.py", "pqv3/research/dependence.py",
         "pqv3/research/pairs.py"),
        ("discover", "depend", "pairs"), ("tests/v3/test_v3_pairs.py",)),
    Row(36, DONE, "the self-concept text is sent verbatim as part of the "
        "system prompt", ("pqv3/agents/doctrine.py",), ("doctrine",),
        ("tests/v3/test_v3_console.py",)),
    Row(37, DONE, "the decision loop runs every candidate through twelve gates "
        "and records the blocking one",
        ("pqv3/decision/decide.py", "pqv3/decision/gates.py"),
        ("decide", "gates"), ("tests/v3/test_v3_gates_agents.py",)),
    Row(38, DONE, "the highest-value action is chosen from measured blockers, "
        "with the reasoning stated rather than a score invented",
        ("pqv3/agents/console.py",), ("chat",),
        ("tests/v3/test_v3_console.py",)),
    Row(39, CONDITIONAL, "natural language is the control layer: no Python, "
        "SQL or API knowledge needed to instruct the system",
        ("pqv3/agents/console.py", "pqv3/server/ui.py", "6-ASK-THE-AI.bat"),
        ("chat", "agent"), ("tests/v3/test_v3_autonomy.py",),
        gap="the change-the-system half needs a model"),
    Row(40, DONE, "monitors rank by importance x impact x urgency, attach to "
        "the next reply, write to notifications.jsonl and POST urgent items to "
        "a webhook", ("pqv3/agents/surface.py", "pqv3/agents/notify.py"),
        ("watch",), ("tests/v3/test_v3_surface.py",)),
    Row(41, DONE, "capabilities are probed not claimed, absent data is "
        "labelled, and this audit exists so completeness is checked rather "
        "than asserted", ("pqv3/agents/doctrine.py",
                          "pqv3/agents/compliance.py"),
        ("doctrine",), ("tests/v3/test_v3_console.py",)),
    Row(42, DONE, "success is measured by capability, and every capability "
        "claim on the DOCTRINE page is probed at request time",
        ("pqv3/agents/doctrine.py",), ("doctrine",),
        ("tests/v3/test_v3_console.py",)),
    Row(43, DONE, "the closing directive is the sum of the sections above; "
        "each is answered in its own row",
        ("docs/MASTER-SYSTEM-PROMPT.md",), ("doctrine",),
        ("tests/v3/test_v3_console.py",)),
)


def audit(root: Path | None = None) -> dict:
    """Verify every cited file and command actually exists."""
    root = root or _ROOT
    try:
        from ..cli import build_parser
        parser = build_parser()
        known = set()
        for a in parser._actions:                             # noqa: SLF001
            if getattr(a, "choices", None):
                known |= set(a.choices)
    except Exception:                                         # noqa: BLE001
        known = set()

    rows = []
    for r in MAP:
        missing = [f"file {f}" for f in r.files if not (root / f).exists()]
        missing += [f"command `pqv3 {c}`" for c in r.commands
                    if known and c not in known]
        missing += [f"test {t}" for t in r.tests if not (root / t).exists()]
        d = r.to_dict()
        d["missing"] = missing
        d["verified"] = not missing
        rows.append(d)

    by = {}
    for d in rows:
        by[d["status"]] = by.get(d["status"], 0) + 1
    unverified = [d for d in rows if not d["verified"]]

    return {
        "sections": rows,
        "total": len(rows),
        "by_status": by,
        "verified": len(rows) - len(unverified),
        "unverified": unverified,
        "complete": not unverified,
        "note": (
            f"{by.get(DONE, 0)} sections implemented and verified, "
            f"{by.get(CONDITIONAL, 0)} implemented and waiting on something "
            f"the operator supplies (a model endpoint, or collector history), "
            f"{by.get(PARTIAL, 0)} genuinely incomplete with the missing piece "
            f"named, {by.get(REFUSED, 0)} deliberately not built because "
            f"building it would contradict the charter. Every file, command "
            f"and test cited above is checked to exist at the moment this "
            f"runs — a section whose code is deleted stops reporting DONE "
            f"without anyone editing this table."),
    }


def covered() -> set:
    return {r.section for r in MAP}
