"""Structure matching. The part that decides what counts as the same material.

Brief section 2.1: two entries with the same chemical formula are not
necessarily the same material. TiO2 is rutile, anatase, and brookite, with
genuinely different properties. Reporting a rutile-versus-anatase energy
difference as "the databases disagree" would be a fabricated disagreement and
would invalidate the whole benchmark.

So nothing in this project compares two values until their structures have been
matched here, and no code path merges records on formula. Where a structure is
missing or a match cannot be established, the comparison is recorded as
polymorph-ambiguous and reported separately.

Tolerances are the pymatgen defaults, recorded in ``config.py``. They are
deliberately not loosened. A looser matcher would merge distinct polymorphs and
produce false agreement, which is the worst failure mode available here.

Determinism note: structural similarity is not a transitive relation, so a
greedy clustering can in principle depend on the order in which records are
considered. Records are therefore sorted by source and identifier before
clustering, which makes the grouping reproducible for a given input set. Brief
section 3.3 requires the core to produce the same output for the same input
every time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Sequence

from pymatgen.analysis.structure_matcher import ElementComparator, StructureMatcher
from pymatgen.core import Structure

from . import config
from .records import PropertyRecord, Source


def build_matcher() -> StructureMatcher:
    """The single matcher configuration used everywhere in the project.

    ``primitive_cell=True`` reduces both structures before comparison, which is
    what lets a two atom NaCl primitive cell match a sixteen atom conventional
    cell of the same phase. ``scale=True`` normalises cell volume, which is
    necessary because different codes and settings relax to slightly different
    lattice constants for the same phase. ``ElementComparator`` matches on
    element rather than on oxidation-state-decorated species, because the two
    databases do not agree on whether to decorate structures and an oxidation
    state is an interpretation rather than a measurement.
    """
    return StructureMatcher(
        ltol=config.STRUCTURE_MATCH_LTOL,
        stol=config.STRUCTURE_MATCH_STOL,
        angle_tol=config.STRUCTURE_MATCH_ANGLE_TOL,
        primitive_cell=True,
        scale=True,
        attempt_supercell=False,
        comparator=ElementComparator(),
    )


MATCHER_DESCRIPTION = (
    f"pymatgen StructureMatcher(ltol={config.STRUCTURE_MATCH_LTOL}, "
    f"stol={config.STRUCTURE_MATCH_STOL}, "
    f"angle_tol={config.STRUCTURE_MATCH_ANGLE_TOL}, primitive_cell=True, "
    "scale=True, comparator=ElementComparator)"
)


class MatchOutcome(str, Enum):
    MATCHED = "matched"
    DIFFERENT_STRUCTURE = "different_structure"
    STRUCTURE_UNAVAILABLE = "structure_unavailable"


@dataclass(frozen=True)
class MatchResult:
    outcome: MatchOutcome
    rms: float | None = None
    max_site_distance: float | None = None
    detail: str = ""

    @property
    def matched(self) -> bool:
        return self.outcome is MatchOutcome.MATCHED

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "rms": self.rms,
            "max_site_distance": self.max_site_distance,
            "detail": self.detail,
        }


def compare_structures(
    a: Structure | None,
    b: Structure | None,
    matcher: StructureMatcher | None = None,
) -> MatchResult:
    """Establish whether two structures describe the same material."""
    if a is None or b is None:
        missing = "both" if a is None and b is None else ("first" if a is None else "second")
        return MatchResult(
            outcome=MatchOutcome.STRUCTURE_UNAVAILABLE,
            detail=f"structure unavailable ({missing}), so identity cannot be established",
        )
    matcher = matcher or build_matcher()
    try:
        rms_result = matcher.get_rms_dist(a, b)
    except Exception as exc:
        return MatchResult(
            outcome=MatchOutcome.STRUCTURE_UNAVAILABLE,
            detail=f"matcher raised {type(exc).__name__}: {exc}",
        )
    if rms_result is None:
        return MatchResult(
            outcome=MatchOutcome.DIFFERENT_STRUCTURE,
            detail=(
                "structures did not match within tolerance, so they are distinct "
                "polymorphs or otherwise different materials"
            ),
        )
    rms, max_dist = rms_result
    return MatchResult(
        outcome=MatchOutcome.MATCHED,
        rms=float(rms),
        max_site_distance=float(max_dist),
        detail="structures matched within tolerance",
    )


@dataclass
class StructureGroup:
    """A set of records established to describe the same material.

    ``representative`` is simply the first record in deterministic order. It
    carries no special authority; it exists so that new candidates have
    something to be compared against.
    """

    group_id: int
    records: list[PropertyRecord] = field(default_factory=list)
    match_details: dict[str, MatchResult] = field(default_factory=dict)

    @property
    def representative(self) -> PropertyRecord:
        return self.records[0]

    @property
    def structure(self) -> Structure | None:
        return self.representative.structure

    @property
    def sources(self) -> set[Source]:
        return {r.source for r in self.records}

    @property
    def formula(self) -> str:
        return self.representative.reduced_formula

    def fingerprint(self) -> str | None:
        return self.representative.structure_fingerprint()

    def records_by_source(self) -> dict[Source, list[PropertyRecord]]:
        out: dict[Source, list[PropertyRecord]] = {}
        for r in self.records:
            out.setdefault(r.source, []).append(r)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "formula": self.formula,
            "structure_fingerprint": self.fingerprint(),
            "sources": sorted(s.value for s in self.sources),
            "n_records": len(self.records),
            "match_details": {k: v.to_dict() for k, v in self.match_details.items()},
        }


def _sort_key(record: PropertyRecord) -> tuple[str, str]:
    return (record.source.value, record.source_id)


@dataclass
class GroupingResult:
    groups: list[StructureGroup]
    unstructured: list[PropertyRecord]
    matcher_description: str = MATCHER_DESCRIPTION

    def to_dict(self) -> dict[str, Any]:
        return {
            "matcher": self.matcher_description,
            "n_groups": len(self.groups),
            "n_records_without_structure": len(self.unstructured),
            "groups": [g.to_dict() for g in self.groups],
        }


def group_records(
    records: Iterable[PropertyRecord],
    matcher: StructureMatcher | None = None,
) -> GroupingResult:
    """Cluster records into structural equivalence classes.

    Records without a structure are never placed in a group. They are returned
    separately so the caller can flag them as polymorph-ambiguous, because an
    unstructured record cannot be shown to be the same material as anything.
    """
    matcher = matcher or build_matcher()
    ordered = sorted(records, key=_sort_key)

    groups: list[StructureGroup] = []
    unstructured: list[PropertyRecord] = []

    for record in ordered:
        if not record.has_structure:
            unstructured.append(record)
            continue
        placed = False
        for group in groups:
            result = compare_structures(group.structure, record.structure, matcher)
            if result.matched:
                group.records.append(record)
                group.match_details[record.label()] = result
                placed = True
                break
        if not placed:
            new_group = StructureGroup(group_id=len(groups), records=[record])
            new_group.match_details[record.label()] = MatchResult(
                outcome=MatchOutcome.MATCHED,
                rms=0.0,
                max_site_distance=0.0,
                detail="group representative",
            )
            groups.append(new_group)

    return GroupingResult(groups=groups, unstructured=unstructured)


def group_by_property(
    records: Sequence[PropertyRecord],
    matcher: StructureMatcher | None = None,
) -> dict[str, GroupingResult]:
    """Group separately for each property.

    Grouping is done per property because the set of sources reporting a
    formation energy is not the same as the set reporting a band gap, and mixing
    them would make a group look better corroborated than it is.
    """
    matcher = matcher or build_matcher()
    by_prop: dict[str, list[PropertyRecord]] = {}
    for r in records:
        by_prop.setdefault(r.property_name.value, []).append(r)
    return {prop: group_records(rs, matcher) for prop, rs in sorted(by_prop.items())}
