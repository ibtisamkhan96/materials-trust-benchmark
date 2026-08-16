"""Documented GGA+U and spin-polarisation methodology for each database.

This module is the attribution engine. Brief rule 5: the value is not "they
differ by 0.2 eV", it is "they differ by 0.2 eV because one used GGA+U and the
other did not". To say that, the project has to know each database's documented
+U policy, because neither database exposes a per-entry answer for every case.

Everything here is transcribed from primary documentation, with sources named so
a reader can check it:

Materials Project
    https://docs.materialsproject.org/methodology/materials-methodology/
    calculation-details/gga+u-calculations/hubbard-u-values
    "For oxides and fluorides containing any of the elements, only GGA+U
    calculations are performed." U is calibrated on oxides and reused for
    fluorides.

OQMD
    Kirklin et al., npj Computational Materials 1, 15010 (2015), Table 1, and
    https://www.oqmd.org/documentation/vasp
    "For several transition metals, lanthanides, and actinides, the GGA+U
    approach is implemented ... when in compounds with oxygen." Dudarev scheme,
    the single parameter being U minus J.
    Also: "Any calculation containing 3d (Sc-Cu) or actinide elements are
    spin-polarized with a ferromagnetic alignment of spins ... this approach
    will not capture more complex magnetic ordering, such as
    antiferromagnetism, which has been found to result in errors to the
    formation energy on the order of 10-20 meV/atom."

The consequence, and the reason this module earns its place, is that the two
policies genuinely differ in ways that are predictable before any data is
fetched. Fluorides get +U at Materials Project and not at OQMD. Copper oxides
get +U at OQMD and not at Materials Project. Molybdenum and tungsten oxides get
+U at Materials Project and not at OQMD. Where both apply +U, the U values still
differ. Each of those is a specific, checkable explanation for a disagreement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pymatgen.core import Composition, Element

# ---------------------------------------------------------------------------
# Materials Project
# ---------------------------------------------------------------------------

#: U values in eV, applied to the d orbitals. Calibrated on oxides and reused
#: unchanged for fluorides.
MP_U_EV: dict[str, float] = {
    "Co": 3.32,
    "Cr": 3.70,
    "Fe": 5.30,
    "Mn": 3.90,
    "Mo": 4.38,
    "Ni": 6.20,
    "V": 3.25,
    "W": 6.20,
}

#: Materials Project applies +U only when one of these anions is present.
MP_U_ANIONS: frozenset[str] = frozenset({"O", "F"})

MP_U_REFERENCE = (
    "Materials Project documentation, Hubbard U values "
    "(oxides and fluorides only)"
)

# ---------------------------------------------------------------------------
# OQMD
# ---------------------------------------------------------------------------

#: U minus J values in eV, Dudarev scheme.
OQMD_U_MINUS_J_EV: dict[str, float] = {
    "V": 3.1,
    "Cr": 3.5,
    "Mn": 3.8,
    "Fe": 4.0,
    "Co": 3.3,
    "Ni": 6.4,
    "Cu": 4.0,
    "Th": 4.0,
    "U": 4.0,
    "Np": 4.0,
    "Pu": 4.0,
}

#: OQMD applies +U only in compounds containing oxygen. Notably not fluorides.
OQMD_U_ANIONS: frozenset[str] = frozenset({"O"})

OQMD_U_REFERENCE = (
    "Kirklin et al., npj Comput. Mater. 1, 15010 (2015), Table 1; "
    "oqmd.org/documentation/vasp (oxides only)"
)

#: OQMD spin-polarises any calculation containing a 3d element or an actinide,
#: initialised ferromagnetically. Everything else is run non-spin-polarised.
OQMD_SPIN_POLARISED_3D: frozenset[str] = frozenset(
    {"Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu"}
)

OQMD_MAGNETIC_REFERENCE = "oqmd.org/documentation/vasp (ferromagnetic initialisation)"

#: The formation energy error OQMD itself attributes to not capturing
#: antiferromagnetic ordering. Quoted in the report as context for
#: disagreements involving magnetic materials.
OQMD_AFM_NEGLECT_ERROR_EV_PER_ATOM = (0.010, 0.020)


def _is_actinide(symbol: str) -> bool:
    try:
        return Element(symbol).is_actinoid
    except Exception:
        return False


def _elements(composition: Composition | str) -> list[str]:
    comp = Composition(composition) if isinstance(composition, str) else composition
    return [str(el) for el in comp.elements]


# ---------------------------------------------------------------------------
# Predicted treatment
# ---------------------------------------------------------------------------

def mp_expected_u(composition: Composition | str) -> dict[str, float]:
    """U values Materials Project is documented to apply to this composition."""
    els = _elements(composition)
    if not (MP_U_ANIONS & set(els)):
        return {}
    return {el: MP_U_EV[el] for el in els if el in MP_U_EV}


def oqmd_expected_u(composition: Composition | str) -> dict[str, float]:
    """U minus J values OQMD is documented to apply to this composition."""
    els = _elements(composition)
    if not (OQMD_U_ANIONS & set(els)):
        return {}
    return {el: OQMD_U_MINUS_J_EV[el] for el in els if el in OQMD_U_MINUS_J_EV}


def oqmd_expected_spin_polarised(composition: Composition | str) -> bool:
    els = _elements(composition)
    return any(el in OQMD_SPIN_POLARISED_3D or _is_actinide(el) for el in els)


@dataclass(frozen=True)
class HubbardComparison:
    """Predicted difference in +U treatment between the two databases.

    ``agrees`` means both databases apply +U to the same elements with the same
    parameter, or neither applies it. Anything else is a documented reason for
    the formation energies to sit on different scales.
    """

    formula: str
    mp_u: dict[str, float]
    oqmd_u: dict[str, float]
    only_mp: dict[str, float] = field(default_factory=dict)
    only_oqmd: dict[str, float] = field(default_factory=dict)
    differing_values: dict[str, tuple[float, float]] = field(default_factory=dict)

    @property
    def agrees(self) -> bool:
        return not (self.only_mp or self.only_oqmd or self.differing_values)

    @property
    def either_applies_u(self) -> bool:
        return bool(self.mp_u or self.oqmd_u)

    def explanation(self) -> str:
        """Plain language attribution, built from the tables. No model involved."""
        if not self.either_applies_u:
            return (
                "Neither database applies a Hubbard U correction to this "
                "composition, so +U is not an available explanation for any "
                "disagreement between them."
            )
        parts: list[str] = []
        if self.only_mp:
            listed = ", ".join(f"{el} (U = {u} eV)" for el, u in sorted(self.only_mp.items()))
            parts.append(
                f"Materials Project applies +U to {listed} and OQMD does not. "
                "Materials Project applies +U to both oxides and fluorides, "
                "while OQMD applies it only to compounds containing oxygen, and "
                "the two databases cover different element sets."
            )
        if self.only_oqmd:
            listed = ", ".join(
                f"{el} (U minus J = {u} eV)" for el, u in sorted(self.only_oqmd.items())
            )
            parts.append(
                f"OQMD applies +U to {listed} and Materials Project does not."
            )
        if self.differing_values:
            listed = ", ".join(
                f"{el} (Materials Project {mp} eV, OQMD {oq} eV)"
                for el, (mp, oq) in sorted(self.differing_values.items())
            )
            parts.append(
                f"Both databases apply +U but with different parameters for {listed}. "
                "Note that Materials Project quotes U while OQMD quotes U minus J "
                "in the Dudarev scheme, so the values are not directly "
                "interchangeable even where they look similar."
            )
        if not parts:
            shared = ", ".join(sorted(self.mp_u))
            return (
                f"Both databases apply +U to {shared} with matching parameters, so "
                "+U treatment is not a source of disagreement here."
            )
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "formula": self.formula,
            "mp_u": self.mp_u,
            "oqmd_u_minus_j": self.oqmd_u,
            "only_mp": self.only_mp,
            "only_oqmd": self.only_oqmd,
            "differing_values": {k: list(v) for k, v in self.differing_values.items()},
            "agrees": self.agrees,
            "explanation": self.explanation(),
            "references": [MP_U_REFERENCE, OQMD_U_REFERENCE],
        }


def compare_hubbard_treatment(composition: Composition | str) -> HubbardComparison:
    comp = Composition(composition) if isinstance(composition, str) else composition
    mp_u = mp_expected_u(comp)
    oqmd_u = oqmd_expected_u(comp)

    only_mp = {el: u for el, u in mp_u.items() if el not in oqmd_u}
    only_oqmd = {el: u for el, u in oqmd_u.items() if el not in mp_u}
    differing = {
        el: (mp_u[el], oqmd_u[el])
        for el in set(mp_u) & set(oqmd_u)
        if abs(mp_u[el] - oqmd_u[el]) > 1e-9
    }
    return HubbardComparison(
        formula=comp.reduced_formula,
        mp_u=mp_u,
        oqmd_u=oqmd_u,
        only_mp=only_mp,
        only_oqmd=only_oqmd,
        differing_values=differing,
    )
