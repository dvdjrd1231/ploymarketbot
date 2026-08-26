"""§5 / §29 — the document-to-system pipeline.

    DOCUMENT -> EXTRACT -> UNDERSTAND -> IDENTIFY CLAIMS -> IDENTIFY
    ASSUMPTIONS -> CONVERT TO TESTABLE HYPOTHESES -> MAP TO EXISTING
    ARCHITECTURE -> DETERMINE MISSING DATA -> ...

This module implements that chain up to and including "determine missing
data". It stops there deliberately: the next step is IMPLEMENT EXPERIMENT, and
§29's closing line is "do not blindly copy a document's conclusions". So a
claim extracted here becomes a *candidate* in the engine's own vocabulary and
nothing more. It enters the ordinary discovery pass, clears the same in-sample
screen, the same Benjamini-Hochberg threshold over the same denominator, the
same walk-forward and the same robustness battery as a mechanically generated
hypothesis. A document confers no privilege. Somebody's PDF asserting an edge
is, to this system, exactly one more untested string.

READING. Stdlib only, because the whole project is. That buys TXT, MD, CSV,
TSV, JSON, LOG, source files, and — via `zipfile` plus `xml.etree`, since both
are zip containers of XML — DOCX and XLSX. It does not buy PDF: every viable
parser is a dependency this project refuses to take, and guessing at a PDF's
text layer produces plausible-looking garbage, which is worse than refusing.
PDFs are declined by name with the reason attached.

THE HARD PART is not reading the file. It is that "smart money moves early"
and `w_secs_since_prev <= 900` are the same claim in two languages, and only
one of them can be tested. `map_to_features` does that translation against the
engine's real feature vocabulary, and — the half that matters — reports the
concepts that translate to *nothing*, because a claim resting on order-book
depth in an installation with nine days of book history is not a hypothesis
yet, it is a data requirement.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024        # a research note, not a corpus
MAX_CHARS = 400_000

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".log", ".json", ".csv",
                 ".tsv", ".py", ".yaml", ".yml", ".ini", ".cfg", ".sql",
                 ".html", ".xml"}

REFUSED = {
    ".pdf": ("PDF text extraction needs a third-party parser, and this project "
             "is standard-library only. Guessing at a PDF's text layer yields "
             "plausible-looking garbage, which §41 makes worse than refusing. "
             "Export it to .docx, .txt or .md and re-run"),
    ".doc": ("legacy .doc is a binary OLE format with no stdlib reader. Save "
             "as .docx"),
    ".xls": ("legacy .xls is a binary format with no stdlib reader. Save as "
             ".xlsx"),
    ".ppt": ("not supported"), ".pptx": ("not supported"),
}


# ---------------------------------------------------------------------------
# The engine's own vocabulary
# ---------------------------------------------------------------------------
# Left: the words a research document actually uses. Right: the observation
# columns those words correspond to. Anything a document says that does not
# land on the right-hand side is not testable here, and saying so is the point.

CONCEPT_FEATURES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("price level", r"\b(price|probability|odds|cheap|expensive|longshot|"
                    r"favou?rite|underpriced|overpriced|mispric\w+)\b",
     ("price", "price_vs_wallet_norm")),
    ("position size", r"\b(size|sizing|stake|notional|bet size|position size|"
                      r"large (bet|trade)|small (bet|trade)|conviction size)\b",
     ("notional", "rel_notional", "w_avg_notional")),
    ("wallet win rate", r"\b(win rate|hit rate|accuracy|strike rate|"
                        r"success rate)\b",
     ("w_win_rate", "w_roll_win_rate")),
    ("wallet profitability", r"\b(roi|return on|profitab\w+|pnl|p&l|"
                             r"expectancy|edge per trade)\b",
     ("w_roi", "w_roll_roi")),
    ("wallet skill", r"\b(skill|alpha|informed|smart money|sharp|edge|"
                     r"t.?stat|significan\w+)\b", ("w_edge_t",)),
    ("streaks", r"\b(streak|consecutive|in a row|tilt|revenge|cold|hot hand|"
                r"after a loss|after a win)\b",
     ("w_consec_losses", "w_consec_wins")),
    ("track record depth", r"\b(track record|experience|history|seasoned|"
                           r"settled|sample size|number of trades)\b",
     ("w_settled_n", "w_seen_n")),
    ("trading cadence", r"\b(frequency|how often|cadence|time since|"
                        r"recently active|dormant|burst|rapid)\b",
     ("w_secs_since_prev", "w_seen_n")),
    ("open exposure", r"\b(exposure|open position|committed capital|"
                      r"already holding)\b", ("w_open_notional",)),
    ("repeat conviction", r"\b(repeat|adds? to|doubl\w+ down|same market|"
                          r"same token|accumulat\w+|scaling in)\b",
     ("w_token_repeat", "w_market_repeat")),
    ("market activity", r"\b(volume|prints|trade flow|turnover|busy|active "
                        r"market|thin market|participation)\b",
     ("market_recent_prints",)),
    ("price momentum", r"\b(momentum|trend|drift|price move|moving|rally|"
                       r"selloff|reversal|mean revers\w+)\b",
     ("market_price_move",)),
    ("price velocity", r"\b(velocity|speed|accelerat\w+|fast move|sudden|"
                       r"sharp move|volatil\w+)\b", ("market_velocity",)),
    ("stale pricing", r"\b(stale|gap|dislocat\w+|lag|divergence|out of line|"
                      r"arbitrage)\b", ("tape_price_gap",)),
    ("time of day", r"\b(hour|time of day|session|overnight|morning|evening|"
                    r"weekend|us hours|asian)\b", ("hour_of_day",)),
    ("time to resolution", r"\b(time to (resolution|settle|expiry|close)|"
                           r"expir\w+|maturity|deadline|days? (out|left|to go)|"
                           r"near resolution|close to settlement)\b",
     ("secs_to_settle",)),
)

# Concepts this system understands but has no observation column for. A claim
# that rests on one of these is a DATA REQUIREMENT, not a hypothesis, and the
# distinction is load-bearing: testing it against a proxy would answer a
# different question and report the answer as if it were this one.
UNAVAILABLE_CONCEPTS: tuple[tuple[str, str, str], ...] = (
    ("order-book depth", r"\b(order.?book|depth|bid.?ask|spread|queue|level "
                         r"2|liquidity provision|market maker|cancel\w*|"
                         r"replenish\w*|imbalance)\b",
     "book_snapshots accumulate only from the moment collectors run, and "
     "cannot be backfilled. Until `collectors.min_history_days` is met these "
     "features are gated out of discovery entirely"),
    ("news and sentiment", r"\b(news|headline|sentiment|announcement|press|"
                           r"twitter|social|reddit|media|narrative)\b",
     "news_items has no history before collection starts and no matrix axis. "
     "The news layer informs the opportunity score, not the discovery pass"),
    ("on-chain flow", r"\b(on.?chain|blockchain|transfer|funding|deposit|"
                      r"withdraw\w*|wallet cluster|address (link|graph)|"
                      r"usdc flow)\b",
     "chain_events requires `collectors.chain_rpc`, and has no matrix axis"),
    ("cross-market structure", r"\b(cross.?market|correlated markets|"
                               r"related markets|basket|pairs? trade|"
                               r"cross.?sectional)\b",
     "the observation matrix is one row per wallet-trade, not per market-pair. "
     "Cross-market relationships need a different matrix build"),
    ("external model", r"\b(machine learning|neural|xgboost|random forest|"
                       r"llm|gpt|embedding|deep learning)\b",
     "no model-training path exists here, and §18 would demand more "
     "out-of-sample evidence from a fitted model than from a rule, not less"),
)

DIRECTION = (
    ("ge", r"\b(high\w*|great\w*|larg\w*|more|above|over|increas\w*|rising|"
           r"longer|older|stronger|better|exceed\w*|at least)\b"),
    ("le", r"\b(low\w*|small\w*|less|fewer|below|under|decreas\w*|falling|"
           r"shorter|younger|weaker|worse|within|at most|early|quick\w*|"
           r"recent\w*)\b"),
)

KIND_PATTERNS = (
    ("FORMULA", r"(=\s*[\w\s()+\-*/^.]{4,}|\b[a-z]+\s*=\s*[\d.]|"
                r"\b(kelly|sharpe|sigma|std ?dev|log|exp|sqrt)\b\s*[({])"),
    ("LIMITATION", r"\b(however|but |limitation|caveat|does not|doesn't|"
                   r"cannot|can't|fails? to|only if|not applicable|assumes "
                   r"perfect|ignore[sd]|excluded)\b"),
    ("ASSUMPTION", r"\b(assum\w+|suppose|given that|if we|we expect|"
                   r"presum\w+|for simplicity|treat\w* as|hold\w* constant)\b"),
    ("CLAIM", r"\b(outperform\w*|beat\w*|predict\w*|profitab\w*|edge|alpha|"
              r"significant\w*|increas\w*|decreas\w*|correlat\w*|lead\w*|"
              r"signal\w*|indicat\w*|tend\w* to|more likely|less likely|"
              r"is (?:a )?(?:good|strong|reliable)|works?|yields?|generates?)\b"),
)

# Not split on ':' — "the signal decays quickly: entries after two hours show
# no edge" is one claim, and splitting it produces a fragment that classifies
# as a claim while carrying none of the condition.
_SENT = re.compile(r"(?<=[.!?;])\s+|\n{2,}|\n(?=[-*•\d])")
_NUMERIC = re.compile(r"\d")


# ---------------------------------------------------------------------------

@dataclass
class Claim:
    text: str
    kind: str
    concepts: list = field(default_factory=list)
    features: list = field(default_factory=list)
    direction: str = ""
    testable: bool = False
    blocked_by: list = field(default_factory=list)
    quantified: bool = False
    caveat: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Document:
    path: str
    kind: str = ""
    ok: bool = False
    error: str = ""
    chars: int = 0
    words: int = 0
    truncated: bool = False
    text: str = ""
    tables: list = field(default_factory=list)
    claims: list = field(default_factory=list)
    proposals: list = field(default_factory=list)
    missing_data: list = field(default_factory=list)
    next_steps: list = field(default_factory=list)
    note: str = ""

    def to_dict(self, *, include_text: bool = False) -> dict:
        d = self.__dict__.copy()
        d["claims"] = [c.to_dict() if isinstance(c, Claim) else c
                       for c in self.claims]
        if not include_text:
            d["text"] = d["text"][:2000]
        return d


# ---------------------------------------------------------------------- read

def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _docx_text(path: Path) -> str:
    """DOCX is a zip of XML. Paragraphs are w:p, runs of text are w:t."""
    import xml.etree.ElementTree as ET
    out = []
    with zipfile.ZipFile(path) as z:
        names = [n for n in ("word/document.xml",) if n in z.namelist()]
        if not names:
            raise ValueError("no word/document.xml — not a Word file")
        root = ET.fromstring(z.read(names[0]))
        for para in root.iter():
            if _strip_ns(para.tag) != "p":
                continue
            runs = [(n.text or "") for n in para.iter()
                    if _strip_ns(n.tag) == "t"]
            line = "".join(runs).strip()
            if line:
                out.append(line)
    return "\n".join(out)


def _xlsx_rows(path: Path) -> list:
    """XLSX is a zip of XML. Cells with t="s" index into sharedStrings."""
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root:
                shared.append("".join(t.text or "" for t in si.iter()
                                      if _strip_ns(t.tag) == "t"))
        sheets = [n for n in z.namelist()
                  if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
        if not sheets:
            raise ValueError("no worksheet — not an Excel file")
        rows = []
        root = ET.fromstring(z.read(sorted(sheets)[0]))
        for row in root.iter():
            if _strip_ns(row.tag) != "row":
                continue
            cells = []
            for c in row:
                if _strip_ns(c.tag) != "c":
                    continue
                v = "".join(x.text or "" for x in c.iter()
                            if _strip_ns(x.tag) == "v")
                if c.get("t") == "s" and v.isdigit() and int(v) < len(shared):
                    v = shared[int(v)]
                cells.append(v)
            if any(cells):
                rows.append(cells)
    return rows


def read(path_str: str) -> Document:
    """Read a document, or say precisely why it could not be read."""
    p = Path(path_str).expanduser()
    d = Document(path=str(p), kind=p.suffix.lower() or "(no suffix)")

    if not p.exists():
        d.error = f"no such file: {p}"
        return d
    if p.is_dir():
        d.error = f"{p} is a directory. Name one file"
        return d
    size = p.stat().st_size
    if size > MAX_BYTES:
        d.error = (f"{size:,} bytes exceeds the {MAX_BYTES:,} byte limit. This "
                   f"reads research notes, not datasets — for a dataset, load "
                   f"it into the store and query it")
        return d
    suf = p.suffix.lower()
    if suf in REFUSED:
        d.error = REFUSED[suf]
        return d

    try:
        if suf == ".docx":
            d.text = _docx_text(p)
        elif suf == ".xlsx":
            d.tables = _xlsx_rows(p)
            d.text = "\n".join(" | ".join(r) for r in d.tables)
        elif suf in (".csv", ".tsv"):
            raw = p.read_text(encoding="utf-8", errors="replace")
            delim = "\t" if suf == ".tsv" else ","
            d.tables = [r for r in csv.reader(io.StringIO(raw),
                                              delimiter=delim) if any(r)]
            d.text = "\n".join(" | ".join(r) for r in d.tables)
        elif suf == ".json":
            obj = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            d.text = json.dumps(obj, indent=2, default=str)
        elif suf in TEXT_SUFFIXES or suf == "":
            d.text = p.read_text(encoding="utf-8", errors="replace")
        else:
            # Unknown suffix: try text, and say that is what happened.
            d.text = p.read_text(encoding="utf-8", errors="replace")
            d.note = (f"'{suf}' is not a format this reader knows; it was read "
                      f"as plain text. Check the extraction below before "
                      f"trusting it")
    except Exception as e:                                    # noqa: BLE001
        d.error = f"{type(e).__name__}: {e}"
        return d

    if len(d.text) > MAX_CHARS:
        d.text = d.text[:MAX_CHARS]
        d.truncated = True
    d.chars = len(d.text)
    d.words = len(d.text.split())
    d.ok = bool(d.text.strip())
    if not d.ok and not d.error:
        d.error = "the file was read but contains no extractable text"
    return d


# ------------------------------------------------------------------- extract

def _kind_of(sentence: str) -> str:
    for kind, pat in KIND_PATTERNS:
        if re.search(pat, sentence, re.I):
            return kind
    return ""


def _direction_of(sentence: str) -> str:
    hits = [name for name, pat in DIRECTION if re.search(pat, sentence, re.I)]
    return hits[0] if len(hits) == 1 else ""


def map_to_features(sentence: str) -> tuple[list, list, list]:
    """(concepts, features, blocked_by) for one sentence."""
    concepts, features, blocked = [], [], []
    for name, pat, feats in CONCEPT_FEATURES:
        if re.search(pat, sentence, re.I):
            concepts.append(name)
            features.extend(f for f in feats if f not in features)
    for name, pat, why in UNAVAILABLE_CONCEPTS:
        if re.search(pat, sentence, re.I):
            blocked.append({"concept": name, "why": why})
    return concepts, features, blocked


def extract(d: Document, *, max_claims: int = 60) -> Document:
    """Sentences -> classified claims -> candidates in the engine's vocabulary."""
    if not d.ok:
        return d

    seen: set = set()
    for raw in _SENT.split(d.text):
        s = " ".join(raw.split())
        if len(s) < 25 or len(s) > 400:
            continue
        key = s.lower()[:120]
        if key in seen:
            continue
        kind = _kind_of(s)
        if not kind:
            continue
        seen.add(key)
        concepts, features, blocked = map_to_features(s)
        c = Claim(text=s, kind=kind, concepts=concepts, features=features,
                  blocked_by=blocked, direction=_direction_of(s),
                  quantified=bool(_NUMERIC.search(s)))
        c.testable = bool(features) and kind in ("CLAIM", "ASSUMPTION")
        if c.testable and blocked:
            # The dangerous case: "order-book imbalance predicts price
            # movement" maps onto price columns through the word "price" while
            # its actual condition — imbalance — has no column at all. Testing
            # the mapped half and calling the claim tested would answer a
            # different question, so the split is stated on the claim itself.
            c.caveat = (
                "partially testable only: this also rests on "
                + ", ".join(b["concept"] for b in blocked)
                + ", which has no observation column. The candidates below "
                  "test the part that maps and say nothing about the rest")
        d.claims.append(c)
        if len(d.claims) >= max_claims:
            break

    # Candidates, in the engine's own words. Deliberately NOT run: they enter
    # `pqv3 discover` and clear the same bar as anything else (§29).
    props: dict = {}
    for c in d.claims:
        if not c.testable:
            continue
        for f in c.features:
            ops = (c.direction,) if c.direction else ("ge", "le")
            for op in ops:
                sym = ">=" if op == "ge" else "<="
                key = f"{f}:{op}"
                if key in props:
                    continue
                props[key] = {
                    "feature": f, "op": op,
                    "statement": f"buy when {f} {sym} <threshold>",
                    "from_claim_text": c.text[:160],
                    "threshold": "swept over the standard quantile grid "
                                 "(0.20 / 0.35 / 0.50 / 0.65 / 0.80) — the "
                                 "document's own number is not adopted, "
                                 "because a threshold taken from prose has no "
                                 "denominator behind it",
                    "direction_source": ("stated in the document"
                                         if c.direction else
                                         "unstated; both directions are "
                                         "tested, and both count against the "
                                         "multiple-comparison denominator"),
                }
    d.proposals = list(props.values())

    blocked: dict = {}
    for c in d.claims:
        for b in c.blocked_by:
            blocked.setdefault(b["concept"], {**b, "from_claim_text": []})
            blocked[b["concept"]]["from_claim_text"].append(c.text[:120])
    d.missing_data = list(blocked.values())

    kinds: dict = {}
    for c in d.claims:
        kinds[c.kind] = kinds.get(c.kind, 0) + 1
    testable = sum(1 for c in d.claims if c.testable)

    d.next_steps = []
    if d.proposals:
        d.next_steps.append(
            f"{len(d.proposals)} candidate(s) map onto live observation "
            f"columns. Run `pqv3 discover` — they are generated by the same "
            f"sweep that produces every other hypothesis, over the same "
            f"quantile grid, and are judged against the same BH threshold. "
            f"Nothing is adopted because the document says so.")
    if d.missing_data:
        d.next_steps.append(
            f"{len(d.missing_data)} concept(s) in this document have no "
            f"observation column. Those are data requirements, not "
            f"hypotheses — testing them against a proxy would answer a "
            f"different question and report it as this one.")
    if not d.proposals and not d.missing_data:
        d.next_steps.append(
            "nothing in this document maps onto the engine's vocabulary. That "
            "is a legitimate outcome (§33) — it may be a good document about "
            "something this system does not observe.")
    d.note = (f"{len(d.claims)} classified statement(s): "
              + ", ".join(f"{k.lower()} {v}" for k, v in sorted(kinds.items()))
              + f". {testable} map to testable columns.")
    return d


def ingest(path: str) -> Document:
    return extract(read(path))
