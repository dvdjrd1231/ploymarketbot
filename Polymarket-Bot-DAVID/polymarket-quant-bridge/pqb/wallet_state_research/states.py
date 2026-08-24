"""The wallet position-state machine (§11, §25, §42).

The handoff's central claim is that the interesting object is not a BUY but a
TRANSITION:

    initial exposure -> inventory state -> opposite-side transition
                     -> position-management mode -> settlement

So a condition is not reduced to one label. It is replayed as a trajectory
with a timestamp on every transition, which makes two questions answerable
that a static label cannot answer:

    "what state was this wallet apparently in at time T?"
    "what predicts which state it enters NEXT?"

The states are observational, not claims about the wallet's intent. A wallet
in ACCUMULATING_ORIGINAL is one whose observed behaviour has been repeated
same-side buying; whether it "intends" to accumulate is unknowable and is not
asserted anywhere.

One rule governs the whole file: a state at time T is computed from events at
or before T. `trajectory()` therefore replays forward and never looks at the
final label, which is why the terminal state is derived from the same
labelling function the rest of the package uses rather than being decided here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .episodes import AGGRESSIVE, DIRECTIONAL, PROTECT, Episode

STATE_0_NO_POSITION = "STATE_0_NO_POSITION"
STATE_1_INITIAL_ONE_SIDED = "STATE_1_INITIAL_ONE_SIDED"
STATE_2_ACCUMULATING_ORIGINAL = "STATE_2_ACCUMULATING_ORIGINAL"
STATE_3_QUIET_ONE_SIDED = "STATE_3_QUIET_ONE_SIDED"
STATE_4_OPPOSITE_TRANSITION = "STATE_4_OPPOSITE_TRANSITION"
STATE_5_TWO_SIDED_PROTECT = "STATE_5_TWO_SIDED_PROTECT_REBALANCE"
STATE_6_TWO_SIDED_AGGRESSIVE = "STATE_6_TWO_SIDED_AGGRESSIVE_OPPOSITE"
STATE_7_SETTLEMENT = "STATE_7_SETTLEMENT"

STATES = (STATE_0_NO_POSITION, STATE_1_INITIAL_ONE_SIDED,
          STATE_2_ACCUMULATING_ORIGINAL, STATE_3_QUIET_ONE_SIDED,
          STATE_4_OPPOSITE_TRANSITION, STATE_5_TWO_SIDED_PROTECT,
          STATE_6_TWO_SIDED_AGGRESSIVE, STATE_7_SETTLEMENT)

# How long a one-sided position must go without a same-side add before it is
# described as QUIET rather than ACCUMULATING. A descriptive threshold, not a
# tuned one: it separates "still building" from "built and waiting", and the
# transition study reports its sensitivity rather than relying on the value.
QUIET_AFTER_SECONDS = 600.0

# The live two-sided split uses the same 1.40 research boundary as the final
# label, applied to the inventory AS IT STANDS. That is the point: it lets the
# trajectory say "this condition looked AGGRESSIVE at minute 4 and PROTECT at
# minute 40", which a single end-of-life label erases.
TWO_SIDED_BOUNDARY = 1.40


@dataclass
class Transition:
    """One state change, with the event that caused it."""

    ts: float
    from_state: str
    to_state: str
    trigger: str = ""
    original_shares: float = 0.0
    opposite_shares: float = 0.0
    seconds_since_first_buy: float = 0.0

    def to_dict(self) -> dict:
        return {"ts": self.ts, "from": self.from_state, "to": self.to_state,
                "trigger": self.trigger,
                "originalShares": round(self.original_shares, 6),
                "oppositeShares": round(self.opposite_shares, 6),
                "secondsSinceFirstBuy": round(self.seconds_since_first_buy, 1)}


@dataclass
class Trajectory:
    """One condition's whole observed path through the state machine."""

    wallet: str = ""
    market_id: str = ""
    transitions: list = field(default_factory=list)
    terminal_state: str = ""
    final_label: str = ""
    settled: bool = False

    def state_at(self, ts: float) -> str:
        """Which state the wallet was apparently in at `ts`.

        Walks the recorded transitions, which were themselves built forward
        from events at or before each transition's own timestamp — so this
        cannot return a state that depended on the future however late `ts` is.
        """
        state = STATE_0_NO_POSITION
        for transition in self.transitions:
            if transition.ts > ts:
                break
            state = transition.to_state
        return state

    def first_entry_into(self, state: str) -> Optional[float]:
        for transition in self.transitions:
            if transition.to_state == state:
                return transition.ts
        return None

    @property
    def reached_two_sided(self) -> bool:
        return any(t.to_state in (STATE_4_OPPOSITE_TRANSITION,
                                  STATE_5_TWO_SIDED_PROTECT,
                                  STATE_6_TWO_SIDED_AGGRESSIVE)
                   for t in self.transitions)

    def pairs(self) -> list:
        """`[(from_state, to_state, seconds_between), ...]` — the transition
        study's raw material."""
        out = []
        for index in range(len(self.transitions) - 1):
            a, b = self.transitions[index], self.transitions[index + 1]
            out.append((a.to_state, b.to_state, b.ts - a.ts))
        return out

    def to_dict(self) -> dict:
        return {"wallet": self.wallet, "market": self.market_id,
                "terminalState": self.terminal_state,
                "finalLabel": self.final_label, "settled": self.settled,
                "transitions": [t.to_dict() for t in self.transitions]}


