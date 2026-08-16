"""Physics-consistency checks.

Brief section 3.1C requires each flag to be individually reported, and section
3.1D forbids collapsing them into an opaque score. So every flag produced here
carries its own message and its own evidence dictionary containing the specific
numbers that triggered it. A reader who disagrees with a flag can see exactly
what it was computed from.

The six flags the brief names are marked ``core`` below. The additional flags
exist because the two databases expose different amounts of metadata, and
silently treating a derived value as a measured one would be a provenance claim
the project cannot support. They are reported alongside the core six rather than
folded into them.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Sequence

from . import config, hubbard
from .matching import GroupingResult, StructureGroup
from .records import (
    Functional,
    GGA_FAMILY,
    MagneticState,
    Property,
    PropertyRecord,
    Source,
)


class Severity(str, Enum):
    #: Recorded for completeness. Does not by itself undermine a comparison.
    INFO = "info"
    #: The comparison is still meaningful but a documented caveat applies.
    CAVEAT = "caveat"
    #: The comparison is compromised, or the sources genuinely disagree.
    WARNING = "warning"


class FlagCode(str, Enum):
    # The six the brief names.
    POLYMORPH_AMBIGUOUS = "POLYMORPH_AMBIGUOUS"
    FUNCTIONAL_MISMATCH = "FUNCTIONAL_MISMATCH"
    MAGNETIC_UNKNOWN = "MAGNETIC_UNKNOWN"
    MAGNETIC_MISMATCH = "MAGNETIC_MISMATCH"
    SINGLE_SOURCE = "SINGLE_SOURCE"
    HYPOTHETICAL = "HYPOTHETICAL"
    LARGE_DISAGREEMENT = "LARGE_DISAGREEMENT"

    # Additional flags, needed for honest provenance reporting.
    CORRECTION_SCHEME_MISMATCH = "CORRECTION_SCHEME_MISMATCH"
    FUNCTIONAL_INFERRED = "FUNCTIONAL_INFERRED"
    MAGNETIC_INFERRED = "MAGNETIC_INFERRED"
    STRUCTURE_UNAVAILABLE = "STRUCTURE_UNAVAILABLE"
    HUBBARD_U_MISMATCH = "HUBBARD_U_MISMATCH"


CORE_FLAGS: frozenset[FlagCode] = frozenset(
    {
        FlagCode.POLYMORPH_AMBIGUOUS,
        FlagCode.FUNCTIONAL_MISMATCH,
        FlagCode.MAGNETIC_UNKNOWN,
        FlagCode.MAGNETIC_MISMATCH,
        FlagCode.SINGLE_SOURCE,
        FlagCode.HYPOTHETICAL,
        FlagCode.LARGE_DISAGREEMENT,
    }
)


@dataclass(frozen=True)
class Flag:
    code: FlagCode
    severity: Severity
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def is_core(self) -> bool:
        return self.code in CORE_FLAGS

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "message": self.message,
            "evidence": self.evidence,
            "core_flag": self.is_core,
        }


# ---------------------------------------------------------------------------
# Spread statistics
# ---------------------------------------------------------------------------

@dataclass
class SourceValues:
    """All values one source reported for one material and property.

    A source can report several values for the same structure, typically because
    a database holds duplicate entries or several cell settings of one phase.
    That intra-source scatter is a different quantity from cross-source
    disagreement and is kept separate, because conflating them would inflate the
    apparent disagreement between databases.
    """

    source: Source
    values: list[float]
    record_ids: list[str]

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def representative(self) -> float:
        return float(statistics.median(self.values))

    @property
    def intra_spread(self) -> float:
        return float(max(self.values) - min(self.values)) if self.n > 1 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "n_values": self.n,
            "values": [round(v, 6) for v in self.values],
            "record_ids": self.record_ids,
            "representative_median": round(self.representative, 6),
            "intra_source_spread": round(self.intra_spread, 6),
        }


@dataclass
class Spread:
    property_name: Property
    units: str
    per_source: dict[Source, SourceValues]

    @property
    def n_sources(self) -> int:
        return len(self.per_source)

    @property
    def representatives(self) -> dict[Source, float]:
        return {s: v.representative for s, v in self.per_source.items()}

    @property
    def cross_source_spread(self) -> float | None:
        """Range across per-source representative values, or None if one source."""
        if self.n_sources < 2:
            return None
        reps = list(self.representatives.values())
        return float(max(reps) - min(reps))

    def signed_difference(self, a: Source, b: Source) -> float | None:
        reps = self.representatives
        if a not in reps or b not in reps:
            return None
        return float(reps[a] - reps[b])

    def to_dict(self) -> dict[str, Any]:
        return {
            "property": self.property_name.value,
            "units": self.units,
            "n_sources": self.n_sources,
            "per_source": {s.value: v.to_dict() for s, v in self.per_source.items()},
            "cross_source_spread": (
                round(self.cross_source_spread, 6)
                if self.cross_source_spread is not None
                else None
            ),
            "max_intra_source_spread": round(
                max((v.intra_spread for v in self.per_source.values()), default=0.0), 6
            ),
        }


def compute_spread(records: Sequence[PropertyRecord]) -> Spread:
    if not records:
        raise ValueError("cannot compute a spread over zero records")
    props = {r.property_name for r in records}
    if len(props) != 1:
        raise ValueError(f"records mix properties: {sorted(p.value for p in props)}")
    prop = props.pop()

    per_source: dict[Source, SourceValues] = {}
    for r in records:
        sv = per_source.get(r.source)
        if sv is None:
            per_source[r.source] = SourceValues(
                source=r.source, values=[float(r.value)], record_ids=[r.source_id]
            )
        else:
            sv.values.append(float(r.value))
            sv.record_ids.append(r.source_id)
    return Spread(
        property_name=prop, units=records[0].units, per_source=per_source
    )


def disagreement_threshold(prop: Property) -> float:
    if prop is Property.FORMATION_ENERGY_PER_ATOM:
        return config.LARGE_DISAGREEMENT_FORMATION_ENERGY_EV_PER_ATOM
    if prop is Property.BAND_GAP:
        return config.LARGE_DISAGREEMENT_BAND_GAP_EV
    raise ValueError(f"no threshold defined for {prop}")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_single_source(spread: Spread) -> list[Flag]:
    if spread.n_sources >= 2:
        return []
    only = next(iter(spread.per_source))
    return [
        Flag(
            code=FlagCode.SINGLE_SOURCE,
            severity=Severity.WARNING,
            message=(
                f"only {only.value} reports {spread.property_name.value} for this "
                "structure, so agreement is not measurable and no corroboration exists"
            ),
            evidence={"source": only.value, "n_sources": 1},
        )
    ]


def check_large_disagreement(spread: Spread) -> list[Flag]:
    value = spread.cross_source_spread
    if value is None:
        return []
    threshold = disagreement_threshold(spread.property_name)
    if value <= threshold:
        return []
    return [
        Flag(
            code=FlagCode.LARGE_DISAGREEMENT,
            severity=Severity.WARNING,
            message=(
                f"sources differ by {value:.4f} {spread.units}, above the documented "
                f"threshold of {threshold} {spread.units}"
            ),
            evidence={
                "cross_source_spread": round(value, 6),
                "threshold": threshold,
                "units": spread.units,
                "per_source_representative": {
                    s.value: round(v, 6) for s, v in spread.representatives.items()
                },
            },
        )
    ]


def check_functional_consistency(records: Sequence[PropertyRecord]) -> list[Flag]:
    """Brief section 2.3: only compare values from the same functional."""
    flags: list[Flag] = []
    by_source: dict[Source, set[Functional]] = {}
    for r in records:
        by_source.setdefault(r.source, set()).add(r.functional)

    all_functionals = {f for fs in by_source.values() for f in fs}

    # Anything outside the GGA family cannot be compared with anything inside it.
    outside = {f for f in all_functionals if f not in GGA_FAMILY}
    if len(all_functionals) > 1 and outside:
        flags.append(
            Flag(
                code=FlagCode.FUNCTIONAL_MISMATCH,
                severity=Severity.WARNING,
                message=(
                    "values were computed with functionals from different families "
                    f"({', '.join(sorted(f.value for f in all_functionals))}), which "
                    "do not measure the same quantity and must not be compared directly"
                ),
                evidence={
                    "functionals_by_source": {
                        s.value: sorted(f.value for f in fs) for s, fs in by_source.items()
                    }
                },
            )
        )
    elif len(all_functionals) > 1:
        flags.append(
            Flag(
                code=FlagCode.FUNCTIONAL_MISMATCH,
                severity=Severity.WARNING,
                message=(
                    "sources used different treatments within the GGA family "
                    f"({', '.join(sorted(f.value for f in all_functionals))}), so part "
                    "of any difference is attributable to the +U treatment rather "
                    "than to the underlying calculation"
                ),
                evidence={
                    "functionals_by_source": {
                        s.value: sorted(f.value for f in fs) for s, fs in by_source.items()
                    }
                },
            )
        )

    # Correction schemes differ between databases even when the functional
    # matches. Brief section 2.2 calls this a real finding to quantify.
    schemes = {r.correction_scheme for r in records}
    if len(schemes) > 1:
        flags.append(
            Flag(
                code=FlagCode.CORRECTION_SCHEME_MISMATCH,
                severity=Severity.CAVEAT,
                message=(
                    "sources applied different correction schemes to this property, "
                    "which produces a systematic offset independent of the "
                    "underlying calculation"
                ),
                evidence={
                    "schemes_by_source": {
                        r.source.value: r.correction_scheme for r in records
                    }
                },
            )
        )

    # Where a functional was inferred from published methodology rather than read
    # from per-entry metadata, say so.
    inferred = {
        r.source.value
        for r in records
        if "methodology" in str(r.extras.get("functional_determined_by", ""))
    }
    if inferred:
        flags.append(
            Flag(
                code=FlagCode.FUNCTIONAL_INFERRED,
                severity=Severity.CAVEAT,
                message=(
                    f"the functional for {', '.join(sorted(inferred))} was derived from "
                    "that database's published methodology because its API exposes no "
                    "per-entry functional field, so it is an inference and not a "
                    "retrieved value"
                ),
                evidence={"sources_with_inferred_functional": sorted(inferred)},
            )
        )
    return flags


def check_hubbard_u(records: Sequence[PropertyRecord]) -> list[Flag]:
    """Attribution: does the documented +U policy explain a difference here?"""
    sources = {r.source for r in records}
    if not {Source.MATERIALS_PROJECT, Source.OQMD} <= sources:
        return []
    comparison = hubbard.compare_hubbard_treatment(records[0].formula)
    if comparison.agrees:
        return []
    return [
        Flag(
            code=FlagCode.HUBBARD_U_MISMATCH,
            severity=Severity.WARNING,
            message=comparison.explanation(),
            evidence=comparison.to_dict(),
        )
    ]


def check_magnetic_consistency(records: Sequence[PropertyRecord]) -> list[Flag]:
    """Brief section 2.5: the same structure computed with different magnetic
    ordering gives different energies and gaps."""
    flags: list[Flag] = []
    states_by_source: dict[Source, set[MagneticState]] = {}
    for r in records:
        states_by_source.setdefault(r.source, set()).add(r.magnetic_state)

    unknown_sources = sorted(
        s.value
        for s, states in states_by_source.items()
        if MagneticState.UNKNOWN in states
    )
    if unknown_sources:
        flags.append(
            Flag(
                code=FlagCode.MAGNETIC_UNKNOWN,
                severity=Severity.CAVEAT,
                message=(
                    f"magnetic ordering is not known for {', '.join(unknown_sources)}, "
                    "so the comparison cannot establish that the same magnetic state "
                    "was computed"
                ),
                evidence={"sources_with_unknown_ordering": unknown_sources},
            )
        )

    known = {
        s: states
        for s, states in states_by_source.items()
        if not (states & {MagneticState.UNKNOWN, MagneticState.NOT_APPLICABLE})
    }
    distinct = {st for states in known.values() for st in states}
    if len(known) >= 2 and len(distinct) > 1:
        flags.append(
            Flag(
                code=FlagCode.MAGNETIC_MISMATCH,
                severity=Severity.WARNING,
                message=(
                    "sources report different magnetic orderings "
                    f"({', '.join(sorted(st.value for st in distinct))}) for the same "
                    "structure, which changes both energy and band gap and therefore "
                    "explains part of any difference"
                ),
                evidence={
                    "ordering_by_source": {
                        s.value: sorted(st.value for st in states)
                        for s, states in known.items()
                    },
                    "oqmd_afm_neglect_error_ev_per_atom": list(
                        hubbard.OQMD_AFM_NEGLECT_ERROR_EV_PER_ATOM
                    ),
                },
            )
        )

    inferred: dict[str, set[MagneticState]] = {}
    for r in records:
        if "methodology" in str(r.extras.get("magnetic_state_determined_by", "")):
            inferred.setdefault(r.source.value, set()).add(r.magnetic_state)
    if inferred:
        inferred_states = {st for states in inferred.values() for st in states}
        # An inferred non-magnetic state for a compound with no partially filled d
        # or f shell carries no physical risk: a spin-polarised calculation would
        # converge to the same answer. An inferred ferromagnetic state does carry
        # risk, because the true ordering may be antiferromagnetic and OQMD
        # documents a real energy error from neglecting that. Grade accordingly
        # rather than penalising every comparison against a database that exposes
        # no magnetic metadata.
        assumed_magnetic = inferred_states - {
            MagneticState.NON_MAGNETIC,
            MagneticState.NOT_APPLICABLE,
        }
        sources = ", ".join(sorted(inferred))
        if assumed_magnetic:
            message = (
                f"the magnetic state for {sources} was derived from published "
                "methodology as "
                + ", ".join(sorted(st.value for st in assumed_magnetic))
                + " rather than read from per-entry metadata, and the true ordering "
                "may be antiferromagnetic, which would change the energy"
            )
            severity = Severity.CAVEAT
        else:
            message = (
                f"the magnetic state for {sources} was derived from published "
                "methodology as non-magnetic, because the composition contains no "
                "3d element or actinide that database spin-polarises, rather than "
                "read from per-entry metadata"
            )
            severity = Severity.INFO
        flags.append(
            Flag(
                code=FlagCode.MAGNETIC_INFERRED,
                severity=severity,
                message=message,
                evidence={
                    "inferred_state_by_source": {
                        src: sorted(st.value for st in states)
                        for src, states in sorted(inferred.items())
                    },
                    "oqmd_afm_neglect_error_ev_per_atom": (
                        list(hubbard.OQMD_AFM_NEGLECT_ERROR_EV_PER_ATOM)
                        if assumed_magnetic
                        else None
                    ),
                },
            )
        )
    return flags


def check_hypothetical(records: Sequence[PropertyRecord]) -> list[Flag]:
    """Brief section 2.6 and 3.1C: is the structure experimentally observed?"""
    hypothetical = sorted(
        {r.label() for r in records if r.structure_is_icsd_derived is False}
    )
    unknown = sorted({r.label() for r in records if r.structure_is_icsd_derived is None})
    flags: list[Flag] = []
    if hypothetical and len(hypothetical) == len(records):
        flags.append(
            Flag(
                code=FlagCode.HYPOTHETICAL,
                severity=Severity.CAVEAT,
                message=(
                    "no source reports this structure as experimentally observed, so "
                    "it is a hypothetical structure and no measurement can corroborate it"
                ),
                evidence={"records": hypothetical},
            )
        )
    elif hypothetical:
        flags.append(
            Flag(
                code=FlagCode.HYPOTHETICAL,
                severity=Severity.INFO,
                message=(
                    "at least one source reports this structure as experimentally "
                    f"observed while {len(hypothetical)} record(s) carry no link to a "
                    "measured structure. Observation is a positive claim, so the "
                    "structure is treated as observed: a database lacking an ICSD "
                    "link is not evidence that the structure was never made"
                ),
                evidence={
                    "hypothetical_records": hypothetical,
                    "n_records": len(records),
                },
            )
        )
    if unknown:
        flags.append(
            Flag(
                code=FlagCode.HYPOTHETICAL,
                severity=Severity.INFO,
                message=(
                    "experimental observation status is unknown for "
                    f"{', '.join(unknown)}"
                ),
                evidence={"records_with_unknown_status": unknown},
            )
        )
    return flags


def check_polymorph_ambiguity(
    group: StructureGroup | None,
    unstructured: Sequence[PropertyRecord] = (),
    competing_groups: int = 0,
) -> list[Flag]:
    """Flag comparisons where structural identity could not be established."""
    flags: list[Flag] = []
    if unstructured:
        by_source = sorted({r.source.value for r in unstructured})
        flags.append(
            Flag(
                code=FlagCode.STRUCTURE_UNAVAILABLE,
                severity=Severity.WARNING,
                message=(
                    f"{len(unstructured)} record(s) from {', '.join(by_source)} carry no "
                    "structure, so they cannot be matched and are excluded from "
                    "structure-matched comparison"
                ),
                evidence={
                    "records": sorted(r.label() for r in unstructured),
                    "sources": by_source,
                },
            )
        )
        flags.append(
            Flag(
                code=FlagCode.POLYMORPH_AMBIGUOUS,
                severity=Severity.WARNING,
                message=(
                    "the composition has records whose structure is unavailable, so "
                    "formula-level identity cannot be promoted to structural identity"
                ),
                evidence={"n_records_without_structure": len(unstructured)},
            )
        )
    if competing_groups > 1:
        flags.append(
            Flag(
                code=FlagCode.POLYMORPH_AMBIGUOUS,
                severity=Severity.INFO,
                message=(
                    f"this composition has {competing_groups} distinct structures across "
                    "the sources, so a formula-level comparison would have merged "
                    "genuinely different materials"
                ),
                evidence={"n_distinct_structures": competing_groups},
            )
        )
    return flags


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_all_checks(
    records: Sequence[PropertyRecord],
    spread: Spread,
    unstructured: Sequence[PropertyRecord] = (),
    competing_groups: int = 0,
) -> list[Flag]:
    """Every check, each contributing its flags independently."""
    flags: list[Flag] = []
    flags.extend(check_single_source(spread))
    flags.extend(check_large_disagreement(spread))
    flags.extend(check_functional_consistency(records))
    flags.extend(check_hubbard_u(records))
    flags.extend(check_magnetic_consistency(records))
    flags.extend(check_hypothetical(records))
    flags.extend(
        check_polymorph_ambiguity(None, unstructured, competing_groups)
    )
    return flags


def flag_codes(flags: Iterable[Flag]) -> list[str]:
    return sorted({f.code.value for f in flags})
