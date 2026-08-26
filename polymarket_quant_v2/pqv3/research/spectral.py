"""§11 — periodicity and quasi-periodicity, on unevenly sampled data.

Prediction-market tape is not a time series in the textbook sense. Prints
arrive when somebody trades: dense during an event, absent for eleven hours
overnight, dense again. An FFT requires equal spacing, so the usual move is to
resample onto a grid — and resampling invents observations at the exact
timestamps where none existed, then reports the sampling grid's own periodicity
back as a finding. An overnight gap resampled at 5-minute intervals produces a
beautiful 24-hour cycle that is a property of the calendar, not of the market.

So this uses the Lomb-Scargle periodogram, which is a least-squares fit of a
sinusoid at each trial frequency directly to the observations at the times they
actually occurred. Nothing is interpolated and no observation is invented.

THE MULTIPLICITY TRAP is the other half, and it is worse here than anywhere
else in the codebase. Scanning 400 frequencies and reporting the tallest peak
gives a spectacular result on pure noise EVERY TIME — the expected maximum of
400 draws from the null is far out in the tail of any single one of them. The
standard analytic false-alarm formulae assume even sampling and Gaussian noise
and are wrong on both counts here.

The fix is to make the surrogate null carry the same search: the statistic is
the HEIGHT OF THE TALLEST PEAK ANYWHERE IN THE SCAN, and the null is the
distribution of that same tallest peak over surrogate series. The search is
then priced in exactly, by construction, with no distributional assumption at
all. Surrogates shuffle the values while KEEPING THE ORIGINAL TIMESTAMPS, so
the null has the identical — and identically irregular — sampling pattern, and
any periodicity the sampling itself imposes appears in the null too and cancels.

WHAT THE SURROGATE NULL DOES NOT FIX, because it cannot: ALIASING. A rhythm the
sampling contributes by itself is cancelled by the null. An alias is different
— it is the product of the real signal and the sampling window, so it exists in
the data and not in the null, and it passes the significance test honestly
while sitting at the wrong period. Measured here, with a true 12-hour cycle
sampled sixteen hours a day: the tallest peak was 8.01 h and the second 23.93 h,
because 1/8 = 1/12 + 1/24 exactly. The true period was present, and third.

So the arrival rhythm gets its own periodogram (`sampling_spectrum`), every
peak is checked against it, and a winner that reproduces another peak folded
against the sampling clock is reported as PERIODICITY_ALIASED — a cycle is
there, but this sampling cannot say which one it is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .surrogate import Rng, shuffle, test


@dataclass
class Periodicity:
    n: int
    span_secs: float = 0.0
    best_period_secs: float = 0.0
    best_power: float = 0.0
    surrogate: dict = field(default_factory=dict)
    peaks: list = field(default_factory=list)
    sampling_peaks: list = field(default_factory=list)
    verdict: str = "NO_PERIODICITY_FOUND"
    warnings: list = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @property
    def best_period_hours(self) -> float:
        return round(self.best_period_secs / 3600.0, 4)


def lomb_scargle(times: list, values: list, freqs: list) -> list:
    """Lomb-Scargle power at each frequency, classical normalisation.

    Power is the sinusoid's fitted amplitude scaled by the series variance, and
    it is NOT bounded by 1 — a strong cycle in n samples reaches order n/2, and
    the measured values here run from about 4 (noise) to about 80 (a clean
    embedded cycle). The absolute height is therefore uninterpretable on its
    own and is never reported without the surrogate distribution beside it,
    which is the same discipline every estimator in this batch follows. `tau` is
    the per-frequency time offset that makes the sine and cosine components
    orthogonal over the actual sample times — it is what makes this valid for
    uneven sampling and is the whole difference from a naive periodogram.
    """
    n = len(values)
    if n < 4:
        return [0.0] * len(freqs)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    if var <= 0:
        return [0.0] * len(freqs)

    out = []
    for f in freqs:
        w = 2.0 * math.pi * f
        s2 = c2 = 0.0
        for t in times:
            s2 += math.sin(2.0 * w * t)
            c2 += math.cos(2.0 * w * t)
        tau = math.atan2(s2, c2) / (2.0 * w) if (s2 or c2) else 0.0

        cc = ss = cs_num = sn_num = 0.0
        for t, v in zip(times, values):
            ct = math.cos(w * (t - tau))
            st = math.sin(w * (t - tau))
            d = v - mean
            cs_num += d * ct
            sn_num += d * st
            cc += ct * ct
            ss += st * st
        p = 0.0
        if cc > 1e-12:
            p += (cs_num * cs_num) / cc
        if ss > 1e-12:
            p += (sn_num * sn_num) / ss
        out.append(p / (2.0 * var))
    return out


def frequency_grid(times: list, *, min_period_secs: float = 0.0,
                   max_period_secs: float = 0.0, n_freq: int = 300) -> list:
    """Trial frequencies, bounded by what the data can actually resolve.

    The long end is capped at half the observed span: a "cycle" longer than two
    repetitions is a trend that has been fitted with a sinusoid, and reporting
    it as a period is how a monotone drift becomes a 90-day cycle. The short
    end is capped at twice the median sampling interval — the Nyquist limit for
    irregular data — because below it a fitted sinusoid is aliasing the gaps.
    """
    if len(times) < 4:
        return []
    span = max(times) - min(times)
    if span <= 0:
        return []
    gaps = sorted(times[i + 1] - times[i] for i in range(len(times) - 1))
    median_gap = gaps[len(gaps) // 2] or 1.0
    lo = min_period_secs or max(2.0 * median_gap, span / len(times))
    hi = max_period_secs or (span / 2.0)
    if hi <= lo:
        return []
    # Log-spaced: periodicity is scale-free, and a linear grid in frequency
    # spends most of its points on periods too short to mean anything.
    step = (math.log(hi) - math.log(lo)) / max(1, n_freq - 1)
    return [1.0 / math.exp(math.log(lo) + i * step) for i in range(n_freq)]


def sampling_spectrum(times: list, *, n_freq: int = 200,
                      bin_secs: float = 0.0) -> list:
    """The spectrum of WHEN observations arrive, ignoring their values.

    Necessary because the surrogate null does not remove aliasing, and cannot.
    Shuffling values over fixed timestamps prices in any rhythm the sampling
    contributes BY ITSELF, but an alias is a product of the real signal and the
    sampling window, so it is present in the data and absent from the null — it
    passes the significance test legitimately and lands at the wrong period.

    Measured, with a true 12-hour cycle sampled 16 hours a day: the two tallest
    peaks were 8.01 h and 23.93 h, and 1/8 = 1/12 + 1/24 exactly. The true
    period was present but third. A reader shown only the tallest peak would
    have been told 8 hours with a good p-value and no warning.

    So the arrival rhythm gets its own periodogram — counts per bin against bin
    centres — and its peaks mark the frequencies against which any finding may
    have been folded.
    """
    if len(times) < 20:
        return []
    span = max(times) - min(times)
    if span <= 0:
        return []
    b = bin_secs or max(600.0, span / 400.0)
    t0 = min(times)
    n_bins = max(8, int(span / b) + 1)
    counts = [0.0] * n_bins
    for t in times:
        counts[min(n_bins - 1, int((t - t0) / b))] += 1.0
    centres = [(i + 0.5) * b for i in range(n_bins)]
    freqs = frequency_grid(centres, n_freq=n_freq)
    if not freqs:
        return []
    power = lomb_scargle(centres, counts, freqs)
    ranked = sorted(range(len(power)), key=lambda i: -power[i])[:4]
    return [{"period_secs": round(1.0 / freqs[i], 2),
             "period_hours": round(1.0 / freqs[i] / 3600.0, 4),
             "power": round(power[i], 4)} for i in ranked]


def _alias_warnings(best_secs: float, sampling: list, peaks: list) -> list:
    """Is the tallest peak an alias of a real cycle against the arrival rhythm?

    For a sampling rhythm S and a true period P, the folded frequencies are
    1/P +/- 1/S. If another observed peak sits at a period that reproduces the
    winner under that arithmetic, the two are indistinguishable from this data
    and neither may be named as the period.
    """
    out = []
    if not best_secs or not sampling:
        return out
    f_best = 1.0 / best_secs
    for s in sampling[:2]:
        f_s = 1.0 / s["period_secs"] if s["period_secs"] else 0.0
        if f_s <= 0:
            continue
        for p in peaks:
            if abs(p["period_secs"] - best_secs) < 1e-6:
                continue
            f_p = 1.0 / p["period_secs"]
            for sign in (1.0, -1.0):
                if abs(f_best - (f_p + sign * f_s)) < 0.06 * f_best:
                    out.append(
                        f"the {best_secs / 3600:.2f} h peak is an exact alias "
                        f"of the {p['period_hours']:.2f} h peak folded against "
                        f"the {s['period_hours']:.2f} h arrival rhythm "
                        f"(1/{best_secs / 3600:.2f} = 1/{p['period_hours']:.2f}"
                        f" {'+' if sign > 0 else '-'} "
                        f"1/{s['period_hours']:.2f}). The two are "
                        f"indistinguishable from this sampling, so neither may "
                        f"be named as THE period")
                    return out
    return out


def analyse(times: list, values: list, *, draws: int = 200,
            seed: int = 20260825, alpha: float = 0.05, n_freq: int = 200,
            min_n: int = 60, top: int = 5) -> Periodicity:
    """Is there a repeating cycle in this series, beyond what the search finds
    in noise?

    Surrogates shuffle the VALUES and keep the TIMES. That pairing is the point:
    the null series is sampled at exactly the same irregular instants, so any
    rhythm contributed by the sampling pattern — an overnight gap, a burst
    around an event — appears in the null at the same strength and cancels out
    of the comparison.
    """
    n = min(len(times), len(values))
    rep = Periodicity(n=n)
    if n < min_n:
        rep.warnings.append(
            f"n={n} is below the {min_n}-observation floor. The tallest peak "
            f"of a short irregular series is noise regardless of its height")
        rep.note = "INSUFFICIENT_EVIDENCE — §33"
        return rep

    times, values = list(times[:n]), list(values[:n])

    # A constant series has zero variance, so every Lomb-Scargle power is
    # exactly 0 and the verdict comes back NO_PERIODICITY_FOUND — which reads
    # as "we looked and there is no cycle" when the truth is "there was nothing
    # to look at". Real tape does this: one token in this database carries
    # 10,690 prints at a single price. §41 — that distinction gets its own
    # verdict rather than being folded into a negative result.
    distinct = len(set(values))
    if distinct < 3:
        rep.verdict = "DEGENERATE_SERIES"
        rep.warnings.append(
            f"the series takes {distinct} distinct value(s) across {n} "
            f"observations. There is no variance to decompose")
        rep.note = ("not a negative result. A constant series has no spectrum, "
                    "and reporting 'no periodicity' here would claim a "
                    "measurement that was never possible")
        return rep

    t0 = min(times)
    times = [float(t - t0) for t in times]
    rep.span_secs = max(times)

    freqs = frequency_grid(times, n_freq=n_freq)
    if not freqs:
        rep.warnings.append("no resolvable frequency band: the span is too "
                            "short relative to the sampling interval")
        return rep

    power = lomb_scargle(times, values, freqs)
    best_i = max(range(len(power)), key=lambda i: power[i])
    rep.best_power = round(power[best_i], 6)
    rep.best_period_secs = round(1.0 / freqs[best_i], 3)

    ranked = sorted(range(len(power)), key=lambda i: -power[i])[:top]
    rep.peaks = [{"period_secs": round(1.0 / freqs[i], 2),
                  "period_hours": round(1.0 / freqs[i] / 3600.0, 4),
                  "power": round(power[i], 6)} for i in ranked]

    rng = Rng(seed)
    null_max = []
    for _ in range(draws):
        null_max.append(max(lomb_scargle(times, shuffle(values, rng), freqs)))
    t = test(rep.best_power, null_max, null="shuffle(values), times kept",
             n=n, alpha=alpha)
    rep.surrogate = t.to_dict()
    rep.surrogate["statistic_is"] = (
        f"height of the tallest peak anywhere in a {len(freqs)}-frequency "
        f"scan — the search is priced into the null by construction")

    rep.sampling_peaks = sampling_spectrum(times, n_freq=n_freq)
    aliases = _alias_warnings(rep.best_period_secs, rep.sampling_peaks,
                              rep.peaks)
    rep.warnings.extend(aliases)

    if t.significant:
        rep.verdict = "PERIODICITY_ALIASED" if aliases else "PERIODICITY_FOUND"
        rep.note = (
            ("THE TALLEST PEAK IS AN ALIAS — see warnings; the true period is "
             "among the peaks below but cannot be picked out from this "
             "sampling. " if aliases else "")
            + f"tallest peak {rep.best_power:.4f} at a period of "
            f"{rep.best_period_hours:.2f} h, against a null whose tallest peak "
            f"over the same scan averages {t.null_mean:.4f} (p={t.p_value:.4f}). "
            f"A cycle in the tape is not a tradable cycle: it says when prints "
            f"cluster, not that a price is predictable, and any strategy "
            f"conditioning on it still has to clear `pqv3 discover`")
    else:
        rep.verdict = "NO_PERIODICITY_FOUND"
        rep.note = (
            f"the tallest peak is {rep.best_power:.4f}; shuffling the values "
            f"over the same timestamps produces a tallest peak averaging "
            f"{t.null_mean:.4f}. Scanning {len(freqs)} frequencies finds a "
            f"good one in anything. §33: nothing here.")
    return rep
