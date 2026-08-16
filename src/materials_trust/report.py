"""Statistics and plots.

Brief rule 4: report honestly. If the databases agree more than expected, that is
the finding. Nothing in this module tunes toward a dramatic result, and the
disagreement statistics are always reported with the count of comparisons behind
them so a reader can judge whether a number means anything.

Brief rule 5: explain every disagreement you can. The cross-source statistics are
therefore stratified by whether the documented Hubbard U policies of the two
databases differ for that composition, which turns "they differ by 0.2 eV" into
"they differ by 0.2 eV in exactly the cases where one applies +U and the other
does not".
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import matplotlib

matplotlib.use("Agg")  # no display on a build machine
import matplotlib.pyplot as plt  # noqa: E402

from . import checks, config  # noqa: E402
from .audit import ConfidenceBand, TrustRecord  # noqa: E402
from .checks import FlagCode  # noqa: E402
from .records import Property, Source  # noqa: E402
from .sources.experimental import GapComparison  # noqa: E402


def _describe(values: Sequence[float]) -> dict[str, Any]:
    """Summary statistics, or an explicit statement that there is no data."""
    vals = [float(v) for v in values]
    if not vals:
        return {"n": 0, "note": "no comparisons available"}
    abs_vals = [abs(v) for v in vals]
    out: dict[str, Any] = {
        "n": len(vals),
        "mean_signed": round(statistics.fmean(vals), 5),
        "median_signed": round(statistics.median(vals), 5),
        "mean_absolute": round(statistics.fmean(abs_vals), 5),
        "median_absolute": round(statistics.median(abs_vals), 5),
        "max_absolute": round(max(abs_vals), 5),
        "min_absolute": round(min(abs_vals), 5),
        "rmse": round((statistics.fmean([v * v for v in vals])) ** 0.5, 5),
    }
    if len(vals) > 1:
        out["std_signed"] = round(statistics.stdev(vals), 5)
    ordered = sorted(abs_vals)
    for pct in (50, 75, 90, 95, 99):
        idx = min(int(round(pct / 100 * (len(ordered) - 1))), len(ordered) - 1)
        out[f"abs_p{pct}"] = round(ordered[idx], 5)
    return out


# ---------------------------------------------------------------------------
# Cross-source agreement
# ---------------------------------------------------------------------------

@dataclass
class PairedDifference:
    formula: str
    property_name: Property
    mp_value: float
    oqmd_value: float
    signed_difference: float
    hubbard_mismatch: bool
    magnetic_mismatch: bool
    functional_mismatch: bool
    structure_fingerprint: str | None
    confidence: str
    flags: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "formula": self.formula,
            "property": self.property_name.value,
            "materials_project": round(self.mp_value, 6),
            "oqmd": round(self.oqmd_value, 6),
            "signed_difference_mp_minus_oqmd": round(self.signed_difference, 6),
            "hubbard_u_mismatch": self.hubbard_mismatch,
            "magnetic_mismatch": self.magnetic_mismatch,
            "functional_mismatch": self.functional_mismatch,
            "structure_fingerprint": self.structure_fingerprint,
            "confidence": self.confidence,
            "flags": self.flags,
        }


def paired_differences(
    trust_records: Iterable[TrustRecord], prop: Property
) -> list[PairedDifference]:
    """Every structure-matched comparison where both databases reported a value."""
    out: list[PairedDifference] = []
    for tr in trust_records:
        if tr.property_name is not prop:
            continue
        reps = tr.spread.representatives
        if Source.MATERIALS_PROJECT not in reps or Source.OQMD not in reps:
            continue
        codes = {f.code for f in tr.flags}
        out.append(
            PairedDifference(
                formula=tr.formula,
                property_name=prop,
                mp_value=reps[Source.MATERIALS_PROJECT],
                oqmd_value=reps[Source.OQMD],
                signed_difference=reps[Source.MATERIALS_PROJECT] - reps[Source.OQMD],
                hubbard_mismatch=FlagCode.HUBBARD_U_MISMATCH in codes,
                magnetic_mismatch=FlagCode.MAGNETIC_MISMATCH in codes,
                functional_mismatch=FlagCode.FUNCTIONAL_MISMATCH in codes,
                structure_fingerprint=tr.structure_fingerprint,
                confidence=tr.confidence.band.value,
                flags=sorted(c.value for c in codes),
            )
        )
    return out


def cross_source_stats(
    trust_records: Sequence[TrustRecord], prop: Property
) -> dict[str, Any]:
    pairs = paired_differences(trust_records, prop)
    diffs = [p.signed_difference for p in pairs]
    threshold = checks.disagreement_threshold(prop)
    units = "eV/atom" if prop is Property.FORMATION_ENERGY_PER_ATOM else "eV"

    within = [d for d in diffs if abs(d) <= threshold]
    with_u = [p.signed_difference for p in pairs if p.hubbard_mismatch]
    without_u = [p.signed_difference for p in pairs if not p.hubbard_mismatch]
    with_mag = [p.signed_difference for p in pairs if p.magnetic_mismatch]
    without_mag = [p.signed_difference for p in pairs if not p.magnetic_mismatch]

    return {
        "property": prop.value,
        "units": units,
        "threshold": threshold,
        "n_structure_matched_pairs": len(pairs),
        "n_within_threshold": len(within),
        "fraction_within_threshold": (
            round(len(within) / len(pairs), 4) if pairs else None
        ),
        "all": _describe(diffs),
        "stratified_by_hubbard_u_policy_mismatch": {
            "policies_differ": _describe(with_u),
            "policies_agree": _describe(without_u),
        },
        "stratified_by_magnetic_ordering_mismatch": {
            "ordering_differs": _describe(with_mag),
            "ordering_agrees": _describe(without_mag),
        },
        "largest_disagreements": [
            p.to_dict()
            for p in sorted(pairs, key=lambda x: abs(x.signed_difference), reverse=True)[:15]
        ],
    }


def flag_frequencies(trust_records: Sequence[TrustRecord]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for tr in trust_records:
        for code in {f.code.value for f in tr.flags}:
            counts[code] = counts.get(code, 0) + 1
    total = len(trust_records)
    return {
        "n_trust_records": total,
        "counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "fractions": {
            k: round(v / total, 4) for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
        }
        if total
        else {},
    }


def confidence_distribution(trust_records: Sequence[TrustRecord]) -> dict[str, int]:
    counts: dict[str, int] = {b.value: 0 for b in ConfidenceBand}
    for tr in trust_records:
        counts[tr.confidence.band.value] += 1
    return counts


def confidence_band_limits(trust_records: Sequence[TrustRecord]) -> dict[str, Any]:
    """Report what is actually holding the confidence bands down.

    A reader who sees zero high-confidence comparisons needs to know whether the
    databases disagreed or whether the metadata was too thin to certify agreement.
    Those are opposite conclusions, so the distinction is reported rather than
    left to be inferred from the band counts.
    """
    multi = [tr for tr in trust_records if len(tr.sources) > 1]
    if not multi:
        return {"n_multi_source_comparisons": 0}

    base_high = [
        tr for tr in multi if "base band high" in (tr.confidence.steps[0] if tr.confidence.steps else "")
    ]
    capping_counts: dict[str, int] = {}
    n_uncapped = 0
    for tr in multi:
        caps = tr.confidence.inputs.get("capping_flags") or []
        if not caps:
            n_uncapped += 1
        for code in caps:
            capping_counts[code] = capping_counts.get(code, 0) + 1

    universal = sorted(
        code for code, n in capping_counts.items() if n == len(multi)
    )
    return {
        "n_multi_source_comparisons": len(multi),
        "n_base_band_high_on_spread_alone": len(base_high),
        "n_final_band_high": sum(
            1 for tr in multi if tr.confidence.band is ConfidenceBand.HIGH
        ),
        "n_with_no_capping_provenance_caveat": n_uncapped,
        "capping_flag_counts": dict(sorted(capping_counts.items(), key=lambda kv: -kv[1])),
        "capping_flags_present_in_every_comparison": universal,
        "note": (
            "A comparison whose base band is high agreed within half the "
            "disagreement threshold. If it does not end at high, the limit was "
            "provenance rather than disagreement."
        ),
    }


# ---------------------------------------------------------------------------
# DFT versus experiment
# ---------------------------------------------------------------------------

def dft_vs_experiment_stats(comparisons: Sequence[GapComparison]) -> dict[str, Any]:
    """Quantify the systematic underestimation of band gaps.

    Metals and gapped materials are separated. For a material measured to be
    metallic the meaningful question is a classification one, does the
    calculation also give zero gap, and averaging a signed error over metals
    would dilute the underestimation that the gapped materials show.
    """
    usable = [c for c in comparisons if c.computed_gap_ev is not None]
    gapped = [c for c in usable if not c.measured_as_metal]
    metals = [c for c in usable if c.measured_as_metal]
    clean_gapped = [c for c in gapped if c.clean]

    errors_all = [c.signed_error_ev for c in gapped if c.signed_error_ev is not None]
    errors_clean = [c.signed_error_ev for c in clean_gapped if c.signed_error_ev is not None]

    n_underestimates = sum(1 for e in errors_clean if e < 0)
    computed_metal_but_measured_gapped = [
        c for c in gapped if c.computed_predicts_metal
    ]

    metal_correct = sum(1 for c in metals if c.computed_predicts_metal)

    return {
        "n_comparisons_attempted": len(comparisons),
        "n_with_computed_value": len(usable),
        "n_measured_gapped": len(gapped),
        "n_measured_metallic": len(metals),
        "n_clean_gapped": len(clean_gapped),
        "polymorph_spread_tolerance_ev": config.POLYMORPH_GAP_SPREAD_TOLERANCE_EV,
        "gapped_all_polymorph_states": _describe(errors_all),
        "gapped_clean_only": _describe(errors_clean),
        "fraction_underestimated_clean": (
            round(n_underestimates / len(errors_clean), 4) if errors_clean else None
        ),
        "n_computed_zero_gap_but_measured_gapped": len(
            computed_metal_but_measured_gapped
        ),
        "fraction_computed_zero_gap_but_measured_gapped": (
            round(len(computed_metal_but_measured_gapped) / len(gapped), 4)
            if gapped
            else None
        ),
        "metals": {
            "n": len(metals),
            "n_computed_also_zero_gap": metal_correct,
            "fraction_computed_also_zero_gap": (
                round(metal_correct / len(metals), 4) if metals else None
            ),
            "note": (
                "a computed gap of zero does not establish metallicity, so this is "
                "reported as a classification agreement rate and not as evidence "
                "that the calculation is correct"
            ),
        },
        "worst_underestimates": [
            c.to_dict()
            for c in sorted(
                clean_gapped, key=lambda c: c.signed_error_ev or 0.0
            )[:15]
        ],
        "worst_overestimates": [
            c.to_dict()
            for c in sorted(
                clean_gapped, key=lambda c: -(c.signed_error_ev or 0.0)
            )[:10]
        ],
    }


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------

def _save(fig, name: str) -> str:
    config.ensure_dirs()
    path = config.FIGURES_DIR / name
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    if path.is_relative_to(config.PROJECT_ROOT):
        return str(path.relative_to(config.PROJECT_ROOT)).replace("\\", "/")
    return str(path).replace("\\", "/")


def plot_disagreement_histogram(
    pairs: Sequence[PairedDifference], prop: Property, filename: str
) -> str | None:
    if not pairs:
        return None
    units = "eV/atom" if prop is Property.FORMATION_ENERGY_PER_ATOM else "eV"
    threshold = checks.disagreement_threshold(prop)
    diffs = [p.signed_difference for p in pairs]

    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.hist(diffs, bins=40, color="#3b6ea5", edgecolor="white")
    ax.axvline(0, color="black", linewidth=1)
    ax.axvline(threshold, color="#b03030", linestyle="--", linewidth=1,
               label=f"threshold {threshold} {units}")
    ax.axvline(-threshold, color="#b03030", linestyle="--", linewidth=1)
    ax.set_xlabel(f"Materials Project minus OQMD ({units})")
    ax.set_ylabel("number of structure-matched materials")
    ax.set_title(f"Cross-source disagreement, {prop.value} (n = {len(diffs)})")
    ax.legend()
    return _save(fig, filename)


def plot_disagreement_by_hubbard(
    pairs: Sequence[PairedDifference], prop: Property, filename: str
) -> str | None:
    groups = {
        "+U policies agree": [abs(p.signed_difference) for p in pairs if not p.hubbard_mismatch],
        "+U policies differ": [abs(p.signed_difference) for p in pairs if p.hubbard_mismatch],
    }
    groups = {k: v for k, v in groups.items() if v}
    if len(groups) < 1:
        return None
    units = "eV/atom" if prop is Property.FORMATION_ENERGY_PER_ATOM else "eV"

    fig, ax = plt.subplots(figsize=(6.5, 4.5))
    labels = list(groups)
    ax.boxplot([groups[k] for k in labels], showfliers=True)
    ax.set_xticks(
        range(1, len(labels) + 1),
        [f"{k}\n(n = {len(groups[k])})" for k in labels],
    )
    ax.axhline(
        checks.disagreement_threshold(prop),
        color="#b03030",
        linestyle="--",
        linewidth=1,
        label="disagreement threshold",
    )
    ax.set_ylabel(f"absolute disagreement ({units})")
    ax.set_title(f"Disagreement attributed to +U policy, {prop.value}")
    ax.legend()
    return _save(fig, filename)


def plot_gap_parity(comparisons: Sequence[GapComparison], filename: str) -> str | None:
    usable = [
        c
        for c in comparisons
        if c.computed_gap_ev is not None and not c.measured_as_metal
    ]
    if not usable:
        return None
    clean = [c for c in usable if c.clean]
    unclean = [c for c in usable if not c.clean]

    fig, ax = plt.subplots(figsize=(6, 6))
    if unclean:
        ax.scatter(
            [c.experimental_gap_ev for c in unclean],
            [c.computed_gap_ev for c in unclean],
            s=16,
            alpha=0.45,
            color="#c0a020",
            label=f"polymorph-ambiguous (n = {len(unclean)})",
        )
    ax.scatter(
        [c.experimental_gap_ev for c in clean],
        [c.computed_gap_ev for c in clean],
        s=18,
        alpha=0.7,
        color="#3b6ea5",
        label=f"clean comparison (n = {len(clean)})",
    )
    top = max(
        [c.experimental_gap_ev for c in usable] + [c.computed_gap_ev for c in usable]
    )
    top = max(top, 1.0) * 1.05
    ax.plot([0, top], [0, top], color="black", linewidth=1, label="parity")
    ax.set_xlim(0, top)
    ax.set_ylim(0, top)
    ax.set_xlabel("experimental band gap (eV)")
    ax.set_ylabel("computed band gap, PBE or PBE+U (eV)")
    ax.set_title("DFT band gap against experiment")
    ax.legend(loc="upper left", fontsize=8)
    return _save(fig, filename)


def plot_gap_error_histogram(
    comparisons: Sequence[GapComparison], filename: str
) -> str | None:
    errors = [
        c.signed_error_ev
        for c in comparisons
        if c.signed_error_ev is not None and not c.measured_as_metal and c.clean
    ]
    if not errors:
        return None
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.hist(errors, bins=40, color="#3b6ea5", edgecolor="white")
    ax.axvline(0, color="black", linewidth=1, label="perfect agreement")
    mean_err = statistics.fmean(errors)
    ax.axvline(
        mean_err,
        color="#b03030",
        linestyle="--",
        linewidth=1.5,
        label=f"mean signed error {mean_err:.2f} eV",
    )
    ax.set_xlabel("computed minus measured band gap (eV)")
    ax.set_ylabel("number of materials")
    ax.set_title(f"DFT band gap error against experiment (n = {len(errors)})")
    ax.legend()
    return _save(fig, filename)


def plot_flag_frequencies(freqs: dict[str, Any], filename: str) -> str | None:
    counts = freqs.get("counts") or {}
    if not counts:
        return None
    labels = list(counts)
    values = [counts[k] for k in labels]
    fig, ax = plt.subplots(figsize=(8, 0.45 * len(labels) + 2))
    ax.barh(labels[::-1], values[::-1], color="#3b6ea5")
    ax.set_xlabel(f"number of trust records (total = {freqs.get('n_trust_records')})")
    ax.set_title("Physics-consistency flag frequency")
    return _save(fig, filename)


def plot_confidence_distribution(counts: dict[str, int], filename: str) -> str | None:
    labels = [k for k, v in counts.items() if v]
    if not labels:
        return None
    values = [counts[k] for k in labels]
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.bar(labels, values, color="#3b6ea5")
    ax.set_ylabel("number of trust records")
    ax.set_title("Confidence band distribution")
    return _save(fig, filename)
