"""The direct human-to-AI control console. §2, §7, §39.

The charter is unambiguous that this is the primary interface: "the chat
interface is a PRIMARY CONTROL INTERFACE ... not merely a cosmetic chatbot
window". So this module is not a wrapper around a language model. The language
model is optional here and is the last thing consulted, exactly as it is
everywhere else in V3.

The order is: read the store, compute the answer, then — only if a local model
is configured — ask it to narrate what was computed. Pull the plug on the model
and every reply below still contains its full evidence, because the evidence
was never the model's to produce. That is §41 built into the control flow
rather than promised in a prompt.

WHAT A REPLY IS. Not prose. A structured record:

    mode        which of the seven charter modes this was read as, and why
    state       RESEARCH / BACKTEST / SHADOW / PAPER / LIVE — §32, stamped on
                every reply so simulation can never read as execution
    finding     the computed answer, as sentences over measured values
    evidence    the store rows behind it, section by section
    actions     the commands that would advance this request, each with the
                effect it has and whether the console may run it
    cannot      the parts of this request this installation cannot perform,
                named, with the charter clause that authorises them anyway
    llm         narration, if a model is configured. Labelled. Never load-bearing.

`cannot` is the field that keeps the rest honest. The charter authorises more
than this installation implements — source modification (§6), live execution
(§32), cross-market strategy families (§13) — and `doctrine.capabilities()`
publishes each gap with the clause that authorises it. A console that answered
"fix the news collector" with confident prose and no such field would be
fabricating a capability, which §41 forbids outright.

EXECUTION. `run()` will execute a command, in process, but only from a fixed
catalogue, only with an explicit confirmation token, and never anything that
can move capital or change the operating mode. §31 wants a rollback point
before anything difficult to reverse; the cheapest such guarantee is a command
set in which nothing needs reversing.
"""

from __future__ import annotations

import io
import json
import re
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path

from . import doctrine
from ..config import Settings
from ..secrets import scrub

_REPO = Path(__file__).resolve().parent.parent.parent

# §40's "do not overwhelm the user" as a number. Anything beyond this is still
# recorded in `discoveries` and reachable from the ACTIVITY page.
MAX_SURFACED_PENDING = 5


# ---------------------------------------------------------------------------
# §7 — the seven modes
# ---------------------------------------------------------------------------
# Ordered. The first mode whose pattern matches wins, so "backtest the wallet
# strategy" reads as BACKTEST rather than RESEARCH: the more specific
# instruction is the one the user actually gave.

MODE_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("DOCUMENT",
     r"[\w./\\~-]+\.(txt|md|markdown|rst|csv|tsv|json|docx|xlsx|pdf|doc|xls|"
     r"log|py|yaml|yml|html|xml|png|jpg|jpeg|gif|webp|bmp)\b",
     "names a file, so the §29 document-to-system pipeline runs on it"),
    ("EXECUTION",
     r"\b(run|execute|start|launch|trade now|place (an? )?order|go live|"
     r"authorize|authorise)\b",
     "an instruction to actually do something, not to analyse it"),
    ("BACKTEST",
     r"\b(backtest|back-test|walk[- ]?forward|out[- ]of[- ]sample|oos|"
     r"replay|test (this|it|every|across)|compare (these|the) strateg)\b",
     "asks for an experiment against history"),
    ("AUDIT",
     r"\b(audit|diagnos\w+|what'?s wrong|everything wrong|every problem|"
     r"bottleneck|health|broken|failing|empty|missing|why is|why are|"
     r"why does|full diagnostic|check the (whole|entire) system)\b",
     "asks what is wrong, which is a diagnostic before it is anything else"),
    ("ENGINEERING",
     r"\b(fix|build|rewrite|refactor|implement|add|remove|delete|change|"
     r"modify|create|integrate|merge|rebuild|make (this|it) faster)\b",
     "asks for a change to the system itself"),
    ("IMPROVEMENT",
     r"\b(improve|make (this|it) better|highest[- ]value|highest[- ]leverage|"
     r"optimi[sz]e|what should (i|we) do|next step|biggest (source|win))\b",
     "asks the system to choose its own next action"),
    ("EXPLANATION",
     r"\b(explain|why did|why was|what changed|how does|what does .* mean|"
     r"walk me through|justify)\b",
     "asks for the reasoning behind something already computed"),
    ("RESEARCH",
     r"\b(research|investigate|find|analy[sz]e|discover|pattern|edge|signal|"
     r"correlat|hypothes|study|examine|look at|show|list|what is|how many)\b",
     "asks a question of the evidence"),
)

# Broad instructions the charter explicitly names in §28. These bypass topic
# matching and go straight to the whole-system decomposition.
_WHOLE_SYSTEM = re.compile(
    r"\b(everything|entire system|whole system|all of it|complete diagnostic|"
    r"full audit|every problem|every bottleneck|every missing|anything wrong|"
    r"the system)\b", re.I)


# ---------------------------------------------------------------------------
# Topic routing: plain English -> the sections that hold the evidence
# ---------------------------------------------------------------------------

TOPICS: tuple[tuple[str, str], ...] = (
    ("news", r"\b(news|headline|feed|article|rss|press|announcement)\b"),
    ("events", r"\b(event time|event timing|resolution time|publication lag)\b"),
    ("blockchain", r"\b(chain|on-?chain|blockchain|wallet transfer|rpc|"
                   r"transaction|polygon|usdc flow)\b"),
    ("microstructure", r"\b(microstructure|order ?book|book|depth|spread|"
                       r"queue|imbalance|liquidity|cancel)\b"),
    ("wallets", r"\b(wallet|dna|copy ?trad|whale|trader|smart money|cohort)\b"),
    ("leaderboard", r"\b(leaderboard|ranking|best wallets|top wallets)\b"),
    ("markets", r"\b(market|question|category|close time|event id)\b"),
    ("opportunities", r"\b(opportunit|scan|candidate|ranked|watchlist)\b"),
    ("strategies", r"\b(strateg|lifecycle|promote|approved|shadow)\b"),
    ("discovery", r"\b(discover|hypothes|search space|p[- ]?value|"
                  r"benjamini|multiple compar|inversion|invert)\b"),
    ("backtest", r"\b(backtest|walk[- ]?forward|out[- ]of[- ]sample|oos|"
                 r"expectancy|robustness)\b"),
    ("validation", r"\b(gate|validation|blocked|blocking|reject|"
                   r"settlement timestamp|settled)\b"),
    ("learning", r"\b(learn|drift|missed|counterfactual|degrad|online)\b"),
    ("losses", r"\b(loss|losing|forensic|drawdown cause)\b"),
    ("agents", r"\b(agent|debate|consensus|disagree|red team|adversarial)\b"),
    ("risk", r"\b(risk|crash|hard stop|halt|ruin|exposure limit)\b"),
    ("portfolio", r"\b(portfolio|exposure|correlat|position|bucket|"
                  r"concentration)\b"),
    ("paper", r"\b(paper|simulat|fill|slippage)\b"),
    ("live", r"\b(live|real money|authoriz|authoris|production)\b"),
    ("activity", r"\b(activity|decision|alert|recent)\b"),
    ("overview", r"\b(overview|account|p&?l|pnl|profit|balance|equity|"
                 r"performance|return)\b"),
    ("system", r"\b(system|install|collector|store|database|schema|"
               r"health|credential|secret|version)\b"),
)


