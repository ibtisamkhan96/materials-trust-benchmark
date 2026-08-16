"""Select the benchmark material set, and justify every criterion.

Brief section 3.2.2: run the benchmark over a documented material set of a few
hundred well known compounds with good coverage across sources, and document
exactly how the set was chosen. This script is that documentation, in executable
form. It writes both the set and a written justification.

The set is anchored on the experimental band gap dataset so that a single set of
materials supports both halves of the benchmark: cross-source agreement between
Materials Project and OQMD, and the comparison of computed gaps against
measurement. Sampling is stratified and deliberately over-samples the
chemistries where the two databases' documented Hubbard U policies differ,
because that is where a physically explainable disagreement is predicted before
any data is fetched. Over-sampling a predicted effect is a legitimate design
choice for a diagnostic benchmark, but it does mean the flag frequencies are not
an unbiased estimate of the whole databases, and the report says so.

Run:  python scripts/select_material_set.py
"""

from __future__ import annotations

import csv
import json
import random
from collections import Counter
from typing import Any

from pymatgen.core import Composition

from materials_trust import config, hubbard
from materials_trust.sources.experimental import (
    DATASET_NAME,
    DATASET_REFERENCE,
    load_experimental_gaps,
)

SEED = 20260815

#: Elements to which either database applies +U in some chemistry.
U_RELEVANT_ELEMENTS = frozenset(hubbard.MP_U_EV) | frozenset(hubbard.OQMD_U_MINUS_J_EV)

CHALCOGENS_AND_PNICTOGENS = frozenset({"S", "Se", "Te", "N", "P", "As", "Sb", "Bi"})
HALOGENS = frozenset({"F", "Cl", "Br", "I"})

#: Target counts per stratum. Sum is the size of the gapped part of the set.
TARGETS: dict[str, int] = {
    "A_correlated_oxide_or_fluoride": 110,
    "B_main_group_oxide": 60,
    "C_halide_non_fluoride": 35,
    "D_chalcogenide_or_pnictide": 40,
    "E_other": 15,
}

#: Materials measured to be metallic, kept as a separate stratum. They support
#: the classification question (does the calculation also give zero gap) and are
#: excluded from the signed error statistics.
METAL_TARGET = 40

STRATUM_RATIONALE: dict[str, str] = {
    "A_correlated_oxide_or_fluoride": (
        "Contains oxygen or fluorine together with an element in either "
        "database's +U table. This is where the two databases' documented "
        "policies diverge most: Materials Project applies +U to fluorides and "
        "OQMD does not, OQMD applies +U to copper oxides and Materials Project "
        "does not, Materials Project applies +U to molybdenum and tungsten "
        "oxides and OQMD does not, and where both apply +U the parameters "
        "differ. Deliberately the largest stratum."
    ),
    "B_main_group_oxide": (
        "Oxides with no +U element in either database. These act as the control "
        "group: any disagreement here cannot be attributed to +U treatment and "
        "must have another explanation."
    ),
    "C_halide_non_fluoride": (
        "Chlorides, bromides, and iodides. Neither database applies +U to these, "
        "and they are strongly ionic with large gaps, so they probe the band gap "
        "comparison at the high gap end."
    ),
    "D_chalcogenide_or_pnictide": (
        "Sulfides, selenides, tellurides, nitrides, phosphides, arsenides. These "
        "are the classic semiconductors and therefore the most informative "
        "chemistry for the DFT versus experiment gap comparison."
    ),
    "E_other": (
        "Intermetallics, hydrides, borides, carbides, and anything not caught "
        "above. Included so the set is not silently restricted to a handful of "
        "anion chemistries."
    ),
}


def has_integer_stoichiometry(formula: str) -> bool:
    """Reject solid solutions and non-stoichiometric entries.

    The dataset contains rows like ``Hg0.7Cd0.3Te`` and ``Ag0.5Ge1Pb1.75S4``.
    These are alloys and solid solutions, not stoichiometric compounds. Neither
    database holds them as single entries, and a structure match against them is
    not a meaningful operation, so they are excluded rather than compared badly.
    """
    try:
        comp = Composition(formula).reduced_composition
    except Exception:
        return False
    amounts = comp.get_el_amt_dict().values()
    return all(abs(v - round(v)) < 1e-6 for v in amounts)


def classify(formula: str) -> str:
    els = {str(e) for e in Composition(formula).elements}
    has_u_element = bool(els & U_RELEVANT_ELEMENTS)
    if ({"O", "F"} & els) and has_u_element:
        return "A_correlated_oxide_or_fluoride"
    if "O" in els:
        return "B_main_group_oxide"
    if (HALOGENS - {"F"}) & els:
        return "C_halide_non_fluoride"
    if CHALCOGENS_AND_PNICTOGENS & els:
        return "D_chalcogenide_or_pnictide"
    return "E_other"


