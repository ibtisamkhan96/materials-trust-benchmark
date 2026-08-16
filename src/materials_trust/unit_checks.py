"""Unit and sign sanity checks for formation energies.

Brief section 2.2: normalise units, because some sources report per formula unit
or in kJ/mol. A missed conversion here would not crash anything. It would
silently produce a benchmark in which one database appears to disagree with
another by a factor of two or three, and the headline finding would be an
arithmetic error dressed up as a physical result. That is the failure this
module exists to make impossible.

The check does not merely test whether a value is close to a literature number.
It tests the competing unit hypotheses against each other. For a compound with
``n`` atoms per formula unit, a value reported in eV per formula unit would be
``n`` times the per atom value, and a value in kJ/mol would be 96.485 times the
value in eV. Those hypotheses are numerically distinguishable, so the harness
identifies which one the data is consistent with rather than assuming.

Reference data are experimental standard formation enthalpies at 298 K. They are
used only to fix the scale, never as the accuracy benchmark: a DFT formation
energy at 0 K without zero point energy is not the same quantity as an
experimental enthalpy at 298 K, and the two legitimately differ by tens of meV
per atom. The tolerance is therefore deliberately loose, wide enough to absorb
that physical difference and still far narrower than a factor of two.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from pymatgen.core import Composition

#: Joules per mole in one electronvolt per particle.
KJ_PER_MOL_PER_EV = 96.485

#: Experimental standard formation enthalpies at 298 K in kJ/mol per formula
#: unit, from standard thermochemical tables (CRC Handbook, NIST-JANAF).
#: Chosen to span ionic halides, simple oxides, and a transition metal oxide, so
#: that a unit error cannot hide in one chemistry.
LITERATURE_FORMATION_ENTHALPY_KJ_PER_MOL: dict[str, float] = {
    "NaCl": -411.12,
    "NaF": -576.6,
    "LiF": -616.0,
    "MgO": -601.6,
    "CaO": -634.9,
    "ZnO": -350.5,
    "Al2O3": -1675.7,
    "TiO2": -944.0,
    "SiO2": -910.7,
    "Fe2O3": -824.2,
}

#: How far a DFT formation energy may sit from the experimental enthalpy before
#: the harness treats the discrepancy as something other than ordinary DFT
#: error. OQMD's own published mean absolute error against experiment is
#: 0.096 eV/atom, so 0.5 eV/atom leaves generous room while still being far
#: below the smallest unit error the check needs to catch, a factor of two.
UNIT_CHECK_TOLERANCE_EV_PER_ATOM = 0.5


def literature_ev_per_atom(formula: str) -> float | None:
    """Experimental formation enthalpy converted to eV/atom."""
    kj = LITERATURE_FORMATION_ENTHALPY_KJ_PER_MOL.get(
        Composition(formula).reduced_formula
    )
    if kj is None:
        return None
    comp = Composition(formula).reduced_composition
    n_atoms = comp.num_atoms
    return (kj / KJ_PER_MOL_PER_EV) / n_atoms


@dataclass(frozen=True)
class UnitVerdict:
    formula: str
    reported_value: float
    literature_ev_per_atom: float
    atoms_per_formula_unit: float
    consistent_hypotheses: list[str]
    verdict: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "formula": self.formula,
            "reported_value": self.reported_value,
            "literature_ev_per_atom": round(self.literature_ev_per_atom, 4),
            "atoms_per_formula_unit": self.atoms_per_formula_unit,
            "consistent_hypotheses": self.consistent_hypotheses,
            "verdict": self.verdict,
            "passed": self.passed,
            "detail": self.detail,
        }


def classify_units(formula: str, reported_value: float) -> UnitVerdict:
    """Decide which unit hypothesis a reported formation energy is consistent with."""
    lit = literature_ev_per_atom(formula)
    if lit is None:
        raise KeyError(f"no literature formation enthalpy recorded for {formula!r}")
    n_atoms = Composition(formula).reduced_composition.num_atoms

    hypotheses: dict[str, float] = {
        "eV/atom": lit,
        "eV per formula unit": lit * n_atoms,
        "kJ/mol per atom": lit * KJ_PER_MOL_PER_EV,
        "kJ/mol per formula unit": lit * KJ_PER_MOL_PER_EV * n_atoms,
    }

    consistent = [
        name
        for name, expected in hypotheses.items()
        # Scale the tolerance with the hypothesis so that a per formula unit
        # hypothesis is judged on its own scale rather than being trivially
        # excluded by an absolute eV/atom tolerance.
        if abs(reported_value - expected)
        <= UNIT_CHECK_TOLERANCE_EV_PER_ATOM * max(1.0, abs(expected / lit))
    ]

    if consistent == ["eV/atom"]:
        verdict = "eV/atom"
        passed = True
        detail = (
            f"reported {reported_value:+.4f} against an experimental "
            f"{lit:+.4f} eV/atom, and inconsistent with every other unit hypothesis"
        )
    elif "eV/atom" in consistent:
        verdict = "ambiguous"
        passed = False
        detail = (
            "value is consistent with eV/atom but also with "
            f"{[h for h in consistent if h != 'eV/atom']}, so the check cannot "
            "discriminate for this compound"
        )
    elif consistent:
        verdict = consistent[0]
        passed = False
        detail = (
            f"reported {reported_value:+.4f} is not consistent with eV/atom "
            f"({lit:+.4f}) but is consistent with {consistent}"
        )
    else:
        verdict = "unrecognised"
        passed = False
        detail = (
            f"reported {reported_value:+.4f} matches no unit hypothesis; expected "
            f"{lit:+.4f} eV/atom. This is not a unit error but a wrong value, a "
            "mismatched structure, or a different reference state"
        )

    return UnitVerdict(
        formula=Composition(formula).reduced_formula,
        reported_value=float(reported_value),
        literature_ev_per_atom=lit,
        atoms_per_formula_unit=float(n_atoms),
        consistent_hypotheses=consistent,
        verdict=verdict,
        passed=passed,
        detail=detail,
    )


class UnitCheckFailure(RuntimeError):
    """Raised to stop a benchmark run whose units cannot be trusted."""


@dataclass
class UnitCheckReport:
    verdicts: list[UnitVerdict]
    source: str

    @property
    def passed(self) -> bool:
        # Ambiguous compounds are tolerated individually. What must hold is that
        # every compound is consistent with eV/atom, and at least one compound
        # discriminates eV/atom uniquely.
        if not self.verdicts:
            return False
        all_consistent = all("eV/atom" in v.consistent_hypotheses for v in self.verdicts)
        any_decisive = any(v.verdict == "eV/atom" for v in self.verdicts)
        return all_consistent and any_decisive

    def failures(self) -> list[UnitVerdict]:
        return [v for v in self.verdicts if "eV/atom" not in v.consistent_hypotheses]

    def raise_if_failed(self) -> None:
        if self.passed:
            return
        lines = [f"unit check failed for {self.source}:"]
        for v in self.failures() or self.verdicts:
            lines.append(f"  {v.formula}: {v.detail}")
        raise UnitCheckFailure("\n".join(lines))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "passed": self.passed,
            "n_compounds": len(self.verdicts),
            "n_decisive": sum(1 for v in self.verdicts if v.verdict == "eV/atom"),
            "tolerance_ev_per_atom": UNIT_CHECK_TOLERANCE_EV_PER_ATOM,
            "verdicts": [v.to_dict() for v in self.verdicts],
        }


def check_sign_convention(values: Sequence[tuple[str, float]]) -> list[str]:
    """Stable compounds must have negative formation energies.

    A sign flip is the other silent catastrophe available in this kind of
    pipeline, and it is trivially detectable: every compound in the reference
    list is thermodynamically stable with respect to its elements.
    """
    problems: list[str] = []
    for formula, value in values:
        if value >= 0:
            problems.append(
                f"{formula} reported a formation energy of {value:+.4f} eV/atom. "
                "Every compound in the reference set is stable with respect to its "
                "elements, so a non-negative value indicates a sign convention error"
            )
    return problems