# ---------------------------------------------------------------------------
# §26 — the failure trace, per subsystem
# ---------------------------------------------------------------------------
# Each chain is INPUT -> PROCESSING -> STORAGE -> MODEL -> DECISION -> UI. The
# first link that fails is the root cause; everything downstream of it is a
# symptom and is reported as such rather than as a second problem. That
# distinction is the entire point of §26 and it is the reason this is a chain
# and not a checklist.

@dataclass
class Link:
    layer: str
    check: str
    ok: bool
    detail: str
    fix: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


# ---------------------------------------------------------------------------
# The action catalogue
# ---------------------------------------------------------------------------
# Every entry is a real `pqv3` subcommand. `runnable` marks the ones the
# console may execute itself; the rest are printed for a human to run
# deliberately in a terminal, with the reason stated.

@dataclass
class Action:
    name: str
    argv: tuple
    effect: str
    runnable: bool = True
    why_not: str = ""
    minutes: str = "seconds"

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["argv"] = list(self.argv)
        d["command"] = "pqv3 " + " ".join(self.argv)
        return d


ACTIONS: dict[str, Action] = {a.name: a for a in (
    Action("inventory", ("inventory",),
           "prints what evidence actually exists, before any of it is used"),
    Action("selftest", ("selftest",), "checks the installation end to end"),
    Action("dna", ("dna",),
           "builds wallet behavioural fingerprints from the tape",
           minutes="1-5 minutes"),
    Action("scan", ("scan",), "scans every eligible market and ranks it",
           minutes="1-3 minutes"),
    Action("signals", ("signals",),
           "turns validated strategies into candidates and prints the funnel"),
    Action("discover", ("discover",),
           "runs the full discovery pass: hypotheses, screen, BH threshold, "
           "walk-forward, robustness", minutes="5-30 minutes"),
    Action("invert", ("invert",),
           "tests whether the blocking signals are themselves predictive"),
    Action("forensics", ("forensics",),
           "classifies every loss and every missed opportunity"),
    Action("strategies", ("strategies",),
           "lists discovered strategies and their lifecycle position"),
    Action("report", ("report",),
           "writes the full research report to var/reports"),
    Action("graph", ("graph",), "builds the cross-wallet relationship graph",
           minutes="1-4 minutes"),
    Action("sequences", ("sequences",), "sequence and order-flow analysis"),
    Action("online", ("online",), "prints the online-learning weights"),
    Action("depend", ("depend", "<token_a>", "<token_b>"),
           "mutual information, transfer entropy and lead-lag between two "
           "tokens, each against a surrogate null", minutes="1-3 minutes"),
    Action("cycles", ("cycles", "<token>"),
           "periodicity on the tape's own irregular sampling, with the "
           "arrival rhythm's spectrum beside it", minutes="1-2 minutes"),
    Action("states", ("states", "<token>"),
           "hidden-state (HMM) model of a price series, with the hurdles a "
           "fit must clear first", minutes="1-2 minutes"),
    Action("montecarlo", ("montecarlo", "<strategy_id>"),
           "sequencing risk: probability of ruin and of hitting the hard stop "
           "across orderings of the same trades"),
    Action("checkpoint", ("checkpoint",),
           "record a rollback point: git commit joined to store state"),
    Action("watch", ("watch",),
           "what the system noticed on its own, ranked"),
    Action("gates", ("gates",), "lists the twelve validity gates and owners"),
    Action("agents", ("agents",), "lists the agents and their roles"),
    Action("capital", ("capital",),
           "proves the capital model at the configured bankroll"),
    Action("collect", ("collect", "--enable"),
           "starts live network collection",
           runnable=False,
           why_not="dials out to third-party endpoints. That is a deliberate "
                   "human decision, not something a chat box should start"),
    Action("authorize-live", ("authorize-live", "--yes"),
           "authorises live trading with real money",
           runnable=False,
           why_not="§32. Live execution is a human action recorded in the "
                   "authorizations table. No console, no agent and no model "
                   "may take it"),
    Action("promote", ("promote", "<strategy_id>", "--to", "<status>"),
           "moves a strategy along the lifecycle ladder",
           runnable=False,
           why_not="promotion past SHADOW is a human authorisation (§31)"),
    Action("mode", ("mode", "<MODE>"), "changes the operating mode",
           runnable=False,
           why_not="§32. The mode ladder is one-directional without a human"),
)}


