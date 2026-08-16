"""The shared record type carried across every data source.

Brief section 2.6: a value without provenance cannot be audited and must not be
reported. That rule is enforced here by construction. ``PropertyRecord`` cannot
be instantiated without its provenance fields, and the validation in
``__post_init__`` rejects records that are internally inconsistent, that carry
values outside physically possible ranges, or that declare units the property
does not use.

The point of putting the enforcement in the type rather than in a downstream
check is that no code path in the project can produce an unprovenanced number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pymatgen.core import Composition, Structure

from . import config


class ProvenanceError(ValueError):
    """Raised when a record is missing or misdeclares its provenance."""


class Source(str, Enum):
    MATERIALS_PROJECT = "materials_project"
    OQMD = "oqmd"
    EXPERIMENT = "experiment"


class Property(str, Enum):
    FORMATION_ENERGY_PER_ATOM = "formation_energy_per_atom"
    BAND_GAP = "band_gap"


EXPECTED_UNITS: dict[Property, str] = {
    Property.FORMATION_ENERGY_PER_ATOM: "eV/atom",
    Property.BAND_GAP: "eV",
}


class Functional(str, Enum):
    """Exchange-correlation treatment behind a computed value.

    ``PBE_OR_PBE_PLUS_U_UNRESOLVED`` is a deliberate, honest category. OQMD's
    REST API does not expose per-entry functional metadata, and OQMD applies +U
    to a documented subset of chemistries. Claiming plain PBE for every OQMD
    entry would be a fabricated provenance claim, so the unresolved case gets
    its own value and is flagged downstream.
    """

    PBE = "PBE"
    PBE_PLUS_U = "PBE+U"
    R2SCAN = "r2SCAN"
    PBE_OR_PBE_PLUS_U_UNRESOLVED = "PBE_or_PBE+U_unresolved"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


#: Functionals that sit within the GGA family and are therefore in principle
#: comparable to one another, subject to the +U caveat handled in checks.py.
GGA_FAMILY: frozenset[Functional] = frozenset(
    {
        Functional.PBE,
        Functional.PBE_PLUS_U,
        Functional.PBE_OR_PBE_PLUS_U_UNRESOLVED,
    }
)


class MagneticState(str, Enum):
    FERROMAGNETIC = "FM"
    ANTIFERROMAGNETIC = "AFM"
    FERRIMAGNETIC = "FiM"
    NON_MAGNETIC = "NM"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class ValueKind(str, Enum):
    COMPUTED = "computed"
    MEASURED = "measured"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class PropertyRecord:
    """One property value from one source, with the provenance to audit it.

    Field mapping to the brief's suggested shared record type:

    ==========================  =====================================
    brief field                 field here
    ==========================  =====================================
    source                      ``source``
    id                          ``source_id``
    formula                     ``formula``
    structure                   ``structure``
    property                    ``property_name``
    value                       ``value``
    units                       ``units``
    functional                  ``functional``
    correction scheme           ``correction_scheme``
    magnetic state              ``magnetic_state``
    is_experimental             ``structure_is_icsd_derived``
    ==========================  =====================================

    ``is_experimental`` is split into two fields here because the brief uses it
    for one thing (is the structure experimentally observed, i.e. ICSD derived)
    while the pipeline also needs to know a different thing (is the value itself
    a measurement rather than a calculation). Conflating them would let a
    measured band gap be compared against a computed one without any flag being
    raised, which rule 2.4 forbids. So structure provenance lives in
    ``structure_is_icsd_derived`` and value provenance in ``value_kind``.
    """

    source: Source
    source_id: str
    formula: str
    property_name: Property
    value: float
    units: str
    functional: Functional
    correction_scheme: str
    magnetic_state: MagneticState
    value_kind: ValueKind
    structure_is_icsd_derived: bool | None
    structure: Structure | None = None
    retrieved_at: str = field(default_factory=_utc_now_iso)
    source_url: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._validate_provenance()
        self._validate_value()
        self._validate_consistency()

    # -- validation ---------------------------------------------------------

    def _validate_provenance(self) -> None:
        if not isinstance(self.source, Source):
            raise ProvenanceError(f"source must be a Source enum, got {self.source!r}")
        if not isinstance(self.property_name, Property):
            raise ProvenanceError(
                f"property_name must be a Property enum, got {self.property_name!r}"
            )
        if not isinstance(self.functional, Functional):
            raise ProvenanceError(
                f"functional must be a Functional enum, got {self.functional!r}. "
                "An unknown functional must be declared as Functional.UNKNOWN, "
                "never omitted."
            )
        if not isinstance(self.magnetic_state, MagneticState):
            raise ProvenanceError(
                f"magnetic_state must be a MagneticState enum, got {self.magnetic_state!r}. "
                "An unknown magnetic state must be declared as MagneticState.UNKNOWN."
            )
        if not isinstance(self.value_kind, ValueKind):
            raise ProvenanceError(
                f"value_kind must be a ValueKind enum, got {self.value_kind!r}"
            )
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ProvenanceError("source_id is required and must be a non-empty string")
        if not isinstance(self.formula, str) or not self.formula.strip():
            raise ProvenanceError("formula is required and must be a non-empty string")
        if not isinstance(self.correction_scheme, str) or not self.correction_scheme.strip():
            raise ProvenanceError(
                "correction_scheme is required. Use the string 'none' to state "
                "explicitly that no correction was applied; an empty value is "
                "not an acceptable substitute for knowing."
            )
        try:
            Composition(self.formula)
        except Exception as exc:  # pragma: no cover - depends on pymatgen internals
            raise ProvenanceError(f"formula {self.formula!r} does not parse: {exc}") from exc
        if self.structure is not None and not isinstance(self.structure, Structure):
            raise ProvenanceError("structure must be a pymatgen Structure or None")

    def _validate_value(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise ProvenanceError(f"value must be a real number, got {self.value!r}")
        if not math.isfinite(float(self.value)):
            raise ProvenanceError(f"value must be finite, got {self.value!r}")

        expected_units = EXPECTED_UNITS[self.property_name]
        if self.units != expected_units:
            raise ProvenanceError(
                f"{self.property_name.value} must be reported in {expected_units!r}, "
                f"got {self.units!r}. Convert at the source module boundary, not here."
            )

        if self.property_name is Property.BAND_GAP:
            if self.value < 0:
                raise ProvenanceError(
                    f"band gap of {self.value} eV is negative and not physical"
                )
            if self.value > config.PHYSICAL_BAND_GAP_BOUND_EV:
                raise ProvenanceError(
                    f"band gap of {self.value} eV exceeds the plausibility bound of "
                    f"{config.PHYSICAL_BAND_GAP_BOUND_EV} eV, which indicates a "
                    "parsing or unit error rather than a real value"
                )
        elif self.property_name is Property.FORMATION_ENERGY_PER_ATOM:
            bound = config.PHYSICAL_FORMATION_ENERGY_BOUND_EV_PER_ATOM
            if abs(self.value) > bound:
                raise ProvenanceError(
                    f"formation energy of {self.value} eV/atom is outside the "
                    f"plausibility bound of +/-{bound} eV/atom. This usually means "
                    "the source reported per formula unit or in kJ/mol and the "
                    "conversion was missed."
                )

    def _validate_consistency(self) -> None:
        if self.value_kind is ValueKind.MEASURED:
            if self.functional is not Functional.NOT_APPLICABLE:
                raise ProvenanceError(
                    "a measured value cannot carry an exchange-correlation "
                    "functional; set Functional.NOT_APPLICABLE"
                )
        else:
            if self.functional is Functional.NOT_APPLICABLE:
                raise ProvenanceError(
                    "a computed value must declare a functional, or "
                    "Functional.UNKNOWN if the source does not expose it"
                )

    # -- derived ------------------------------------------------------------

    @property
    def reduced_formula(self) -> str:
        return Composition(self.formula).reduced_formula

    @property
    def has_structure(self) -> bool:
        return self.structure is not None

    @property
    def uses_hubbard_u(self) -> bool | None:
        """True, False, or None when the source does not let us tell."""
        if self.functional is Functional.PBE_PLUS_U:
            return True
        if self.functional is Functional.PBE:
            return False
        return None

    def structure_fingerprint(self) -> str | None:
        """A short human-checkable structure descriptor for the report.

        This is a description, never a matching criterion. Structural identity
        is established only by StructureMatcher in matching.py.
        """
        if self.structure is None:
            return None
        try:
            spg = self.structure.get_space_group_info(symprec=0.1)
            return f"{self.reduced_formula} {spg[0]} (#{spg[1]}) n={len(self.structure)}"
        except Exception:
            return f"{self.reduced_formula} symmetry-undetermined n={len(self.structure)}"

    def label(self) -> str:
        return f"{self.source.value}:{self.source_id}"

    def to_dict(self, include_structure: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": self.source.value,
            "source_id": self.source_id,
            "formula": self.formula,
            "reduced_formula": self.reduced_formula,
            "property": self.property_name.value,
            "value": float(self.value),
            "units": self.units,
            "functional": self.functional.value,
            "correction_scheme": self.correction_scheme,
            "magnetic_state": self.magnetic_state.value,
            "value_kind": self.value_kind.value,
            "structure_is_icsd_derived": self.structure_is_icsd_derived,
            "structure_available": self.has_structure,
            "structure_fingerprint": self.structure_fingerprint(),
            "retrieved_at": self.retrieved_at,
            "source_url": self.source_url,
            "extras": dict(self.extras),
        }
        if include_structure and self.structure is not None:
            out["structure"] = self.structure.as_dict()
        return out


@dataclass(frozen=True)
class SourceFailure:
    """A recorded retrieval failure.

    Brief rule 5.1: if an API fails, the pipeline reports the failure and never
    fabricates a number to fill the gap. Failures are first-class objects so
    that coverage statistics can distinguish "this source has no value for this
    material" from "we could not reach this source".
    """

    source: Source
    query: str
    reason: str
    occurred_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "query": self.query,
            "reason": self.reason,
            "occurred_at": self.occurred_at,
        }
