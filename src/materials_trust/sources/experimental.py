"""Experimental band gaps, and the DFT versus experiment comparison.

Source dataset: ``expt_gap_kingsbury`` via matminer, 4604 rows with columns
``formula``, ``expt_gap`` (eV), and ``likely_mpid``. It derives from the Zhuo
et al. compilation (J. Phys. Chem. Lett. 2018, 9, 1668) with Materials Project
IDs associated by Kingsbury et al.

Two features of this dataset drive the design, and both were established by
loading it rather than by reading about it:

**Only 2481 of the 4604 rows carry a Materials Project ID.** The rest cannot be
tied to a specific computed entry at all, so they are excluded from the
structure-aware comparison rather than matched on formula.

**2450 of the 4604 rows report a gap of exactly 0.0 eV**, meaning the material
was measured to be metallic. Folding those into a mean signed error would be a
serious mistake: the interesting question for a metal is a classification
question, does the calculation also predict zero gap, and averaging it together
with semiconductors would dilute the very underestimation the benchmark exists
to quantify. So metals and gapped materials are separated, and each is reported
on its own terms.

The deeper problem is structural, and brief section 2.1 forbids ignoring it. An
experimental band gap is reported against a composition, not against a crystal
structure. There is no structure to match, so ``StructureMatcher`` cannot rescue
us here and pretending otherwise would be dishonest. The comparison is therefore
explicitly labelled composition-level, and to make the residual ambiguity
visible the pipeline retrieves every computed polymorph of the composition and
reports the spread of computed gaps across them. Where that spread is large, the
comparison is not clean, because the experiment may have been performed on a
different polymorph than the one being compared, and it is reported separately
rather than folded into the headline number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import pandas as pd
from pymatgen.core import Composition

from .. import config
from ..records import (
    Functional,
    MagneticState,
    Property,
    PropertyRecord,
    Source,
    ValueKind,
)

DATASET_NAME = "expt_gap_kingsbury"
DATASET_REFERENCE = (
    "Zhuo, Masouri Tehrani and Brgoch, J. Phys. Chem. Lett. 2018, 9, 1668, "
    "as compiled in matminer's expt_gap_kingsbury with Materials Project IDs "
    "associated by Kingsbury et al."
)

#: Gaps at or below this are treated as a measurement of "metallic" rather than
#: as a small finite gap.
METAL_GAP_THRESHOLD_EV = 0.0


def load_experimental_gaps(require_mpid: bool = False) -> pd.DataFrame:
    """Load the curated experimental band gap dataset."""
    from matminer.datasets import load_dataset

    df = load_dataset(DATASET_NAME)
    df = df.rename(columns={"expt_gap": "expt_gap_ev"})
    df["reduced_formula"] = [
        Composition(f).reduced_formula for f in df["formula"].tolist()
    ]
    df["is_metal_measured"] = df["expt_gap_ev"] <= METAL_GAP_THRESHOLD_EV
    if require_mpid:
        df = df[df["likely_mpid"].notna()].copy()
    return df.reset_index(drop=True)


def experimental_records(df: pd.DataFrame) -> list[PropertyRecord]:
    """Turn dataset rows into provenanced records.

    ``structure`` is None and ``structure_is_icsd_derived`` is None because the
    dataset carries no structure. That is not a gap in the plumbing, it is a
    property of experimental band gap compilations, and the record type records
    it honestly instead of inventing a structure.
    """
    records: list[PropertyRecord] = []
    for row in df.itertuples(index=False):
        mpid = getattr(row, "likely_mpid", None)
        source_id = str(mpid) if isinstance(mpid, str) and mpid else f"formula:{row.formula}"
        records.append(
            PropertyRecord(
                source=Source.EXPERIMENT,
                source_id=source_id,
                formula=row.formula,
                property_name=Property.BAND_GAP,
                value=float(row.expt_gap_ev),
                units="eV",
                functional=Functional.NOT_APPLICABLE,
                correction_scheme="not_applicable",
                magnetic_state=MagneticState.NOT_APPLICABLE,
                value_kind=ValueKind.MEASURED,
                structure_is_icsd_derived=None,
                structure=None,
                source_url=None,
                extras={
                    "dataset": DATASET_NAME,
                    "dataset_reference": DATASET_REFERENCE,
                    "likely_mpid": mpid if isinstance(mpid, str) else None,
                    "mpid_association": (
                        "composition-level association published with the dataset, "
                        "not a structure match"
                    ),
                    "measured_as_metal": bool(row.is_metal_measured),
                },
            )
        )
    return records


@dataclass
class PolymorphGaps:
    """Every computed gap for one composition, with the ground state identified."""

    formula: str
    entries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def gaps(self) -> list[float]:
        return [e["band_gap"] for e in self.entries]

    @property
    def spread(self) -> float | None:
        if len(self.gaps) < 2:
            return None
        return float(max(self.gaps) - min(self.gaps))

    def ground_state(self) -> dict[str, Any] | None:
        """The entry with the lowest energy above hull.

        Experiments are normally performed on the thermodynamically stable
        phase, so this is the defensible default comparison partner. It is a
        stated assumption, not a certainty, which is why the polymorph spread
        travels alongside it.
        """
        scored = [e for e in self.entries if e.get("energy_above_hull") is not None]
        if not scored:
            return self.entries[0] if self.entries else None
        return min(scored, key=lambda e: e["energy_above_hull"])

    def by_mpid(self, mpid: str | None) -> dict[str, Any] | None:
        if not mpid:
            return None
        for e in self.entries:
            if e["material_id"] == mpid:
                return e
        return None


def polymorph_gaps_from_records(records: Sequence[PropertyRecord]) -> PolymorphGaps:
    """Collect computed band gaps for one composition from source records."""
    if not records:
        raise ValueError("no records supplied")
    out = PolymorphGaps(formula=records[0].reduced_formula)
    for r in records:
        if r.property_name is not Property.BAND_GAP:
            continue
        out.entries.append(
            {
                "material_id": r.source_id,
                "source": r.source.value,
                "band_gap": float(r.value),
                "energy_above_hull": r.extras.get("energy_above_hull"),
                "functional": r.functional.value,
                "magnetic_state": r.magnetic_state.value,
                "structure_fingerprint": r.structure_fingerprint(),
                "is_icsd_derived": r.structure_is_icsd_derived,
            }
        )
    return out


@dataclass
class GapComparison:
    """One composition-level comparison of a computed gap against a measurement."""

    formula: str
    experimental_gap_ev: float
    measured_as_metal: bool
    computed_gap_ev: float | None
    computed_material_id: str | None
    computed_source: str | None
    computed_functional: str | None
    polymorph_spread_ev: float | None
    n_polymorphs: int
    likely_mpid: str | None
    likely_mpid_gap_ev: float | None
    notes: list[str] = field(default_factory=list)

    @property
    def signed_error_ev(self) -> float | None:
        """Computed minus measured. Negative means the calculation underestimates."""
        if self.computed_gap_ev is None:
            return None
        return float(self.computed_gap_ev - self.experimental_gap_ev)

    @property
    def clean(self) -> bool:
        """Is this comparison free of polymorph ambiguity?"""
        if self.computed_gap_ev is None:
            return False
        if self.polymorph_spread_ev is None:
            return True
        return self.polymorph_spread_ev <= config.POLYMORPH_GAP_SPREAD_TOLERANCE_EV

    @property
    def computed_predicts_metal(self) -> bool | None:
        if self.computed_gap_ev is None:
            return None
        return self.computed_gap_ev <= METAL_GAP_THRESHOLD_EV

    def to_dict(self) -> dict[str, Any]:
        return {
            "formula": self.formula,
            "experimental_gap_ev": self.experimental_gap_ev,
            "measured_as_metal": self.measured_as_metal,
            "computed_gap_ev": self.computed_gap_ev,
            "computed_material_id": self.computed_material_id,
            "computed_source": self.computed_source,
            "computed_functional": self.computed_functional,
            "signed_error_ev": (
                round(self.signed_error_ev, 4) if self.signed_error_ev is not None else None
            ),
            "polymorph_spread_ev": (
                round(self.polymorph_spread_ev, 4)
                if self.polymorph_spread_ev is not None
                else None
            ),
            "n_polymorphs": self.n_polymorphs,
            "clean_comparison": self.clean,
            "polymorph_spread_tolerance_ev": config.POLYMORPH_GAP_SPREAD_TOLERANCE_EV,
            "likely_mpid": self.likely_mpid,
            "likely_mpid_gap_ev": self.likely_mpid_gap_ev,
            "comparison_level": "composition, not structure matched",
            "notes": list(self.notes),
        }


def compare_gap(
    formula: str,
    experimental_gap_ev: float,
    computed_records: Sequence[PropertyRecord],
    likely_mpid: str | None = None,
) -> GapComparison:
    """Compare a measured gap against computed gaps for the same composition."""
    notes: list[str] = [
        "an experimental band gap is reported against a composition, so this "
        "comparison is composition-level and cannot be structure matched"
    ]
    gaps = polymorph_gaps_from_records(computed_records)
    ground = gaps.ground_state()
    by_id = gaps.by_mpid(likely_mpid)

    if ground is None:
        notes.append("no computed band gap available for this composition")
        return GapComparison(
            formula=Composition(formula).reduced_formula,
            experimental_gap_ev=float(experimental_gap_ev),
            measured_as_metal=experimental_gap_ev <= METAL_GAP_THRESHOLD_EV,
            computed_gap_ev=None,
            computed_material_id=None,
            computed_source=None,
            computed_functional=None,
            polymorph_spread_ev=gaps.spread,
            n_polymorphs=len(gaps.entries),
            likely_mpid=likely_mpid,
            likely_mpid_gap_ev=None,
            notes=notes,
        )

    spread = gaps.spread
    if spread is not None and spread > config.POLYMORPH_GAP_SPREAD_TOLERANCE_EV:
        notes.append(
            f"computed gaps for the {len(gaps.entries)} known polymorphs of this "
            f"composition span {spread:.2f} eV, above the "
            f"{config.POLYMORPH_GAP_SPREAD_TOLERANCE_EV} eV tolerance, so the "
            "measurement may not correspond to the polymorph compared here"
        )
    if by_id is not None and by_id is not ground:
        notes.append(
            f"the dataset associates this measurement with {likely_mpid} "
            f"(computed gap {by_id['band_gap']:.3f} eV) while the lowest energy "
            f"polymorph is {ground['material_id']} "
            f"(computed gap {ground['band_gap']:.3f} eV)"
        )

    return GapComparison(
        formula=Composition(formula).reduced_formula,
        experimental_gap_ev=float(experimental_gap_ev),
        measured_as_metal=experimental_gap_ev <= METAL_GAP_THRESHOLD_EV,
        computed_gap_ev=float(ground["band_gap"]),
        computed_material_id=ground["material_id"],
        computed_source=ground["source"],
        computed_functional=ground["functional"],
        polymorph_spread_ev=spread,
        n_polymorphs=len(gaps.entries),
        likely_mpid=likely_mpid,
        likely_mpid_gap_ev=float(by_id["band_gap"]) if by_id else None,
        notes=notes,
    )