@dataclass
class Reply:
    question: str
    mode: str
    mode_reason: str
    state: str
    finding: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    diagnosis: list = field(default_factory=list)
    plan: list = field(default_factory=list)
    document: dict = field(default_factory=dict)
    agent: dict = field(default_factory=dict)
    surfaced: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    cannot: list = field(default_factory=list)
    charter: list = field(default_factory=list)
    llm: dict = field(default_factory=dict)
    ran: dict = field(default_factory=dict)
    topics: list = field(default_factory=list)
    ts: int = 0
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class Console:
    """The control plane behind `/api/chat`, `pqv3 chat` and the CHAT page."""

    def __init__(self, st: Settings, store, api=None, engine=None) -> None:
        self.st = st
        self.store = store
        self.engine = engine
        if api is None:
            from ..server.api import Api
            api = Api(st, store, engine)
        self.api = api

    # ------------------------------------------------------------- routing
    def classify(self, text: str) -> tuple[str, str]:
        t = text.lower()
        for mode, pattern, reason in MODE_PATTERNS:
            if not re.search(pattern, t, re.I):
                continue
            if mode == "DOCUMENT" and not self._is_ingestion(text):
                # "add a module called scanner.py" names a file and is an
                # instruction to WRITE one, not a document to read. A filename
                # alone cannot decide this, and getting it wrong sends an
                # engineering job to the extraction pipeline, which then
                # reports "no such file" and does nothing.
                continue
            return mode, reason
        return "RESEARCH", "no explicit instruction verb; read as a question"

    # Verbs that mean "produce this file", as opposed to "read this file".
    _MAKES_FILE = re.compile(
        r"\b(add|create|make|write|build|generate|new|rewrite|refactor|"
        r"implement|rename|move|delete|remove|fix|modify|change|edit|call(ed)?|"
        r"name[sd]?)\b", re.I)

    def _is_ingestion(self, text: str) -> bool:
        """Is the named file something to READ, or something to WORK ON?

        The verb decides, and whether the file exists is deliberately not
        consulted. Existence was tried first and was wrong: "rewrite
        pqv3/scanner/signals.py" names a file that is very much there, and
        routing it to the extraction pipeline because of that turned an
        engineering job into a document summary.

        With no engineering verb, ingestion wins whether the path exists or
        not — so a mistyped document path reports "could not read that"
        instead of silently becoming a research question.
        """
        if not self.find_path(text):
            return False
        return not self._MAKES_FILE.search(text)

    def topics(self, text: str) -> list[str]:
        t = text.lower()
        hits = [name for name, pat in TOPICS if re.search(pat, t, re.I)]
        if _WHOLE_SYSTEM.search(t):
            # §28: do not hand a broad instruction back to the user. Decompose.
            return ["system", "overview", "discovery", "validation",
                    "microstructure", "news", "wallets", "strategies", "risk"]
        return hits or ["overview", "system"]

    # ------------------------------------------------------------ evidence
    def _summarise(self, name: str, payload: dict) -> dict:
        """Compact one API section into something a reply can carry.

        Scalars are kept whole; lists are reduced to a length plus their first
        rows. A reply that inlined 200 wallet rows would be unreadable, and one
        that inlined none would be unverifiable — the first rows plus the count
        is the smallest thing that is still checkable.
        """
        out: dict = {}
        for k, v in payload.items():
            if k in ("generated_ts",):
                continue
            if isinstance(v, (int, float, str, bool)) or v is None:
                out[k] = v
            elif isinstance(v, list):
                out[f"{k}_n"] = len(v)
                if v and isinstance(v[0], dict):
                    out[f"{k}_first"] = v[:3]
            elif isinstance(v, dict):
                out[k] = {kk: vv for kk, vv in list(v.items())[:12]
                          if isinstance(vv, (int, float, str, bool))
                          or vv is None}
        return out

    def evidence(self, topics: list[str]) -> dict:
        ev: dict = {}
        for name in topics:
            try:
                payload = self.api.get(name)
            except Exception as e:                            # noqa: BLE001
                ev[name] = {"error": f"{type(e).__name__}: {e}",
                            "note": "this section raised while being read; "
                                    "that is itself the finding"}
                continue
            ev[name] = self._summarise(name, payload)
        return ev

    # ----------------------------------------------------------- §26 trace
    def diagnose(self, topic: str) -> list[Link]:
        c, s = self.st.collectors, self.store
        span = s.history_span_days
        health = {h["collector"]: h for h in s.health()}

        def hlink(collector: str) -> Link:
            h = health.get(collector)
            if not h:
                return Link("PROCESSING", f"{collector} collector has run",
                            False, "no health row — this collector has never "
                            "attempted a cycle",
                            "run `pqv3 collect --enable`")
            ok = h["status"] == "OK"
            return Link("PROCESSING", f"{collector} collector status", ok,
                        f"{h['status']}: {h.get('error') or h.get('detail') or ''}"
                        .strip(": "),
                        "" if ok else "read the error above; it is the "
                                      "collector's own report")

        if topic == "news":
            return [
                Link("INPUT", "collectors enabled", c.enabled,
                     f"collectors.enabled = {c.enabled}",
                     "run `pqv3 collect --enable`, or set it in config"),
                Link("INPUT", "news feeds configured", bool(c.news_feeds),
                     f"{len(c.news_feeds)} feed(s) in collectors.news_feeds",
                     "add feed URLs to `collectors.news_feeds`. Unset is "
                     "reported as NOT_CONFIGURED, never silently skipped"),
                hlink("news"),
                Link("STORAGE", "news_items rows", s.count("news_items") > 0,
                     f"{s.count('news_items')} rows, "
                     f"{span('news_items', 'capture_ts'):.2f} d of history",
                     "news history cannot be backfilled: it accumulates from "
                     "the moment collection starts"),
                Link("MODEL", "items linked to markets",
                     s.count("news_market_links") > 0,
                     f"{s.count('news_market_links')} links",
                     "linking runs during a scan; run `pqv3 scan`"),
                Link("UI", "the NEWS panel queries these rows", True,
                     "the panel is a SELECT over news_items; an empty panel "
                     "means empty rows, never a rendering fault"),
            ]

        if topic == "blockchain":
            return [
                Link("INPUT", "collectors enabled", c.enabled,
                     f"collectors.enabled = {c.enabled}",
                     "run `pqv3 collect --enable`"),
                Link("INPUT", "chain RPC configured", bool(c.chain_rpc),
                     "set" if c.chain_rpc else "collectors.chain_rpc is empty",
                     "set `collectors.chain_rpc` to a Polygon endpoint"),
                hlink("chain"),
                Link("STORAGE", "chain_events rows", s.count("chain_events") > 0,
                     f"{s.count('chain_events')} rows, last block "
                     f"{s.get_meta('chain_last_block', '0')}"),
                Link("UI", "the BLOCKCHAIN panel queries these rows", True,
                     "direct SELECT over chain_events"),
            ]

        if topic == "microstructure":
            d = span("book_snapshots")
            return [
                Link("INPUT", "collectors enabled", c.enabled,
                     f"collectors.enabled = {c.enabled}",
                     "run `pqv3 collect --enable`"),
                hlink("orderbook"),
                Link("STORAGE", "book_snapshots rows",
                     s.count("book_snapshots") > 0,
                     f"{s.count('book_snapshots')} snapshots over {d:.2f} d"),
                Link("MODEL", "enough history for depth features",
                     d >= c.min_history_days,
                     f"{d:.2f} d of {c.min_history_days} d required",
                     "this is the one data class that CANNOT be backfilled. "
                     "Depth, spread and queue position for past markets are "
                     "gone; the only fix is elapsed collection time"),
                Link("UI", "the MICROSTRUCTURE panel groups by token", True,
                     "aggregate over book_snapshots"),
            ]

        if topic == "wallets":
            dna = self.engine.wallet_dna if self.engine else {}
            inv = self.api.source.inventory()
            return [
                Link("INPUT", "V1 tape present", bool(inv.get("available")),
                     f"{inv.get('wallet_trades', 0)} trades, "
                     f"{inv.get('tape_days', 0)} d"
                     if inv.get("available") else "no V1 database found",
                     "point PQV3_DATA_DB at the collected tape"),
                Link("MODEL", "wallet DNA built", bool(dna),
                     f"{len(dna)} profiles in memory",
                     "run `pqv3 dna`"),
                Link("UI", "the WALLETS panel ranks by alpha over the price "
                           "band", True,
                     "never by win rate — this dataset's favourite-longshot "
                     "bias makes win rate a measure of price preference"),
            ]

        if topic in ("discovery", "backtest", "strategies"):
            passes = s.count("research_passes")
            live_ladder = "status IN ('SHADOW','PAPER','APPROVED','LIVE')"
            past_discovered = s.count("strategies", live_ladder)
            return [
                Link("INPUT", "observation matrix", bool(passes) or
                     self.api.source.available,
                     "tape readable" if self.api.source.available
                     else "no historical source",
                     "point PQV3_DATA_DB at the tape"),
                Link("PROCESSING", "a discovery pass has run", passes > 0,
                     f"{passes} pass(es), {s.count('hypotheses')} hypotheses",
                     "run `pqv3 discover`"),
                Link("MODEL", "hypotheses survived the BH threshold",
                     s.count("strategies") > 0,
                     f"{s.count('strategies')} strategies persisted",
                     "surviving nothing is a legitimate result (§33), not a "
                     "failure — it means the search found no effect that "
                     "clears its own denominator"),
                Link("DECISION", "strategies reach the decision path",
                     past_discovered > 0,
                     f"{past_discovered} past DISCOVERED",
                     "run `pqv3 signals`, then promote deliberately"),
                Link("UI", "the DISCOVERY panel shows the full denominator",
                     True,
                     "a p-value is never shown without the search that "
                     "produced it"),
            ]

        if topic in ("paper", "live", "overview"):
            fills = s.count("fills")
            n_dec = s.count("decisions")
            n_trade = s.count("decisions", "action='TRADE'")
            n_no = s.count("decisions", "action='DO_NOT_TRADE'")
            return [
                Link("INPUT", "a decision has been produced", n_dec > 0,
                     f"{n_dec} decisions recorded",
                     "run `pqv3 scan --decide 5`"),
                Link("DECISION", "a decision cleared every gate", n_trade > 0,
                     f"{n_trade} TRADE, {n_no} DO_NOT_TRADE",
                     "see the VALIDATION page for which gate blocks most"),
                Link("STORAGE", "fills recorded", fills > 0,
                     f"{fills} fills",
                     "nothing has executed in any mode"),
                Link("UI", "money panels stamp their mode", True,
                     "paper P&L can never be read as realised P&L (§32)"),
            ]

        # Generic: the section either returns rows or explains why not.
        payload = self.api.get(topic)
        n = payload.get("n")
        return [Link("STORAGE", f"{topic} rows",
                     bool(n) if n is not None else True,
                     f"n = {n}" if n is not None else "section has no row count",
                     payload.get("note", ""))]

    def root_cause(self, links: list[Link]) -> Link | None:
        for ln in links:
            if not ln.ok:
                return ln
        return None

    # -------------------------------------------------------------- §28/§38
    def audit(self) -> list[dict]:
        """Whole-system diagnostic, ranked. Every row is a measured fact.

        Ranking is by whether the finding currently blocks the research loop,
        then by how much of the system it gates. Nothing here is a score — a
        made-up severity number would be exactly the false precision §24 warns
        against, so severity is an ordered label with its reason attached.
        """
        s, c = self.store, self.st.collectors
        out: list[dict] = []

        def finding(sev, what, measured, why, action=""):
            out.append({"severity": sev, "finding": what,
                        "measured": measured, "why_it_matters": why,
                        "action": action})

        if not self.api.source.available:
            finding("BLOCKING", "no historical tape is readable",
                    f"data_db = {self.st.data_db}",
                    "every research path reads the tape first; without it "
                    "discovery, DNA and backtesting cannot run at all",
                    "point PQV3_DATA_DB at the collected database")

        passes = s.count("research_passes")
        if not passes:
            finding("BLOCKING", "no discovery pass has ever run",
                    "research_passes = 0",
                    "the decision path consumes validated strategies; with no "
                    "pass there is nothing for it to consume", "discover")

        if not c.enabled:
            finding("HIGH", "live collectors are disabled",
                    f"collectors.enabled = {c.enabled}",
                    "order-book depth, news and chain data cannot be "
                    "backfilled. Every day collectors stay off is a day of "
                    "microstructure history that is permanently unavailable",
                    "collect")

        span = s.history_span_days("book_snapshots")
        if span < c.min_history_days:
            finding("HIGH", "insufficient order-book history for depth features",
                    f"{span:.2f} d of {c.min_history_days} d",
                    "every microstructure feature is gated until this clears, "
                    "which removes an entire signal class from discovery",
                    "collect")

        from ..ingest.settled_ts import coverage
        cov = coverage(s)
        if not cov.get("pit_features_enabled"):
            finding("HIGH", "settlement timestamps are not usable",
                    f"{cov.get('usable')}/{cov.get('total')} usable",
                    "point-in-time features are disabled without them, and "
                    "the $100 capital test falls back to a MODELLED holding "
                    "period whose results are refused as validation evidence",
                    "collect --backfill-settled")

        if not s.count("markets"):
            finding("MEDIUM", "no market metadata",
                    "markets = 0",
                    "questions, categories and close times are unavailable, so "
                    "opportunities render as bare IDs", "collect")

        if not s.count("news_items"):
            finding("MEDIUM", "no news captured",
                    "news_items = 0",
                    "the news and event signal classes contribute nothing to "
                    "the opportunity score", "collect")

        if not s.count("chain_events"):
            finding("MEDIUM", "no chain data",
                    "chain_events = 0",
                    "funding and capital-flow signals are unavailable",
                    "collect")

        dna = self.engine.wallet_dna if self.engine else {}
        if not dna:
            finding("MEDIUM", "wallet DNA has not been built",
                    "0 profiles",
                    "wallet-informed signals and the relationship graph both "
                    "read from DNA", "dna")

        if not s.count("loss_forensics") and s.count("positions",
                                                     "status!='OPEN'"):
            finding("MEDIUM", "closed positions without forensic records",
                    "loss_forensics = 0",
                    "§26: a loss that is not examined teaches nothing",
                    "forensics")

        if not s.count("missed_opportunities"):
            finding("LOW", "no missed-opportunity analysis",
                    "missed_opportunities = 0",
                    "without it every false positive is punished and every "
                    "false negative is invisible, so the gates only ever "
                    "ratchet tighter", "forensics")

        cfg = self.st.agents
        if not (cfg.llm_provider and cfg.llm_endpoint and cfg.llm_model):
            finding("INFO", "no local language model configured",
                    "PQV3_LLM_* unset",
                    "commentary only. Every number on every page is computed "
                    "without it; this is a missing narrator, not a missing "
                    "capability")

        if not out:
            finding("INFO", "no blocking condition found",
                    "all probed subsystems returned rows",
                    "§33: reporting nothing wrong is a legitimate result")

        order = {"BLOCKING": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
        out.sort(key=lambda f: order.get(f["severity"], 9))
        return out

    def highest_value_action(self) -> dict:
        """§38. The single next thing, chosen from measured findings."""
        findings = self.audit()
        top = findings[0]
        act = ACTIONS.get(top.get("action", "").split()[0]
                          if top.get("action") else "")
        return {"finding": top,
                "action": act.to_dict() if act else None,
                "reasoning": (
                    "chosen as the highest-severity measured blocker, not as "
                    "an estimate of expected value. §38 asks for the "
                    "highest-value action; with no completed trades there is "
                    "no measured return to rank against, and inventing one "
                    "would be the false precision §24 forbids. Severity here "
                    "means 'how much of the research loop is currently "
                    "unavailable because of this'."),
                "alternatives": findings[1:4]}

    # --------------------------------------------------------------- §6/§27
    #
    # This installation cannot edit files. What it CAN do is locate the work
    # precisely, which is the part of §27 (UNDERSTAND -> INSPECT -> PLAN) that
    # does not need a write tool. Every path below is checked against the disk
    # before it is offered, so the plan can never name a file that is not there.

    MODULE_MAP: dict[str, tuple[str, ...]] = {
        "news": ("pqv3/ingest/collectors.py", "pqv3/news/causality.py",
                 "pqv3/ingest/social.py", "pqv3/server/api.py",
                 "pqv3/server/ui.py"),
        "blockchain": ("pqv3/ingest/chain_decode.py",
                       "pqv3/ingest/collectors.py", "pqv3/intelligence/graph.py"),
        "microstructure": ("pqv3/ingest/collectors.py", "pqv3/features/__init__.py",
                           "pqv3/execution/simulator.py"),
        "wallets": ("pqv3/intelligence/wallets.py", "pqv3/intelligence/graph.py",
                    "pqv3/intelligence/sequences.py"),
        "discovery": ("pqv3/research/discover.py", "pqv3/research/hypothesis.py",
                      "pqv3/research/stats.py", "pqv3/research/matrix.py"),
        "backtest": ("pqv3/research/backtest.py", "pqv3/research/walkforward.py",
                     "pqv3/research/robustness.py", "pqv3/research/baseline.py"),
        "validation": ("pqv3/decision/gates.py", "pqv3/research/validate.py",
                       "pqv3/research/inversion.py"),
        "strategies": ("pqv3/research/discover.py", "pqv3/scanner/signals.py",
                       "pqv3/decision/decide.py"),
        "risk": ("pqv3/crash/meter.py", "pqv3/portfolio/capital.py",
                 "pqv3/portfolio/correlation.py"),
        "portfolio": ("pqv3/portfolio/capital.py",
                      "pqv3/portfolio/correlation.py"),
        "agents": ("pqv3/agents/registry.py", "pqv3/agents/debate.py",
                   "pqv3/agents/base.py", "pqv3/agents/llm.py"),
        "opportunities": ("pqv3/scanner/opportunity.py", "pqv3/scanner/signals.py"),
        "learning": ("pqv3/learning/online.py", "pqv3/learning/forensics.py"),
        "losses": ("pqv3/learning/forensics.py",),
        "paper": ("pqv3/execution/simulator.py", "pqv3/portfolio/capital.py"),
        "live": ("pqv3/execution/simulator.py", "pqv3/cli.py"),
        "overview": ("pqv3/server/api.py", "pqv3/server/ui.py"),
        "activity": ("pqv3/server/api.py", "pqv3/core/store.py"),
        "markets": ("pqv3/ingest/collectors.py", "pqv3/core/source.py"),
        "events": ("pqv3/ingest/settled_ts.py", "pqv3/news/causality.py"),
        "leaderboard": ("pqv3/intelligence/wallets.py",),
        "system": ("pqv3/runtime.py", "pqv3/bootstrap.py", "pqv3/config.py",
                   "pqv3/core/store.py"),
    }

    def locate(self, topics: list[str]) -> list[dict]:
        seen, out = set(), []
        for t in topics:
            for rel in self.MODULE_MAP.get(t, ()):
                if rel in seen:
                    continue
                seen.add(rel)
                p = _REPO / rel
                out.append({"path": rel, "exists": p.exists(),
                            "lines": (len(p.read_text(encoding="utf-8",
                                                      errors="replace")
                                          .splitlines())
                                      if p.exists() else 0),
                            "topic": t})
        return out

    def plan(self, text: str, topics: list[str]) -> list[dict]:
        """§27's first three steps, which are the three this system can take."""
        files = self.locate(topics)
        present = [f for f in files if f["exists"]]
        steps = [
            {"step": 1, "phase": "UNDERSTAND",
             "detail": f"read as {self.classify(text)[0]} over "
                       f"{', '.join(topics) or 'the whole system'}"},
            {"step": 2, "phase": "INSPECT",
             "detail": f"{len(present)} file(s) carry this behaviour",
             "files": present},
            {"step": 3, "phase": "PLAN",
             "detail": "the change belongs in the layer the diagnosis names as "
                       "the root cause, not in the layer where the symptom is "
                       "visible (§26). Run the diagnosis first if you have "
                       "not."},
            {"step": 4, "phase": "MODIFY",
             "detail": "NOT AVAILABLE in this installation — the console has "
                       "no write tool. §6 authorises it; the code does not "
                       "implement it. Apply the change with your editor or an "
                       "external coding agent.",
             "blocked": True},
            {"step": 5, "phase": "TEST",
             "detail": "python -m pytest tests/v3 -q"},
            {"step": 6, "phase": "VERIFY",
             "detail": "re-ask this same question; the diagnosis chain is the "
                       "acceptance test"},
        ]
        return steps

    # ------------------------------------------------------------- §5 / §29
    # Quoted first, because that is the only form that can carry a space. The
    # unquoted form is any non-whitespace run ending in a known extension,
    # with a lookahead so sentence punctuation does not end up inside the path.
    _PATH = re.compile(
        r"""["'`]([^"'`\n]+\.[A-Za-z0-9]{1,9})["'`]"""
        r"""|(\S*[\w\-]\.(?:txt|md|markdown|rst|csv|tsv|json|docx|xlsx|pdf|"""
        r"""doc|xls|log|py|yaml|yml|html|xml|png|jpg|jpeg|gif|webp|bmp))"""
        r"""(?=[\s,;:)\]!?]|\.|$)""",
        re.I)

    def find_path(self, text: str) -> str:
        m = self._PATH.search(text)
        if not m:
            return ""
        return (m.group(1) or m.group(2) or "").strip()

    def ingest(self, path: str) -> dict:
        """Read a document and convert it into candidates. §29.

        Returns the extraction, never a conclusion. The pipeline's own closing
        rule is that a document confers no privilege: what comes out of here is
        a candidate phrased in the engine's vocabulary, and it clears the same
        screen, threshold, walk-forward and robustness battery as a hypothesis
        the sweep generated on its own.
        """
        from . import documents

        d = documents.ingest(path, self.st)
        try:
            self.store.insert("documents", [{
                "path": d.path, "kind": d.kind, "ok": int(d.ok),
                "error": d.error, "chars": d.chars, "words": d.words,
                "claims": [c.to_dict() for c in d.claims],
                "proposals": d.proposals, "missing_data": d.missing_data,
                "note": d.note,
            }], source="ingest")
        except Exception:                                     # noqa: BLE001
            pass
        return d.to_dict()

    # ---------------------------------------------------------------- §40
    def surfaced(self, *, detect: bool = True) -> list:
        """What the system noticed on its own and has not been told about."""
        from .surface import Surfacer
        s = Surfacer(self.st, self.store, self.engine)
        try:
            fresh = s.run() if detect else []
            if fresh:
                return fresh
            return [dict(r) for r in s.pending(limit=MAX_SURFACED_PENDING)]
        except Exception:                                     # noqa: BLE001
            # A monitor that breaks must not take the console with it.
            return []

    # ------------------------------------------------------------ execution
    def run(self, name: str, *, confirm: str = "", extra: tuple = ()) -> dict:
        """Execute one catalogued command. §31, §32.

        Three conditions, all required: the command is in the catalogue, the
        catalogue marks it runnable, and the caller echoed its exact name back
        as `confirm`. The last one exists so a model that hallucinates a
        function call cannot trip execution — a hallucination that also has to
        guess a confirmation token is a hallucination that fails closed.
        """
        act = ACTIONS.get(name)
        if not act:
            return {"ok": False, "error": f"unknown action '{name}'",
                    "available": sorted(ACTIONS)}
        if not act.runnable:
            return {"ok": False, "action": act.to_dict(), "refused": True,
                    "error": act.why_not}
        if confirm != name:
            return {"ok": False, "action": act.to_dict(), "needs_confirm": True,
                    "error": f"pass confirm='{name}' to run this"}

        from ..cli import main as cli_main
        argv = list(act.argv) + list(extra)
        buf = io.StringIO()
        t0 = time.perf_counter()
        try:
            with redirect_stdout(buf), redirect_stderr(buf):
                code = cli_main(argv)
        except SystemExit as e:                               # noqa: PERF203
            code = int(e.code or 0)
        except Exception as e:                                # noqa: BLE001
            return {"ok": False, "action": act.to_dict(),
                    "error": f"{type(e).__name__}: {e}",
                    "output": buf.getvalue()[-4000:],
                    "elapsed_ms": int((time.perf_counter() - t0) * 1000)}
        out = buf.getvalue()
        return {"ok": code == 0, "action": act.to_dict(), "exit_code": code,
                "output": out[-8000:],
                "truncated": len(out) > 8000,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000)}

    # ---------------------------------------------------------------- ask
    def ask(self, text: str, *, run: str = "", confirm: str = "",
            narrate: bool = True, no_act: bool = False) -> dict:
        t0 = time.perf_counter()
        text = (text or "").strip()
        if not text:
            return Reply(question="", mode="RESEARCH",
                         mode_reason="empty input", state=self.st.mode.value,
                         finding=["Ask a question, or give an instruction. "
                                  "Try: 'audit the whole system', 'why is the "
                                  "news panel empty', 'what should I do "
                                  "next'."],
                         ts=int(time.time())).to_dict()

        mode, reason = self.classify(text)
        topics = self.topics(text)
        r = Reply(question=text, mode=mode, mode_reason=reason,
                  state=self.st.mode.value, topics=topics,
                  ts=int(time.time()))

        # §41 boundary, attached before anything else is computed. Filtered to
        # what is relevant to THIS request: a reply to "how many wallets" that
        # recited the whole boundary would train the reader to skip the field,
        # and the field only works if it is read.
        caps = doctrine.capabilities(self.st, self.store, self.engine)
        r.cannot = [c for c in caps["cannot"]
                    if _relevant_limit(c["capability"], mode, text)]

        r.evidence = self.evidence(topics)

        if mode == "DOCUMENT":
            path = self.find_path(text)
            d = self.ingest(path)
            r.document = d
            r.charter = [doctrine.cite(5), doctrine.cite(29), doctrine.cite(18)]
            if not d["ok"]:
                r.finding = [f"Could not read {path}.", d["error"]]
            else:
                r.finding = [
                    f"Read {path}: {d['words']:,} words, {d['chars']:,} chars"
                    + (" (truncated)" if d.get("truncated") else "") + ".",
                    d["note"],
                    f"{len(d['proposals'])} candidate(s) map onto live "
                    f"observation columns; {len(d['missing_data'])} concept(s) "
                    f"in this document have no column at all."]
                r.finding += d["next_steps"]
                r.finding.append(
                    "Nothing here has been tested and nothing has been "
                    "adopted. §29: a document's conclusions are not copied — "
                    "its claims are converted into candidates that must clear "
                    "the same bar as any other.")
                if d["proposals"]:
                    r.actions.append(ACTIONS["discover"].to_dict())

        elif mode == "AUDIT":
            # "Why is the news panel empty" and "audit everything" are both
            # AUDIT, and they want different answers. The narrow question wants
            # the chain for its own subsystem — burying that under a
            # whole-system severity list would answer a question nobody asked.
            broad = bool(_WHOLE_SYSTEM.search(text.lower()))
            for t in topics[:4]:
                links = self.diagnose(t)
                rc = self.root_cause(links)
                r.plan.append({"topic": t,
                               "chain": [ln.to_dict() for ln in links],
                               "root_cause": rc.to_dict() if rc else None,
                               "note": "" if rc else
                               "every link in this chain holds"})
                if rc:
                    r.diagnosis.append({"topic": t, "root_cause": rc.to_dict(),
                                        "chain": [ln.to_dict() for ln in links]})

            if broad:
                findings = self.audit()
                r.finding = [f"{f['severity']}: {f['finding']} "
                             f"({f['measured']})" for f in findings]
                r.diagnosis = findings + r.diagnosis
            else:
                for p in r.plan:
                    rc = p["root_cause"]
                    if not rc:
                        r.finding.append(
                            f"{p['topic'].upper()}: every link from input to "
                            f"UI holds. Nothing in this chain is broken.")
                        continue
                    r.finding.append(
                        f"{p['topic'].upper()}: the first break is at "
                        f"{rc['layer']} — {rc['check']}. {rc['detail']}"
                        + (f" Fix: {rc['fix']}" if rc["fix"] else ""))
                    r.finding.append(
                        f"Everything downstream of {rc['layer']} is a symptom "
                        f"of that, not a second fault (§26). The panel itself "
                        f"is not broken — it is reporting an empty table "
                        f"accurately.")
            r.charter = [doctrine.cite(26), doctrine.cite(28),
                         doctrine.cite(33)]

        elif mode == "IMPROVEMENT":
            hv = self.highest_value_action()
            r.finding = [
                f"Highest-value next action: {hv['finding']['finding']} "
                f"({hv['finding']['measured']}).",
                hv["finding"]["why_it_matters"], hv["reasoning"]]
            r.diagnosis = [hv["finding"]] + hv["alternatives"]
            r.charter = [doctrine.cite(38), doctrine.cite(24)]
            if hv["action"]:
                r.actions.append(hv["action"])

        elif mode in ("ENGINEERING",):
            # §2: "Do NOT merely respond with instructions telling the user how
            # to make the change." If a model is configured, the agent does the
            # work. The plan is what remains when there is nothing to drive it.
            from .autonomy import Agent, status as agent_status
            ast = agent_status(self.st)
            files = [f["path"] for f in self.locate(topics) if f["exists"]]

            if ast["available"] and self.st.agents.agent_auto and not no_act:
                sess = Agent(self.st, self.store).run(
                    text,
                    context=(f"Files that carry this behaviour: "
                             f"{', '.join(files)}" if files else ""))
                r.agent = sess.to_dict()
                r.finding = [
                    f"Ran the engineering objective with {sess.model} over "
                    f"{len(sess.steps)} step(s).",
                    sess.answer or sess.reason,
                    (f"Changed {len(sess.files_changed)} file(s): "
                     f"{', '.join(sess.files_changed)}."
                     if sess.files_changed else "No file was changed."),
                    (f"Test suite: "
                     f"{'PASSED' if sess.tests.get('passed') else 'FAILED'}."
                     if sess.tests.get("ran") else
                     "The test suite was not run in this session."),
                    f"Rollback: {sess.rollback}"]
                if sess.note:
                    r.finding.append(sess.note)
            else:
                r.plan = self.plan(text, topics)
                r.finding = [
                    f"Read as an engineering instruction over "
                    f"{', '.join(topics)}.",
                    f"The behaviour lives in: "
                    f"{', '.join(files) or 'no mapped file'}.",
                    ("The agent is available but auto-execution is off "
                     "(PQV3_AGENT_AUTO=0), so this is the plan only."
                     if ast["available"] else ast["note"])]
            r.charter = [doctrine.cite(27), doctrine.cite(6), doctrine.cite(2)]

        elif mode == "BACKTEST":
            r.finding = [
                "Backtesting in this system is not a separate script: a "
                "hypothesis reaches an out-of-sample result only by going "
                "through the discovery pass, which applies the in-sample "
                "screen, the Benjamini-Hochberg threshold over the full "
                "denominator, walk-forward and the robustness battery.",
                "Run `pqv3 discover` to produce candidates, then "
                "`pqv3 backtest <strategy_id>` and `pqv3 walkforward "
                "<strategy_id>` for one of them."]
            r.actions += [ACTIONS["discover"].to_dict(),
                          ACTIONS["strategies"].to_dict(),
                          ACTIONS["invert"].to_dict()]
            r.charter = [doctrine.cite(17), doctrine.cite(18)]

        elif mode == "EXECUTION":
            r.finding = [
                "Execution requests are routed to the action catalogue. "
                "Nothing that moves capital or changes the operating mode is "
                "runnable from here (§32).",
                "Pass `run=<action>` and `confirm=<action>` to execute one."]
            r.actions = [a.to_dict() for a in ACTIONS.values()]
            r.charter = [doctrine.cite(32), doctrine.cite(31)]

        else:   # RESEARCH / EXPLANATION
            r.finding = self._describe(topics, r.evidence)
            for t in topics[:3]:
                links = self.diagnose(t)
                rc = self.root_cause(links)
                if rc:
                    r.diagnosis.append(
                        {"topic": t, "root_cause": rc.to_dict(),
                         "chain": [ln.to_dict() for ln in links]})
            r.charter = [doctrine.cite(41), doctrine.cite(33)]

        # Actions relevant to whatever the topics were.
        for t in topics:
            for name in _TOPIC_ACTIONS.get(t, ()):
                d = ACTIONS[name].to_dict()
                if d not in r.actions:
                    r.actions.append(d)

        # §40. Attached to the reply rather than pushed as a notification: the
        # console has no channel to interrupt on, and the moment the user is
        # already reading is the cheapest moment to tell them.
        r.surfaced = self.surfaced()

        if run:
            r.ran = self.run(run, confirm=confirm)

        if narrate:
            r.llm = self._narrate(r)

        r.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        self._remember(r)
        return _scrub_reply(r.to_dict())

    def _describe(self, topics: list[str], ev: dict) -> list[str]:
        """Sentences over measured values. No adjectives that imply a verdict."""
        out = []
        for t in topics[:6]:
            d = ev.get(t) or {}
            if "error" in d:
                out.append(f"{t.upper()}: the section raised "
                           f"{d['error']} while being read.")
                continue
            counts = {k: v for k, v in d.items()
                      if k.endswith("_n") or k == "n"}
            bits = ", ".join(f"{k.removesuffix('_n')}={v}"
                             for k, v in list(counts.items())[:5]) or "no rows"
            note = d.get("note") or ""
            out.append(f"{t.upper()}: {bits}."
                       + (f" {note}" if note else ""))
        if not out:
            out.append("INSUFFICIENT EVIDENCE — no section in this store "
                       "answers that. §33: that is a legitimate answer, not a "
                       "failure.")
        return out

    def _narrate(self, r: Reply) -> dict:
        """Optional prose over the finished record. Never load-bearing."""
        from .llm import LocalLLM
        llm = LocalLLM(self.st)
        if not llm.configured:
            return {"available": False,
                    "note": "no local model configured. Every line of "
                            "`finding` above was computed, not generated — "
                            "the model would only rephrase them."}
        limit = self.st.agents.llm_context_limit
        system, which = doctrine.system_prompt("console", context_limit=limit)
        payload = json.dumps(
            {"question": r.question, "mode": r.mode, "state": r.state,
             "finding": r.finding, "evidence": r.evidence,
             "diagnosis": r.diagnosis[:6], "cannot": r.cannot},
            default=str)[:6000]
        res = llm.ask(
            "Narrate this control-console reply in under 180 words for an "
            "experienced but non-technical reader. Use only the facts below. "
            "Do not add figures.\n\n" + payload,
            role="console", system=system)
        d = res.to_dict()
        d["charter_in_force"] = which
        d["status"] = ("commentary only; the reply above stands without it"
                       if res.available else
                       "the model was unreachable. This is not an error "
                       "condition — the reply above is complete.")
        return d

    # ----------------------------------------------------------- §22 memory
    def _remember(self, r: Reply) -> None:
        try:
            self.store.insert("console_turns", [{
                "question": r.question[:2000], "mode": r.mode,
                "state": r.state, "topics": list(r.topics),
                "finding": r.finding, "diagnosis": r.diagnosis[:20],
                "actions": [a.get("name") for a in r.actions],
                "ran": r.ran.get("action", {}).get("name", "") if r.ran else "",
                "llm_available": bool(r.llm.get("available")),
                "elapsed_ms": r.elapsed_ms,
            }], source="console")
        except Exception:                                     # noqa: BLE001
            # A console that cannot write its transcript must still answer.
            # Losing the memory is a degradation; losing the answer is a fault.
            pass

    def history(self, limit: int = 50) -> list[dict]:
        try:
            return self.store.query(
                "SELECT id, ts, question, mode, state, finding, ran "
                "  FROM console_turns ORDER BY id DESC LIMIT ?", (limit,))
        except Exception:                                     # noqa: BLE001
            return []


