"""Build the static assay UI under site/.

The 19 MB trust_records.json cannot be shipped to a browser. This script
writes a compact per-composition index plus the dashboard numbers the page
actually renders, and copies the figures.
"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

from materials_trust import config


def _median(source_block: dict | None) -> float | None:
    if not source_block:
        return None
    value = source_block.get("representative_median")
    return None if value is None else round(float(value), 4)


def _best_pair(records: list[dict], prop: str) -> dict | None:
    multi = [
        t
        for t in records
        if t.get("property") == prop and len(t.get("sources") or []) >= 2
    ]
    if not multi:
        return None

    def spread(t: dict) -> float:
        value = (t.get("spread") or {}).get("cross_source_spread")
        return 99.0 if value is None else float(value)

    chosen = min(multi, key=spread)
    per = (chosen.get("spread") or {}).get("per_source") or {}
    return {
        "structure": chosen.get("structure_fingerprint"),
        "mp": _median(per.get("materials_project")),
        "oqmd": _median(per.get("oqmd")),
        "spread": round(spread(chosen), 4),
        "band": (chosen.get("confidence") or {}).get("band"),
        "flags": chosen.get("core_flags") or [],
    }


def compact_materials() -> list[dict]:
    set_path = config.RESULTS_DIR / "material_set.csv"
    with set_path.open(newline="", encoding="utf-8") as fh:
        rows = {r["reduced_formula"]: r for r in csv.DictReader(fh)}

    audits = json.loads((config.RESULTS_DIR / "trust_records.json").read_text(encoding="utf-8"))
    out = []
    for audit in audits:
        formula = audit["composition"]
        row = rows.get(formula, {})
        records = audit.get("trust_records") or []
        sources = set()
        for tr in records:
            sources.update(tr.get("sources") or [])
        structures = {
            tr.get("structure_fingerprint")
            for tr in records
            if tr.get("structure_fingerprint")
        }
        out.append(
            {
                "formula": formula,
                "mpid": row.get("likely_mpid") or "",
                "expt_gap": float(row["expt_gap_ev"]) if row.get("expt_gap_ev") else None,
                "metal": (row.get("is_metal_measured") or "").lower() == "true",
                "stratum": (row.get("selection_stratum") or "").replace("_", " "),
                "hubbard_differs": (row.get("hubbard_policy_differs") or "").lower() == "true",
                "hubbard": (row.get("hubbard_explanation") or "")[:280],
                "in_mp": "materials_project" in sources,
                "in_oqmd": "oqmd" in sources,
                "n_structures": len(structures),
                "n_records": len(records),
                "fe": _best_pair(records, "formation_energy_per_atom"),
                "gap": _best_pair(records, "band_gap"),
            }
        )
    out.sort(key=lambda m: m["formula"])
    return out


def dashboard(summary: dict) -> dict:
    fe = summary["cross_source_formation_energy"]
    gap = summary["cross_source_band_gap"]
    expt = summary["dft_vs_experiment_materials_project"]
    cov = summary["coverage"]
    return {
        "generated_at": summary["generated_at"],
        "n_compositions": cov["n_compositions"],
        "found_mp": cov["found_in_materials_project"],
        "found_oqmd": cov["found_in_oqmd"],
        "found_both": cov["found_in_both"],
        "found_neither": cov["found_in_neither"],
        "n_matched": cov["n_structure_matched_multi_source"],
        "n_trust_records": summary["n_trust_records"],
        "fe_pairs": fe["n_structure_matched_pairs"],
        "fe_within": fe["n_within_threshold"],
        "fe_fraction": fe["fraction_within_threshold"],
        "fe_mae": fe["all"]["mean_absolute"],
        "fe_signed": fe["all"]["mean_signed"],
        "fe_threshold": fe["threshold"],
        "fe_hubbard_differ_mae": fe["stratified_by_hubbard_u_policy_mismatch"]["policies_differ"]["mean_absolute"],
        "fe_hubbard_agree_mae": fe["stratified_by_hubbard_u_policy_mismatch"]["policies_agree"]["mean_absolute"],
        "gap_pairs": gap["n_structure_matched_pairs"],
        "gap_mae": gap["all"]["mean_absolute"],
        "gap_fraction": gap["fraction_within_threshold"],
        "expt_n": expt["gapped_clean_only"]["n"],
        "expt_signed": expt["gapped_clean_only"]["mean_signed"],
        "expt_under": expt["fraction_underestimated_clean"],
        "confidence": summary["confidence_distribution"],
        "confidence_note": summary["confidence_band_limits"]["note"],
        "n_base_high": summary["confidence_band_limits"]["n_base_band_high_on_spread_alone"],
        "flags": summary["flag_frequencies"]["counts"],
    }


def main() -> int:
    config.ensure_dirs()
    site = config.PROJECT_ROOT / "site"
    data = site / "data"
    figures = site / "figures"
    data.mkdir(parents=True, exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)

    summary = json.loads((config.RESULTS_DIR / "summary.json").read_text(encoding="utf-8"))
    (data / "dashboard.json").write_text(
        json.dumps(dashboard(summary), indent=2), encoding="utf-8"
    )
    materials = compact_materials()
    (data / "materials.json").write_text(
        json.dumps(materials, separators=(",", ":")), encoding="utf-8"
    )

    src_fig = config.RESULTS_DIR / "figures"
    if src_fig.exists():
        for png in src_fig.glob("*.png"):
            shutil.copy2(png, figures / png.name)

    print(f"wrote {len(materials)} materials to {data / 'materials.json'}")
    print(f"figures: {len(list(figures.glob('*.png')))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