def main() -> int:
    config.ensure_dirs()
    rng = random.Random(SEED)

    raw = load_experimental_gaps(require_mpid=False)
    n_raw = len(raw)

    df = raw[raw["likely_mpid"].notna()].copy()
    n_with_mpid = len(df)

    df["integer_stoichiometry"] = [has_integer_stoichiometry(f) for f in df["formula"]]
    df = df[df["integer_stoichiometry"]].copy()
    n_integer = len(df)

    # One row per composition. Where the dataset holds several measurements for
    # the same composition, keep the median so a single outlying measurement
    # cannot drive selection, and record how many measurements existed.
    df["reduced_formula"] = [Composition(f).reduced_formula for f in df["formula"]]
    grouped = (
        df.groupby("reduced_formula")
        .agg(
            expt_gap_ev=("expt_gap_ev", "median"),
            n_measurements=("expt_gap_ev", "size"),
            gap_range_ev=("expt_gap_ev", lambda s: float(s.max() - s.min())),
            likely_mpid=("likely_mpid", "first"),
        )
        .reset_index()
    )
    n_compositions = len(grouped)

    grouped["is_metal_measured"] = grouped["expt_gap_ev"] <= 0.0
    grouped["stratum"] = [classify(f) for f in grouped["reduced_formula"]]
    hub = [hubbard.compare_hubbard_treatment(f) for f in grouped["reduced_formula"]]
    grouped["hubbard_policy_differs"] = [not h.agrees for h in hub]
    grouped["hubbard_explanation"] = [h.explanation() for h in hub]

    gapped = grouped[~grouped["is_metal_measured"]]
    metals = grouped[grouped["is_metal_measured"]]

    selected: list[dict[str, Any]] = []
    shortfalls: dict[str, dict[str, int]] = {}

    for stratum, target in TARGETS.items():
        pool = gapped[gapped["stratum"] == stratum]
        rows = pool.to_dict(orient="records")
        rows.sort(key=lambda r: r["reduced_formula"])  # deterministic base order
        if len(rows) > target:
            # Within a stratum, prefer compositions where the +U policies differ,
            # then fill the remainder at random from a fixed seed.
            differing = [r for r in rows if r["hubbard_policy_differs"]]
            same = [r for r in rows if not r["hubbard_policy_differs"]]
            take_differing = differing[: min(len(differing), target)]
            remaining = target - len(take_differing)
            rng.shuffle(same)
            chosen = take_differing + same[:remaining]
        else:
            chosen = rows
        shortfalls[stratum] = {"target": target, "available": len(rows), "selected": len(chosen)}
        for r in chosen:
            r["selection_stratum"] = stratum
            r["selection_role"] = "gapped semiconductor or insulator"
        selected.extend(chosen)

    metal_rows = metals.to_dict(orient="records")
    metal_rows.sort(key=lambda r: r["reduced_formula"])
    rng.shuffle(metal_rows)
    metal_chosen = metal_rows[:METAL_TARGET]
    for r in metal_chosen:
        r["selection_stratum"] = "M_measured_metallic"
        r["selection_role"] = "measured metallic, classification check only"
    selected.extend(metal_chosen)
    shortfalls["M_measured_metallic"] = {
        "target": METAL_TARGET,
        "available": len(metal_rows),
        "selected": len(metal_chosen),
    }

    selected.sort(key=lambda r: (r["selection_stratum"], r["reduced_formula"]))

    columns = [
        "reduced_formula",
        "likely_mpid",
        "expt_gap_ev",
        "n_measurements",
        "gap_range_ev",
        "is_metal_measured",
        "selection_stratum",
        "selection_role",
        "hubbard_policy_differs",
        "hubbard_explanation",
    ]
    csv_path = config.RESULTS_DIR / "material_set.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in selected:
            writer.writerow(row)

    stratum_counts = Counter(r["selection_stratum"] for r in selected)
    n_differ = sum(1 for r in selected if r["hubbard_policy_differs"])

    provenance = {
        "seed": SEED,
        "dataset": DATASET_NAME,
        "dataset_reference": DATASET_REFERENCE,
        "funnel": {
            "rows_in_dataset": n_raw,
            "rows_with_materials_project_id": n_with_mpid,
            "rows_with_integer_stoichiometry": n_integer,
            "distinct_compositions": n_compositions,
        },
        "targets": TARGETS,
        "metal_target": METAL_TARGET,
        "per_stratum": shortfalls,
        "selected_total": len(selected),
        "selected_with_differing_hubbard_policy": n_differ,
    }
    (config.RESULTS_DIR / "material_set_provenance.json").write_text(
        json.dumps(provenance, indent=2), encoding="utf-8"
    )

    lines = [
        "# Benchmark material set",
        "",
        "Generated by `scripts/select_material_set.py` with a fixed seed, so the",
        "set is reproducible. This file explains every criterion applied.",
        "",
        "## Where the set comes from",
        "",
        f"The set is anchored on the `{DATASET_NAME}` dataset ({DATASET_REFERENCE}).",
        "Anchoring on it means one material set serves both halves of the benchmark:",
        "cross-source agreement between Materials Project and OQMD, and the",
        "comparison of computed band gaps against measurement.",
        "",
        "## Filters applied, in order",
        "",
        f"1. Start from all {n_raw} rows in the dataset.",
        f"2. Keep only rows carrying a Materials Project ID: {n_with_mpid} rows.",
        "   Rows without one cannot be tied to a specific computed entry, and",
        "   matching them on formula alone is exactly what brief section 2.1",
        "   forbids.",
        f"3. Keep only integer stoichiometries: {n_integer} rows. The dataset",
        "   contains solid solutions such as Hg0.7Cd0.3Te. Neither database holds",
        "   these as single entries and a structure match against them is not a",
        "   meaningful operation.",
        f"4. Collapse to one row per composition: {n_compositions} compositions.",
        "   Where several measurements exist for a composition the median is kept,",
        "   and the number of measurements and their range are recorded so that",
        "   experimental scatter is visible rather than hidden.",
        "",
        "## Stratification",
        "",
        "Compositions are assigned to exactly one stratum by the rules below,",
        "evaluated in order. Within each stratum, compositions whose +U policies",
        "differ between the two databases are selected first, then the remainder",
        "is filled by seeded random sampling.",
        "",
    ]
    for stratum, target in TARGETS.items():
        info = shortfalls[stratum]
        lines.append(f"### {stratum}")
        lines.append("")
        lines.append(STRATUM_RATIONALE[stratum])
        lines.append("")
        lines.append(
            f"Target {target}, available {info['available']}, selected {info['selected']}."
        )
        lines.append("")
    lines.extend(
        [
            "### M_measured_metallic",
            "",
            "Compositions measured to have a zero band gap. Held separately because",
            "brief section 2.4 is explicit that a computed gap of 0.0 eV does not",
            "establish that a material is a metal. Averaging a signed gap error over",
            "materials whose measured gap is zero would also dilute the systematic",
            "underestimation that the gapped materials exhibit. These are used only",
            "for the classification question, does the calculation also give zero gap.",
            "",
            f"Target {METAL_TARGET}, available "
            f"{shortfalls['M_measured_metallic']['available']}, selected "
            f"{shortfalls['M_measured_metallic']['selected']}.",
            "",
            "## Resulting set",
            "",
            f"Total compositions selected: **{len(selected)}**.",
            "",
            "| stratum | n |",
            "| --- | --- |",
        ]
    )
    for stratum, count in sorted(stratum_counts.items()):
        lines.append(f"| {stratum} | {count} |")
    lines.extend(
        [
            "",
            f"Compositions where the two databases' documented +U policies differ: "
            f"**{n_differ}** of {len(selected)}.",
            "",
            "## Known bias in this set",
            "",
            "The set intentionally over-samples correlated oxides and fluorides",
            "because that is where a physically explainable disagreement is",
            "predicted in advance. The consequence is that flag frequencies measured",
            "on this set are not an unbiased estimate of the frequencies across the",
            "whole of either database, and they should not be quoted as such. The",
            "cross-source agreement statistics stratified by +U policy remain valid",
            "within each stratum, because the stratification is exactly the variable",
            "being conditioned on.",
            "",
            "A second bias follows from anchoring on an experimental band gap",
            "compilation: the set is restricted to materials someone chose to measure",
            "a band gap for, which skews towards semiconductors and insulators of",
            "technological interest and away from both metals and obscure compounds.",
        ]
    )
    (config.DOCS_DIR / "material-set.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"dataset rows: {n_raw}")
    print(f"with mp-id: {n_with_mpid}")
    print(f"integer stoichiometry: {n_integer}")
    print(f"distinct compositions: {n_compositions}")
    for stratum, info in shortfalls.items():
        print(
            f"  {stratum:34s} target {info['target']:4d}  available {info['available']:5d}"
            f"  selected {info['selected']:4d}"
        )
    print(f"total selected: {len(selected)}")
    print(f"with differing +U policy: {n_differ}")
    print(f"wrote {csv_path}")
    print(f"wrote {config.DOCS_DIR / 'material-set.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