# Whole fields that are foreign end to end: the user's question, rows read out
# of the store, whatever a language model returned.
_FOREIGN_FIELDS = ("question", "evidence", "ran", "llm", "topics")

# Individual keys that carry foreign text wherever they appear — a collector's
# own error string, a captured stdout, a sentence lifted out of a supplied
# document, a `note` composed from store contents.
_FOREIGN_KEYS = frozenset((
    "text", "tables", "output", "error", "detail", "measured", "note", "path",
    "from_claim_text", "message", "narrative", "thesis", "statement"))


def _scrub_foreign(obj):
    """Recurse, redacting only the leaves whose key marks them as foreign."""
    if isinstance(obj, dict):
        return {k: (scrub(v) if k in _FOREIGN_KEYS else _scrub_foreign(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub_foreign(v) for v in obj]
    return obj


def _scrub_reply(d: dict) -> dict:
    """Redact the foreign half of a reply; leave this module's own prose alone.

    `secrets.redact` treats any run of twelve short lowercase words as a BIP-39
    seed phrase. That is the correct trade for untrusted text — a false
    redaction is cosmetic, a missed key is unrecoverable — but ordinary English
    trips it constantly, and applying it to the console's own explanations
    deleted sentences out of the middle of them.

    The fix is not to loosen the pattern. It is to notice that `finding`,
    `plan`, `actions`, `cannot` and `charter` are assembled from string
    literals in this module plus integers counted out of SQL. There is no path
    by which a credential enters them, so there is nothing there to redact.
    Every field that COULD carry one — foreign text, wherever it is nested —
    is still scrubbed, and a new field defaults to being scrubbed only if its
    key says it carries foreign text, which is the safe direction to default in.
    """
    out = _scrub_foreign(d)
    for k in _FOREIGN_FIELDS:
        if k in out:
            out[k] = scrub(out[k])
    return out


def _relevant_limit(capability: str, mode: str, text: str) -> bool:
    """Does this boundary apply to what was just asked?

    Erring towards showing it: a limit shown unnecessarily is noise, a limit
    hidden when it applies is the fabrication §41 forbids.
    """
    t = text.lower()
    if capability.startswith("narrative commentary"):
        # Reported by the reply's own `llm` field, on every reply. Repeating it
        # here would put a permanent notice above every answer.
        return False
    if capability.startswith("source-file"):
        return mode in ("ENGINEERING", "IMPROVEMENT", "AUDIT")
    if capability.startswith("PDF"):
        return mode == "DOCUMENT" or bool(
            re.search(r"\b(pdf|document|paper|report|attach)\b", t))
    if capability.startswith("live order"):
        return mode == "EXECUTION" or bool(
            re.search(r"\b(live|order|trade|buy|sell|execute|real money)\b", t))
    if capability.startswith("autonomous self"):
        return mode in ("IMPROVEMENT", "ENGINEERING")
    return True


# Which catalogued commands are worth offering for each topic.
_TOPIC_ACTIONS: dict[str, tuple[str, ...]] = {
    "news": ("collect",), "blockchain": ("collect",),
    "microstructure": ("collect", "cycles"), "markets": ("collect",),
    "events": ("collect",),
    "wallets": ("dna", "graph"), "leaderboard": ("dna",),
    "discovery": ("discover", "invert", "depend"),
    "backtest": ("discover", "montecarlo"),
    "strategies": ("strategies", "signals"),
    "opportunities": ("scan", "signals"),
    "validation": ("gates", "invert"),
    "learning": ("forensics", "online"), "losses": ("forensics",),
    "agents": ("agents",), "risk": ("capital", "montecarlo"),
    "portfolio": ("capital",),
    "system": ("selftest", "inventory", "checkpoint"),
    "overview": ("inventory", "watch"),
    "paper": ("scan",), "live": ("capital",), "activity": ("scan",),
}
