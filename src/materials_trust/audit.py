"""Audit orchestration: fetch, match, compare, flag, explain.

Brief section 3.1D: for each material emit a structured record containing the
values, the spread, the flags, and a confidence band, and do not emit a single
opaque score with no explanation. The confidence band produced here is a label
derived by a published rule from quantities that travel alongside it, so a
reader can recompute it by hand. The rule is stated in
:func:`assess_confidence` and reproduced in the README.

No language model is involved anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Sequence

from pymatgen.core import Composition

from . import checks, matching
from .checks import Flag, FlagCode, Severity, Spread
from .records import Property, PropertyRecord, Source, SourceFailure
from .sources.materials_project import MaterialsProjectSource
from .sources.oqmd import OQMDSource


class ConfidenceBand(str, Enum):
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    NOT_ASSESSABLE = "not_assessable"


_LADDER = [ConfidenceBand.HIGH, ConfidenceBand.MODERATE, ConfidenceBand.LOW]

#: Warning-severity flags grouped by underlying cause, so that two flags
#: describing the same physical problem do not demote the band twice.
_DEMOTION_CATEGORIES: dict[str, set[FlagCode]] = {
    "functional_or_hubbard_u": {
        FlagCode.FUNCTIONAL_MISMATCH,
        FlagCode.HUBBARD_U_MISMATCH,
    },
    "magnetic": {FlagCode.MAGNETIC_MISMATCH},
    "structure": {FlagCode.POLYMORPH_AMBIGUOUS, FlagCode.STRUCTURE_UNAVAILABLE},
}

_CAPPING_FLAGS: set[FlagCode] = {
    FlagCode.MAGNETIC_UNKNOWN,
    FlagCode.FUNCTIONAL_INFERRED,
    FlagCode.MAGNETIC_INFERRED,
    FlagCode.CORRECTION_SCHEME_MISMATCH,
    FlagCode.HYPOTHETICAL,
}


@dataclass(frozen=True)
class ConfidenceAssessment:
    """A confidence band plus the full derivation that produced it."""

    band: ConfidenceBand
    steps: list[str]
    inputs: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "band": self.band.value,
            "derivation": list(self.steps),
            "inputs": dict(self.inputs),
            "rule": (
                "Base band from the ratio of cross-source spread to the documented "
                "threshold for the property: ratio <= 0.5 gives high, ratio <= 1.0 "
                "gives moderate, ratio > 1.0 gives low. The band is then demoted one "
                "step for each distinct category of warning-severity flag present "
                "(functional or Hubbard U, magnetic ordering, structural identity), "
                "and capped at moderate if any caveat-severity provenance flag "
                "applies. Informational flags carry context and never move the band. "
                "A comparison with fewer than two sources is not assessable rather "
                "than being assigned a band."
            ),
        }


def assess_confidence(spread: Spread, flags: Sequence[Flag]) -> ConfidenceAssessment:
    """Derive a confidence band by a published, reproducible rule."""
    steps: list[str] = []
    codes = {f.code for f in flags}
    # Severity decides whether a flag affects the band, because the same code is
    # raised at different severities for different situations. POLYMORPH_AMBIGUOUS
    # is a warning when a structure could not be matched, which genuinely weakens
    # the comparison, and informational when the composition merely has other
    # polymorphs that were correctly kept apart, which does not weaken this
    # structure-matched comparison at all.
    warning_codes = {f.code for f in flags if f.severity is Severity.WARNING}
    caveat_codes = {
        f.code for f in flags if f.severity in (Severity.WARNING, Severity.CAVEAT)
    }

    cross = spread.cross_source_spread
    if cross is None:
        return ConfidenceAssessment(
            band=ConfidenceBand.NOT_ASSESSABLE,
            steps=[
                f"only {spread.n_sources} source reports this property for this "
                "structure, so agreement cannot be measured and no band is assigned"
            ],
            inputs={"n_sources": spread.n_sources},
        )

    threshold = checks.disagreement_threshold(spread.property_name)
    ratio = cross / threshold if threshold else float("inf")
    if ratio <= 0.5:
        band = ConfidenceBand.HIGH
    elif ratio <= 1.0:
        band = ConfidenceBand.MODERATE
    else:
        band = ConfidenceBand.LOW
    steps.append(
        f"cross-source spread {cross:.4f} {spread.units} against threshold "
        f"{threshold} {spread.units} gives ratio {ratio:.2f}, base band {band.value}"
    )

    for category, category_codes in _DEMOTION_CATEGORIES.items():
        present = sorted(c.value for c in warning_codes & category_codes)
        if present:
            index = min(_LADDER.index(band) + 1, len(_LADDER) - 1)
            new_band = _LADDER[index]
            steps.append(
                f"demoted from {band.value} to {new_band.value} because of "
                f"{category} warning(s): {', '.join(present)}"
            )
            band = new_band

    caps = sorted(c.value for c in caveat_codes & _CAPPING_FLAGS)
    if caps and band is ConfidenceBand.HIGH:
        steps.append(
            "capped at moderate because provenance caveats apply: " + ", ".join(caps)
        )
        band = ConfidenceBand.MODERATE

    return ConfidenceAssessment(
        band=band,
        steps=steps,
        inputs={
            "cross_source_spread": round(cross, 6),
            "threshold": threshold,
            "units": spread.units,
            "ratio": round(ratio, 4),
            "flags": sorted(c.value for c in codes),
            "demoting_flags": sorted(
                c.value
                for c in warning_codes
                & set().union(*_DEMOTION_CATEGORIES.values())
            ),
            "capping_flags": sorted(c.value for c in caveat_codes & _CAPPING_FLAGS),
        },
    )


@dataclass
class TrustRecord:
    """The auditable unit of output: one property, one structure-matched material."""

    formula: str
    property_name: Property
    group_id: int
    structure_fingerprint: str | None
    records: list[PropertyRecord]
    spread: Spread
    flags: list[Flag]
    confidence: ConfidenceAssessment
    matcher_description: str

    @property
    def sources(self) -> list[str]:
        return sorted({r.source.value for r in self.records})

    def explanations(self) -> list[str]:
        """Deterministic explanations, taken from the flags themselves.

        These are produced by the core, not by a language model. Layer 3 may
        rephrase them but may not add quantitative content.
        """
        return [f.message for f in self.flags if f.severity is not Severity.INFO]

    def to_dict(self, include_structures: bool = False) -> dict[str, Any]:
        return {
            "formula": self.formula,
            "property": self.property_name.value,
            "group_id": self.group_id,
            "structure_fingerprint": self.structure_fingerprint,
            "sources": self.sources,
            "values": [r.to_dict(include_structure=include_structures) for r in self.records],
            "spread": self.spread.to_dict(),
            "flags": [f.to_dict() for f in self.flags],
            "core_flags": sorted({f.code.value for f in self.flags if f.is_core}),
            "confidence": self.confidence.to_dict(),
            "explanations": self.explanations(),
            "structure_matching": self.matcher_description,
        }


@dataclass
class Coverage:
    """Which sources actually produced data. Brief rule 5.3."""

    composition: str
    n_records_by_source: dict[str, int] = field(default_factory=dict)
    n_structures_by_source: dict[str, int] = field(default_factory=dict)
    n_distinct_structures: int = 0
    n_structure_matched_multi_source: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "composition": self.composition,
            "n_records_by_source": self.n_records_by_source,
            "n_structures_by_source": self.n_structures_by_source,
            "n_distinct_structures": self.n_distinct_structures,
            "n_structure_matched_multi_source": self.n_structure_matched_multi_source,
        }


@dataclass
class AuditResult:
    composition: str
    trust_records: list[TrustRecord]
    coverage: Coverage
    failures: list[SourceFailure]
    polymorph_ambiguous_records: list[PropertyRecord] = field(default_factory=list)

    def to_dict(self, include_structures: bool = False) -> dict[str, Any]:
        return {
            "composition": self.composition,
            "trust_records": [
                t.to_dict(include_structures=include_structures) for t in self.trust_records
            ],
            "coverage": self.coverage.to_dict(),
            "failures": [f.to_dict() for f in self.failures],
            "polymorph_ambiguous_records": [
                r.to_dict() for r in self.polymorph_ambiguous_records
            ],
        }

    def summary_line(self) -> str:
        bands: dict[str, int] = {}
        for t in self.trust_records:
            bands[t.confidence.band.value] = bands.get(t.confidence.band.value, 0) + 1
        band_text = ", ".join(f"{k}={v}" for k, v in sorted(bands.items())) or "none"
        return (
            f"{self.composition}: {len(self.trust_records)} trust record(s), "
            f"confidence {band_text}, "
            f"{self.coverage.n_structure_matched_multi_source} structure-matched "
            f"multi-source comparison(s), {len(self.failures)} failure(s)"
        )


class Auditor:
    """Cross-source audit for a composition.

    Deterministic: given the same cached payloads it produces byte-identical
    output, because grouping order, statistics, and flag generation are all fixed.
    """

    def __init__(
        self,
        mp: MaterialsProjectSource | None = None,
        oqmd: OQMDSource | None = None,
        properties: Iterable[Property] = (
            Property.FORMATION_ENERGY_PER_ATOM,
            Property.BAND_GAP,
        ),
    ) -> None:
        self.mp = mp if mp is not None else MaterialsProjectSource()
        self.oqmd = oqmd if oqmd is not None else OQMDSource()
        self.properties = list(properties)
        self.matcher = matching.build_matcher()

    def collect(self, composition: str) -> list[PropertyRecord]:
        reduced = Composition(composition).reduced_formula
        records: list[PropertyRecord] = []
        records.extend(self.mp.records_for(formula=reduced, properties=self.properties))
        records.extend(self.oqmd.records_for(reduced, properties=self.properties))
        return records

    def audit(self, composition: str) -> AuditResult:
        reduced = Composition(composition).reduced_formula
        # Source objects accumulate failures across calls, so snapshot the marks
        # and attribute only newly recorded failures to this audit. Without this
        # a long benchmark run would re-report every earlier failure on every
        # subsequent material.
        mp_mark = len(self.mp.failures)
        oqmd_mark = len(self.oqmd.failures)
        records = self.collect(reduced)

        coverage = Coverage(composition=reduced)
        for r in records:
            key = r.source.value
            coverage.n_records_by_source[key] = coverage.n_records_by_source.get(key, 0) + 1
            if r.has_structure:
                coverage.n_structures_by_source[key] = (
                    coverage.n_structures_by_source.get(key, 0) + 1
                )

        trust_records: list[TrustRecord] = []
        ambiguous: list[PropertyRecord] = []

        grouped_by_property = matching.group_by_property(records, self.matcher)
        for prop_name, grouping in grouped_by_property.items():
            prop = Property(prop_name)
            ambiguous.extend(grouping.unstructured)
            competing = len(grouping.groups)
            if prop is Property.FORMATION_ENERGY_PER_ATOM:
                coverage.n_distinct_structures = competing

            for group in grouping.groups:
                spread = checks.compute_spread(group.records)
                flags = checks.run_all_checks(
                    records=group.records,
                    spread=spread,
                    unstructured=[
                        r for r in grouping.unstructured if r.property_name is prop
                    ],
                    competing_groups=competing,
                )
                confidence = assess_confidence(spread, flags)
                trust_records.append(
                    TrustRecord(
                        formula=group.formula,
                        property_name=prop,
                        group_id=group.group_id,
                        structure_fingerprint=group.fingerprint(),
                        records=list(group.records),
                        spread=spread,
                        flags=flags,
                        confidence=confidence,
                        matcher_description=grouping.matcher_description,
                    )
                )
                if len(group.sources) >= 2 and prop is Property.FORMATION_ENERGY_PER_ATOM:
                    coverage.n_structure_matched_multi_source += 1

        failures = list(self.mp.failures[mp_mark:]) + list(self.oqmd.failures[oqmd_mark:])
        return AuditResult(
            composition=reduced,
            trust_records=trust_records,
            coverage=coverage,
            failures=failures,
            polymorph_ambiguous_records=ambiguous,
        )

    def audit_identifier(self, identifier: str) -> AuditResult:
        """Audit by composition or by Materials Project ID."""
        ident = identifier.strip()
        if ident.lower().startswith("mp-"):
            docs = self.mp.summary(material_ids=[ident])
            if not docs:
                raise ValueError(f"no Materials Project entry found for {ident!r}")
            formula = docs[0].get("formula_pretty")
            if not formula:
                raise ValueError(f"{ident!r} returned no formula")
            return self.audit(formula)
        return self.audit(ident)