def _two_sided_state(original: float, opposite: float) -> str:
    if original <= 0:
        return STATE_6_TWO_SIDED_AGGRESSIVE
    ratio = opposite / original
    return (STATE_6_TWO_SIDED_AGGRESSIVE if ratio >= TWO_SIDED_BOUNDARY
            else STATE_5_TWO_SIDED_PROTECT)


def trajectory(episode: Episode, settled: bool = False,
               quiet_after: float = QUIET_AFTER_SECONDS) -> Trajectory:
    """Replay one condition forward through the state machine.

    Forward, event by event, with net inventory (§6) rather than gross
    purchases. Every transition carries the inventory that produced it, so the
    trajectory is auditable against the tape without re-deriving anything.
    """
    out = Trajectory(wallet=episode.wallet, market_id=episode.market_id,
                     final_label=episode.label, settled=settled)
    state = STATE_0_NO_POSITION
    original = opposite = 0.0
    last_same_side_ts = 0.0
    same_side_buys = 0

    def _move(to_state: str, ts: float, trigger: str) -> None:
        nonlocal state
        if to_state == state:
            return
        out.transitions.append(Transition(
            ts=ts, from_state=state, to_state=to_state, trigger=trigger,
            original_shares=original, opposite_shares=opposite,
            seconds_since_first_buy=(ts - episode.first_buy_ts
                                     if episode.first_buy_ts else 0.0)))
        state = to_state

    for event in episode.events:
        is_original = event.token_id == episode.original_token
        if is_original:
            original += event.signed_shares
            if event.is_buy:
                same_side_buys += 1
                # A QUIET stretch is only knowable once it has ELAPSED, so it
                # is emitted at the moment the next event proves it happened —
                # never back-dated to the start of the silence, which would be
                # a state stamped before the information that established it.
                if (state in (STATE_1_INITIAL_ONE_SIDED,
                              STATE_2_ACCUMULATING_ORIGINAL)
                        and last_same_side_ts
                        and event.ts - last_same_side_ts >= quiet_after):
                    _move(STATE_3_QUIET_ONE_SIDED, event.ts,
                          f"no same-side activity for "
                          f"{event.ts - last_same_side_ts:.0f}s")
                last_same_side_ts = event.ts
                if same_side_buys == 1:
                    _move(STATE_1_INITIAL_ONE_SIDED, event.ts, "first BUY")
                elif state in (STATE_1_INITIAL_ONE_SIDED,
                               STATE_3_QUIET_ONE_SIDED):
                    _move(STATE_2_ACCUMULATING_ORIGINAL, event.ts,
                          "same-side add")
        else:
            opposite += event.signed_shares
            if event.is_buy and state in (
                    STATE_1_INITIAL_ONE_SIDED, STATE_2_ACCUMULATING_ORIGINAL,
                    STATE_3_QUIET_ONE_SIDED):
                _move(STATE_4_OPPOSITE_TRANSITION, event.ts,
                      "first opposite-side BUY")
            elif opposite > 0 and original > 0:
                _move(_two_sided_state(original, opposite), event.ts,
                      "two-sided inventory changed")
        # A two-sided position that is already past the transition keeps its
        # live classification current, so `state_at` answers honestly at any
        # instant rather than only at event boundaries.
        if state in (STATE_4_OPPOSITE_TRANSITION, STATE_5_TWO_SIDED_PROTECT,
                     STATE_6_TWO_SIDED_AGGRESSIVE) and opposite > 0 \
                and original > 0:
            _move(_two_sided_state(original, opposite), event.ts,
                  "inventory ratio crossed the research boundary")

    if settled and episode.last_activity_ts:
        _move(STATE_7_SETTLEMENT, episode.last_activity_ts, "settlement")
    out.terminal_state = state
    return out


