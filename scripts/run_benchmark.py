"""Run the full benchmark and write the results.

Brief rule 1: real data only. If a source fails, the failure is recorded and the
material is reported with reduced coverage. Nothing is imputed, and no gap is
filled with a fabricated number.

Brief rule 7: verify before claiming. Every number in the generated report comes
from this run.

Run:  python scripts/run_benchmark.py [--limit N] [--no-cache]
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import traceback
from typing import Any

from materials_trust import config, report, unit_checks
from materials_trust.audit import Auditor, TrustRecord
from materials_trust.records import Property, Source
from materials_trust.sources.experimental import GapComparison, compare_gap
from materials_trust.sources.materials_project import MaterialsProjectSource, MissingAPIKey
from materials_trust.sources.oqmd import OQMDSource


def load_material_set(limit: int | None = None) -> list[dict[str, Any]]:
    path = config.RESULTS_DIR / "material_set.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run scripts/select_material_set.py first."
        )
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["expt_gap_ev"] = float(r["expt_gap_ev"])
        r["is_metal_measured"] = r["is_metal_measured"].strip().lower() == "true"
        r["hubbard_policy_differs"] = (
            r["hubbard_policy_differs"].strip().lower() == "true"
        )
    return rows[:limit] if limit else rows


def run_unit_checks(mp: MaterialsProjectSource, oqmd: OQMDSource) -> dict[str, Any]:
    """Verify formation energy units on both sources before trusting anything."""
    out: dict[str, Any] = {}
    for label, src in (("oqmd", oqmd), ("materials_project", mp)):
        verdicts = []
        signs: list[tuple[str, float]] = []
        for formula in unit_checks.LITERATURE_FORMATION_ENTHALPY_KJ_PER_MOL:
            if label == "oqmd":
                records = src.records_for(
                    formula, properties=[Property.FORMATION_ENERGY_PER_ATOM]
                )
            else:
                records = src.records_for(
                    formula=formula, properties=[Property.FORMATION_ENERGY_PER_ATOM]
                )
            values = [
                r.value
                for r in records
                if r.property_name is Property.FORMATION_ENERGY_PER_ATOM
            ]
            if not values:
                continue
            value = min(values)
            verdicts.append(unit_checks.classify_units(formula, value))
            signs.append((formula, value))
        rep = unit_checks.UnitCheckReport(verdicts=verdicts, source=label)
        rep.raise_if_failed()
        problems = unit_checks.check_sign_convention(signs)
        if problems:
            raise unit_checks.UnitCheckFailure("\n".join(problems))
        out[label] = rep.to_dict()
        print(f"  {label}: units confirmed eV/atom on {len(verdicts)} reference compounds")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="audit only the first N")
    parser.add_argument("--no-cache", action="store_true", help="bypass the disk cache")
    args = parser.parse_args()

    config.ensure_dirs()
    started = time.time()

    mp = MaterialsProjectSource(cache_enabled=not args.no_cache)
    oqmd = OQMDSource(cache_enabled=not args.no_cache)
    try:
        mp._require_key()
    except MissingAPIKey as exc:
        print(f"FATAL: {exc}")
        return 2

    print("Verifying formation energy units before running the benchmark")
    unit_report = run_unit_checks(mp, oqmd)

    materials = load_material_set(args.limit)
    print(f"\nAuditing {len(materials)} compositions")

    auditor = Auditor(mp=mp, oqmd=oqmd)
    all_trust: list[TrustRecord] = []
    audit_payloads: list[dict[str, Any]] = []
    gap_comparisons_mp: list[GapComparison] = []
    gap_comparisons_oqmd: list[GapComparison] = []
    run_failures: list[dict[str, Any]] = []

    coverage_totals = {
        "n_compositions": len(materials),
        "found_in_materials_project": 0,
        "found_in_oqmd": 0,
        "found_in_both": 0,
        "found_in_neither": 0,
        "n_structure_matched_multi_source": 0,
    }

    for index, row in enumerate(materials, start=1):
        formula = row["reduced_formula"]
        try:
            result = auditor.audit(formula)
        except Exception as exc:
            run_failures.append(
                {
                    "composition": formula,
                    "reason": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=3),
                }
            )
            print(f"  [{index:3d}/{len(materials)}] {formula:14s} FAILED: {exc}")
            continue

        all_trust.extend(result.trust_records)
        payload = result.to_dict()
        payload["material_set_row"] = {
            "expt_gap_ev": row["expt_gap_ev"],
            "likely_mpid": row["likely_mpid"],
            "selection_stratum": row["selection_stratum"],
            "is_metal_measured": row["is_metal_measured"],
        }
        audit_payloads.append(payload)

        in_mp = Source.MATERIALS_PROJECT.value in result.coverage.n_records_by_source
        in_oqmd = Source.OQMD.value in result.coverage.n_records_by_source
        coverage_totals["found_in_materials_project"] += int(in_mp)
        coverage_totals["found_in_oqmd"] += int(in_oqmd)
        coverage_totals["found_in_both"] += int(in_mp and in_oqmd)
        coverage_totals["found_in_neither"] += int(not in_mp and not in_oqmd)
        coverage_totals["n_structure_matched_multi_source"] += (
            result.coverage.n_structure_matched_multi_source
        )

        # DFT versus experiment, done separately for each computed source so that
        # neither database's coverage gaps contaminate the other's statistics.
        computed = [
            r
            for tr in result.trust_records
            for r in tr.records
            if tr.property_name is Property.BAND_GAP
        ]
        mp_gap_records = [r for r in computed if r.source is Source.MATERIALS_PROJECT]
        oqmd_gap_records = [r for r in computed if r.source is Source.OQMD]
        if mp_gap_records:
            gap_comparisons_mp.append(
                compare_gap(
                    formula,
                    row["expt_gap_ev"],
                    mp_gap_records,
                    likely_mpid=row["likely_mpid"] or None,
                )
            )
        if oqmd_gap_records:
            gap_comparisons_oqmd.append(
                compare_gap(formula, row["expt_gap_ev"], oqmd_gap_records)
            )

        if index % 10 == 0 or index == len(materials):
            print(
                f"  [{index:3d}/{len(materials)}] {formula:14s} "
                f"trust_records={len(all_trust)} "
                f"both_sources={coverage_totals['found_in_both']}"
            )

    print("\nComputing statistics")
    fe_stats = report.cross_source_stats(all_trust, Property.FORMATION_ENERGY_PER_ATOM)
    gap_stats = report.cross_source_stats(all_trust, Property.BAND_GAP)
    flags = report.flag_frequencies(all_trust)
    confidence = report.confidence_distribution(all_trust)
    confidence_limits = report.confidence_band_limits(all_trust)
    expt_mp = report.dft_vs_experiment_stats(gap_comparisons_mp)
    expt_oqmd = report.dft_vs_experiment_stats(gap_comparisons_oqmd)

    print("Rendering plots")
    fe_pairs = report.paired_differences(all_trust, Property.FORMATION_ENERGY_PER_ATOM)
    gap_pairs = report.paired_differences(all_trust, Property.BAND_GAP)
    figures = {
        "formation_energy_disagreement": report.plot_disagreement_histogram(
            fe_pairs, Property.FORMATION_ENERGY_PER_ATOM, "formation_energy_disagreement.png"
        ),
        "band_gap_disagreement": report.plot_disagreement_histogram(
            gap_pairs, Property.BAND_GAP, "band_gap_disagreement.png"
        ),
        "formation_energy_by_hubbard": report.plot_disagreement_by_hubbard(
            fe_pairs, Property.FORMATION_ENERGY_PER_ATOM, "formation_energy_by_hubbard.png"
        ),
        "band_gap_by_hubbard": report.plot_disagreement_by_hubbard(
            gap_pairs, Property.BAND_GAP, "band_gap_by_hubbard.png"
        ),
        "gap_parity": report.plot_gap_parity(gap_comparisons_mp, "gap_parity.png"),
        "gap_error_histogram": report.plot_gap_error_histogram(
            gap_comparisons_mp, "gap_error_histogram.png"
        ),
        "flag_frequencies": report.plot_flag_frequencies(flags, "flag_frequencies.png"),
        "confidence_distribution": report.plot_confidence_distribution(
            confidence, "confidence_distribution.png"
        ),
    }

    elapsed = time.time() - started
    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": round(elapsed, 1),
        "n_compositions_requested": len(materials),
        "n_compositions_audited": len(audit_payloads),
        "n_trust_records": len(all_trust),
        "coverage": coverage_totals,
        "unit_checks": unit_report,
        "cross_source_formation_energy": fe_stats,
        "cross_source_band_gap": gap_stats,
        "dft_vs_experiment_materials_project": expt_mp,
        "dft_vs_experiment_oqmd": expt_oqmd,
        "flag_frequencies": flags,
        "confidence_distribution": confidence,
        "confidence_band_limits": confidence_limits,
        "figures": {k: v for k, v in figures.items() if v},
        "n_run_failures": len(run_failures),
        "source_failures": {
            "materials_project": [f.to_dict() for f in mp.failures],
            "oqmd": [f.to_dict() for f in oqmd.failures],
        },
    }

    (config.RESULTS_DIR / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    (config.RESULTS_DIR / "trust_records.json").write_text(
        json.dumps(audit_payloads, indent=2), encoding="utf-8"
    )
    (config.RESULTS_DIR / "gap_comparisons.json").write_text(
        json.dumps(
            {
                "materials_project": [c.to_dict() for c in gap_comparisons_mp],
                "oqmd": [c.to_dict() for c in gap_comparisons_oqmd],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if run_failures:
        (config.RESULTS_DIR / "run_failures.json").write_text(
            json.dumps(run_failures, indent=2), encoding="utf-8"
        )

    print(f"\nDone in {elapsed / 60:.1f} min")
    print(f"  trust records: {len(all_trust)}")
    print(f"  coverage: {coverage_totals}")
    print(
        "  formation energy pairs: "
        f"{fe_stats['n_structure_matched_pairs']}, MAE "
        f"{fe_stats['all'].get('mean_absolute')} eV/atom"
    )
    print(
        "  band gap pairs: "
        f"{gap_stats['n_structure_matched_pairs']}, MAE "
        f"{gap_stats['all'].get('mean_absolute')} eV"
    )
    print(
        "  DFT vs experiment (Materials Project, clean gapped): "
        f"n={expt_mp['gapped_clean_only'].get('n')}, mean signed error "
        f"{expt_mp['gapped_clean_only'].get('mean_signed')} eV"
    )
    print(f"  run failures: {len(run_failures)}")
    print(f"\nWrote results to {config.RESULTS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
