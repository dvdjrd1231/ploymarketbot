"""Module 4 - Feature Engineering (engine-side research features).

Turns the live state of the Bid engine, the Ask engine and the 100 line-break
engines into ONE research row. Every feature named in the brief is produced here
and every one carries a written definition (FEATURE_DOCS), which is exported
alongside the data - "all feature calculations should be documented and
exported".

This is the UPSTREAM feature layer (structure -> research row). The downstream
`core/features.py` then derives its cross-sectional / rolling families from
these rows inside the LEAN bridge. Two layers, one contract: a CSV of named
numeric columns keyed by timestamp.

Everything is incremental: one snapshot costs O(#engines), no history scan.
"""
from __future__ import annotations

from dataclasses import dataclass

from .events import FeatureSnapshot
from .logging_setup import get_logger

log = get_logger("features")


# --------------------------------------------------------------------------
# The 14 features the brief names, plus the supporting raw state they build on.
# Keys must match exactly what compute() emits.
# --------------------------------------------------------------------------
FEATURE_DOCS: dict[str, str] = {
    # ---- the 14 required research features
    "structural_velocity":
        "Mean signed speed of the Bid and Ask quote ladders, in price levels "
        "(ticks) per second, EMA-smoothed. Positive = structure climbing.",
    "structural_acceleration":
        "Rate of change of structural_velocity (ticks/second^2), EMA-smoothed. "
        "Positive = the structural move is still gaining speed.",
    "void_size":
        "Mean size, in ticks, of the currently OPEN liquidity voids across both "
        "sides. A void's size is the structural gap that created it.",
    "void_persistence":
        "Mean age in seconds of currently open liquidity voids. High = price is "
        "leaving unfilled structure behind it and not coming back.",
    "reversal_frequency":
        "Mean EMA reversal rate across all 100 line-break engines: the fraction "
        "of recent lines that flipped direction. High = chop, low = trend.",
    "persistence_length":
        "Mean run length (consecutive same-direction lines) across the 100 "
        "engines. The raw structural staying-power of the current move.",
    "compression":
        "Ratio of ticks spent forming the CURRENT line to each engine's own "
        "long-run ticks-per-line, averaged. >1 = price is grinding, structure "
        "is compressing.",
    "expansion":
        "Reciprocal of compression. >1 = lines are printing faster than normal, "
        "i.e. structural expansion / impulse.",
    "resolution_agreement":
        "How lopsided the directional vote is across the 100 resolutions: "
        "|2*up_fraction - 1|. 1.0 = all 100 engines agree, 0.0 = a 50/50 split.",
    "resolution_divergence":
        "1 - resolution_agreement. High = the fast and slow structural "
        "resolutions disagree, which typically brackets a turning point.",
    "structural_volatility":
        "Standard deviation of engine direction across the 100 resolutions, "
        "blended with quote-ladder velocity dispersion. Dispersion of structure "
        "itself, not of price.",
    "trend_persistence":
        "persistence_length normalized by (1 + reversal_frequency): long runs "
        "with few reversals score high. The trend-quality feature.",
    "liquidity_imbalance":
        "(bid_size - ask_size) / (bid_size + ask_size) at the touch, blended "
        "with the open-void imbalance between the two sides. "
        "Positive = bid-heavy book / more voids left above.",
    "directional_consensus":
        "Mean signed direction (-1..+1) across all 100 engines. The single "
        "number that says which way the whole structural stack is leaning.",

    # ---- supporting structure (also exported; the discovery search may use them)
    "price": "Last traded price at the moment of the snapshot.",
    "spread_ticks": "Ask minus bid, in ticks, at the snapshot.",
    "bid_velocity": "Bid-ladder velocity (ticks/sec, EMA).",
    "ask_velocity": "Ask-ladder velocity (ticks/sec, EMA).",
    "bid_acceleration": "Bid-ladder acceleration (ticks/sec^2, EMA).",
    "ask_acceleration": "Ask-ladder acceleration (ticks/sec^2, EMA).",
    "bid_void_open": "Count of currently open liquidity voids on the bid side.",
    "ask_void_open": "Count of currently open liquidity voids on the ask side.",
    "bid_void_persistence": "Mean age (s) of open bid-side voids.",
    "ask_void_persistence": "Mean age (s) of open ask-side voids.",
    "bid_void_size": "Mean size (ticks) of open bid-side voids.",
    "ask_void_size": "Mean size (ticks) of open ask-side voids.",
    "bid_nonprint_count": "Cumulative bid-side non-print events.",
    "ask_nonprint_count": "Cumulative ask-side non-print events.",
    "bid_nonprint_rate": "Bid-side non-print events per second (EMA).",
    "ask_nonprint_rate": "Ask-side non-print events per second (EMA).",
    "bid_gap_size": "EMA size (ticks) of bid-side structural gaps.",
    "ask_gap_size": "EMA size (ticks) of ask-side structural gaps.",
    "bid_skipped_prices": "Cumulative bid-side skipped price levels.",
    "ask_skipped_prices": "Cumulative ask-side skipped price levels.",
    "bid_structural_gaps": "Cumulative bid-side structural gaps.",
    "ask_structural_gaps": "Cumulative ask-side structural gaps.",
    "void_imbalance":
        "(bid_voids - ask_voids) / (bid_voids + ask_voids). Which side of the "
        "book is being left unfilled.",
    "nonprint_imbalance":
        "(bid_nonprints - ask_nonprints) / (bid_nonprints + ask_nonprints), on "
        "the event RATES, so it reflects now rather than the whole session.",
    "res_consensus": "Raw mean direction across the 100 engines (= directional_consensus).",
    "res_up_fraction": "Fraction of the 100 engines currently pointing up.",
    "res_lines_total": "Total lines printed across all engines (activity proxy).",
    "quote_size_imbalance": "Touch-size imbalance only, without the void blend.",
}