# ---------------------------------------------------------------------------
# §25 / §42 — the transition study
# ---------------------------------------------------------------------------


@dataclass
class TransitionStudy:
    """Empirical transition structure across many conditions.

    A transition MATRIX, not a single label distribution. The question the
    handoff cares about is conditional — given the state now, what comes next
    — and a marginal distribution over final labels cannot answer it.
    """

    counts: dict = field(default_factory=dict)      # "from|to" -> n
    dwell: dict = field(default_factory=dict)       # "from|to" -> [seconds]
    terminal: dict = field(default_factory=dict)
    trajectories: int = 0
    reached_two_sided: int = 0

    def add(self, path: Trajectory) -> None:
        self.trajectories += 1
        if path.reached_two_sided:
            self.reached_two_sided += 1
        self.terminal[path.terminal_state] = \
            self.terminal.get(path.terminal_state, 0) + 1
        for from_state, to_state, seconds in path.pairs():
            key = f"{from_state}|{to_state}"
            self.counts[key] = self.counts.get(key, 0) + 1
            self.dwell.setdefault(key, []).append(seconds)

    def probabilities(self, min_observations: int = 20) -> dict:
        """`P(next | current)`, over rows with enough observations.

        A row seen three times gets no probabilities. A transition matrix
        estimated from three observations is three observations wearing a
        matrix's authority.
        """
        by_from: dict[str, dict] = {}
        for key, n in self.counts.items():
            from_state, to_state = key.split("|")
            by_from.setdefault(from_state, {})[to_state] = n
        out: dict = {}
        for from_state, row in by_from.items():
            total = sum(row.values())
            if total < min_observations:
                out[from_state] = {
                    "observations": total, "sufficient": False,
                    "reason": f"{total} observation(s), below the "
                              f"{min_observations} floor"}
                continue
            out[from_state] = {
                "observations": total, "sufficient": True,
                "next": {to: round(n / total, 4)
                         for to, n in sorted(row.items(),
                                             key=lambda kv: -kv[1])},
                "medianSecondsToNext": {
                    to: round(_median(self.dwell.get(
                        f"{from_state}|{to}", [])), 1)
                    for to in row},
            }
        return out

    def to_dict(self, min_observations: int = 20) -> dict:
        return {
            "trajectories": self.trajectories,
            "reachedTwoSided": self.reached_two_sided,
            "twoSidedShare": (round(self.reached_two_sided
                                    / self.trajectories, 4)
                              if self.trajectories else 0.0),
            "terminalStates": dict(sorted(self.terminal.items(),
                                          key=lambda kv: -kv[1])),
            "transitionCounts": dict(sorted(self.counts.items(),
                                            key=lambda kv: -kv[1])),
            "transitionProbabilities": self.probabilities(min_observations),
            "note": ("Observational states. A state describes what the tape "
                     "shows, never what the wallet intended, and the "
                     "three modes are not asserted to be three separate "
                     "systems (§7) — that is one of the things this study "
                     "exists to test."),
        }


def _median(values: list) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def study(episodes: Iterable[Episode], settled_markets: Optional[set] = None
          ) -> tuple:
    """Build every trajectory and the transition study over them."""
    settled_markets = settled_markets or set()
    out = TransitionStudy()
    paths = []
    for episode in episodes:
        path = trajectory(episode,
                          settled=episode.market_id in settled_markets)
        paths.append(path)
        out.add(path)
    return out, paths