def _safe_ratio(a: float, b: float) -> float:
    """(a-b)/(a+b) with a zero-safe denominator; result in [-1, 1]."""
    total = a + b
    if total <= 1e-12:
        return 0.0
    return (a - b) / total


@dataclass
class FeatureConfig:
    tick_size: float = 0.25

    @classmethod
    def from_config(cls, cfg) -> "FeatureConfig":
        return cls(tick_size=float(cfg.get("instrument.tick_size", 0.25)))


class StructureFeatureBuilder:
    """Combines the engines' snapshots into one documented research row."""

    def __init__(self, cfg: FeatureConfig):
        self.cfg = cfg
        self.tick = cfg.tick_size
        self.rows = 0

    def compute(self, ts: float, price: float, nonprint_snap: dict[str, float],
                lb_snap: dict[str, float],
                bid_size: float = 0.0, ask_size: float = 0.0) -> FeatureSnapshot:
        g = nonprint_snap.get
        r = lb_snap.get

        bid_vel = g("bid_velocity", 0.0)
        ask_vel = g("ask_velocity", 0.0)
        bid_acc = g("bid_acceleration", 0.0)
        ask_acc = g("ask_acceleration", 0.0)

        bid_voids = g("bid_void_open", 0.0)
        ask_voids = g("ask_void_open", 0.0)
        bid_vsize = g("bid_void_size", 0.0)
        ask_vsize = g("ask_void_size", 0.0)
        bid_vpers = g("bid_void_persistence", 0.0)
        ask_vpers = g("ask_void_persistence", 0.0)

        bid_quote = g("bid_quote", 0.0)
        ask_quote = g("ask_quote", 0.0)
        bsz = bid_size or g("bid_quote_size", 0.0)
        asz = ask_size or g("ask_quote_size", 0.0)

        # ---- the 14 named features ----------------------------------------
        structural_velocity = 0.5 * (bid_vel + ask_vel)
        structural_acceleration = 0.5 * (bid_acc + ask_acc)

        open_voids = bid_voids + ask_voids
        void_size = ((bid_vsize * bid_voids + ask_vsize * ask_voids) / open_voids
                     if open_voids > 0 else 0.0)
        void_persistence = ((bid_vpers * bid_voids + ask_vpers * ask_voids) / open_voids
                            if open_voids > 0 else 0.0)

        reversal_frequency = r("res_reversal_frequency", 0.0)
        persistence_length = r("res_trend_persistence", 0.0)
        compression = r("res_compression", 1.0)
        expansion = r("res_expansion", 1.0)
        resolution_agreement = r("res_agreement", 0.0)
        resolution_divergence = r("res_divergence", 1.0)
        directional_consensus = r("res_consensus", 0.0)

        # Structure-of-structure volatility: how much the resolutions disagree,
        # blended with how differently the two ladders are moving.
        ladder_dispersion = abs(bid_vel - ask_vel)
        structural_volatility = (r("res_structural_volatility", 0.0)
                                 + 0.1 * min(ladder_dispersion, 10.0))

        trend_persistence = persistence_length / (1.0 + reversal_frequency)

        quote_size_imbalance = _safe_ratio(bsz, asz)
        void_imbalance = _safe_ratio(bid_voids, ask_voids)
        liquidity_imbalance = 0.5 * quote_size_imbalance + 0.5 * void_imbalance

        nonprint_imbalance = _safe_ratio(g("bid_nonprint_rate", 0.0),
                                         g("ask_nonprint_rate", 0.0))

        spread_ticks = ((ask_quote - bid_quote) / self.tick
                        if (ask_quote > 0 and bid_quote > 0) else 0.0)

        values: dict[str, float] = {
            "price": price,
            "spread_ticks": spread_ticks,

            "structural_velocity": structural_velocity,
            "structural_acceleration": structural_acceleration,
            "void_size": void_size,
            "void_persistence": void_persistence,
            "reversal_frequency": reversal_frequency,
            "persistence_length": persistence_length,
            "compression": compression,
            "expansion": expansion,
            "resolution_agreement": resolution_agreement,
            "resolution_divergence": resolution_divergence,
            "structural_volatility": structural_volatility,
            "trend_persistence": trend_persistence,
            "liquidity_imbalance": liquidity_imbalance,
            "directional_consensus": directional_consensus,

            "void_imbalance": void_imbalance,
            "nonprint_imbalance": nonprint_imbalance,
            "quote_size_imbalance": quote_size_imbalance,
        }

        # Raw per-side structure + cross-resolution aggregates ride along so the
        # discovery search can find edges we did not anticipate.
        for k, v in nonprint_snap.items():
            values.setdefault(k, v)
        for k, v in lb_snap.items():
            values.setdefault(k, v)

        self.rows += 1
        return FeatureSnapshot(ts=ts, price=price, values=values)

    # ------------------------------------------------------------------ docs
    @staticmethod
    def documentation() -> list[dict[str, str]]:
        return [{"feature": k, "definition": v} for k, v in FEATURE_DOCS.items()]

    @staticmethod
    def write_docs(path) -> None:
        """Export the feature dictionary as Markdown next to the dataset."""
        from pathlib import Path
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Research Feature Dictionary",
            "",
            "Auto-generated by `core/structure_features.py`. Every column in "
            "`structure_features.csv` is defined here.",
            "",
            "## The 14 required research features",
            "",
            "| Feature | Definition |",
            "| --- | --- |",
        ]
        required = [
            "structural_velocity", "structural_acceleration", "void_size",
            "void_persistence", "reversal_frequency", "persistence_length",
            "compression", "expansion", "resolution_agreement",
            "resolution_divergence", "structural_volatility", "trend_persistence",
            "liquidity_imbalance", "directional_consensus",
        ]
        for k in required:
            lines.append(f"| `{k}` | {FEATURE_DOCS[k]} |")

        lines += ["", "## Supporting structural columns", "",
                  "| Column | Definition |", "| --- | --- |"]
        for k, v in FEATURE_DOCS.items():
            if k not in required:
                lines.append(f"| `{k}` | {v} |")

        lines += [
            "",
            "## Per-resolution columns (`multi_resolution_linebreak.csv`)",
            "",
            "| Column | Definition |",
            "| --- | --- |",
            "| `lb_res{R}_dir` | Direction of the R-tick line-break engine (+1 up, -1 down). |",
            "| `lb_res{R}_run` | Consecutive same-direction lines at resolution R. |",
            "| `lb_res{R}_close` | Current line close (structural level) at resolution R. |",
            "| `lb_res{R}_rev_rate` | EMA reversal rate at resolution R. |",
            "| `lb_res{R}_ticks_in_line` | Ticks consumed by the line currently forming at R. |",
            "",
        ]
        p.write_text("\n".join(lines), encoding="utf-8")
        log.info("feature documentation written", extra={"path": str(p)})
