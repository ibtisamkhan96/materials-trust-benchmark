"""Golden tests encoding the brief's non-negotiable physics rules.

These are regression tests for correctness claims, not coverage decoration. Each
one corresponds to a rule in brief section 2, and if any of them fails the
benchmark's central claim is void.

Everything here runs offline. Structures are built from published lattice
parameters rather than fetched, so the tests are deterministic and do not depend
on a network, an API key, or the contents of the cache. Tests that do need live
data are marked with ``@pytest.mark.network`` and are the exception.
"""

from __future__ import annotations

import math

import pytest
from pymatgen.core import Lattice, Structure

from materials_trust import checks, hubbard, matching, unit_checks
from materials_trust.audit import ConfidenceBand, assess_confidence
from materials_trust.checks import FlagCode, Severity
from materials_trust.records import (
    Functional,
    MagneticState,
    Property,
    PropertyRecord,
    ProvenanceError,
    Source,
    ValueKind,
)

# ---------------------------------------------------------------------------
# Reference structures, from published crystallography
# ---------------------------------------------------------------------------


def rutile() -> Structure:
    """TiO2 rutile, P4_2/mnm, a = 4.5937 A, c = 2.9587 A, O at (0.3053, 0.3053, 0)."""
    return Structure.from_spacegroup(
        "P4_2/mnm",
        Lattice.tetragonal(4.5937, 2.9587),
        ["Ti", "O"],
        [[0.0, 0.0, 0.0], [0.3053, 0.3053, 0.0]],
    )


def anatase() -> Structure:
    """TiO2 anatase, I4_1/amd, a = 3.7845 A, c = 9.5143 A, O at (0, 0, 0.2081)."""
    return Structure.from_spacegroup(
        "I4_1/amd",
        Lattice.tetragonal(3.7845, 9.5143),
        ["Ti", "O"],
        [[0.0, 0.0, 0.0], [0.0, 0.0, 0.2081]],
    )


def _record(
    source: Source,
    value: float,
    prop: Property = Property.FORMATION_ENERGY_PER_ATOM,
    functional: Functional = Functional.PBE,
    magnetic: MagneticState = MagneticState.NON_MAGNETIC,
    structure: Structure | None = None,
    formula: str = "TiO2",
    source_id: str = "test-1",
    correction: str = "none",
    icsd: bool | None = True,
    extras: dict | None = None,
) -> PropertyRecord:
    return PropertyRecord(
        source=source,
        source_id=source_id,
        formula=formula,
        property_name=prop,
        value=value,
        units="eV/atom" if prop is Property.FORMATION_ENERGY_PER_ATOM else "eV",
        functional=functional,
        correction_scheme=correction,
        magnetic_state=magnetic,
        value_kind=ValueKind.COMPUTED,
        structure_is_icsd_derived=icsd,
        structure=structure,
        extras=extras or {},
    )


# ---------------------------------------------------------------------------
# Rule 2.1, match structures and not formulas
# ---------------------------------------------------------------------------


class TestStructureMatching:
    def test_rutile_and_anatase_must_not_match(self):
        """The single most important test in the project.

        Rutile and anatase are both TiO2 and have genuinely different properties.
        If the matcher merged them, the benchmark would report a fabricated
        disagreement and brief section 2.1 would be violated.
        """
        result = matching.compare_structures(rutile(), anatase())
        assert not result.matched, (
            "rutile and anatase matched, which would merge distinct polymorphs "
            "and fabricate a disagreement"
        )
        assert result.outcome is matching.MatchOutcome.DIFFERENT_STRUCTURE

    def test_same_phase_matches_itself(self):
        assert matching.compare_structures(rutile(), rutile()).matched
        assert matching.compare_structures(anatase(), anatase()).matched

    def test_same_phase_matches_across_cell_choice(self):
        """A supercell of a phase is the same material as its primitive cell.

        Databases report different cell settings for the same phase, so a matcher
        that failed this would mark real agreement as polymorph-ambiguous.
        """
        supercell = rutile()
        supercell.make_supercell([2, 1, 1])
        assert matching.compare_structures(rutile(), supercell).matched

    def test_slightly_strained_phase_still_matches(self):
        """Different codes relax to slightly different lattice constants."""
        strained = rutile()
        strained.apply_strain(0.01)
        assert matching.compare_structures(rutile(), strained).matched

    def test_missing_structure_is_never_a_match(self):
        result = matching.compare_structures(rutile(), None)
        assert result.outcome is matching.MatchOutcome.STRUCTURE_UNAVAILABLE
        assert not result.matched

    def test_grouping_separates_polymorphs(self):
        records = [
            _record(Source.MATERIALS_PROJECT, -3.31, structure=rutile(), source_id="mp-2657"),
            _record(Source.OQMD, -3.30, structure=rutile(), source_id="2475"),
            _record(Source.MATERIALS_PROJECT, -3.29, structure=anatase(), source_id="mp-390"),
            _record(Source.OQMD, -3.28, structure=anatase(), source_id="2575"),
        ]
        grouping = matching.group_records(records)
        assert len(grouping.groups) == 2, "rutile and anatase must form separate groups"
        for group in grouping.groups:
            assert len(group.sources) == 2
            assert len(group.records) == 2

    def test_records_without_structure_are_never_grouped(self):
        records = [
            _record(Source.MATERIALS_PROJECT, -3.31, structure=rutile()),
            _record(Source.OQMD, -3.30, structure=None, source_id="no-struct"),
        ]
        grouping = matching.group_records(records)
        assert len(grouping.unstructured) == 1
        assert len(grouping.groups) == 1
        assert grouping.groups[0].sources == {Source.MATERIALS_PROJECT}


# ---------------------------------------------------------------------------
# Rule 2.6, provenance on everything
# ---------------------------------------------------------------------------


class TestProvenanceEnforcement:
    def test_functional_must_be_an_enum_not_a_string(self):
        with pytest.raises(ProvenanceError, match="functional"):
            PropertyRecord(
                source=Source.OQMD,
                source_id="1",
                formula="NaCl",
                property_name=Property.FORMATION_ENERGY_PER_ATOM,
                value=-2.05,
                units="eV/atom",
                functional="PBE",  # type: ignore[arg-type]
                correction_scheme="none",
                magnetic_state=MagneticState.NON_MAGNETIC,
                value_kind=ValueKind.COMPUTED,
                structure_is_icsd_derived=True,
            )

    def test_empty_correction_scheme_is_rejected(self):
        with pytest.raises(ProvenanceError, match="correction_scheme"):
            _record(Source.OQMD, -2.05, correction="")

    def test_empty_source_id_is_rejected(self):
        with pytest.raises(ProvenanceError, match="source_id"):
            _record(Source.OQMD, -2.05, source_id="  ")

    def test_measured_value_cannot_carry_a_functional(self):
        with pytest.raises(ProvenanceError, match="measured value"):
            PropertyRecord(
                source=Source.EXPERIMENT,
                source_id="expt-1",
                formula="Si",
                property_name=Property.BAND_GAP,
                value=1.17,
                units="eV",
                functional=Functional.PBE,
                correction_scheme="not_applicable",
                magnetic_state=MagneticState.NOT_APPLICABLE,
                value_kind=ValueKind.MEASURED,
                structure_is_icsd_derived=None,
            )

    def test_computed_value_must_declare_a_functional(self):
        with pytest.raises(ProvenanceError, match="must declare a functional"):
            PropertyRecord(
                source=Source.OQMD,
                source_id="1",
                formula="Si",
                property_name=Property.BAND_GAP,
                value=0.61,
                units="eV",
                functional=Functional.NOT_APPLICABLE,
                correction_scheme="none",
                magnetic_state=MagneticState.NON_MAGNETIC,
                value_kind=ValueKind.COMPUTED,
                structure_is_icsd_derived=True,
            )

    def test_unknown_functional_is_allowed_when_declared(self):
        rec = _record(Source.OQMD, -2.05, functional=Functional.UNKNOWN)
        assert rec.functional is Functional.UNKNOWN
        assert rec.uses_hubbard_u is None


# ---------------------------------------------------------------------------
# Rule 2.2, units and physical bounds
# ---------------------------------------------------------------------------


class TestUnitsAndBounds:
    def test_wrong_units_string_is_rejected(self):
        with pytest.raises(ProvenanceError, match="eV/atom"):
            PropertyRecord(
                source=Source.OQMD,
                source_id="1",
                formula="NaCl",
                property_name=Property.FORMATION_ENERGY_PER_ATOM,
                value=-4.26,
                units="eV",
                functional=Functional.PBE,
                correction_scheme="none",
                magnetic_state=MagneticState.NON_MAGNETIC,
                value_kind=ValueKind.COMPUTED,
                structure_is_icsd_derived=True,
            )

    def test_negative_band_gap_is_rejected(self):
        with pytest.raises(ProvenanceError, match="negative"):
            _record(Source.OQMD, -0.5, prop=Property.BAND_GAP)

    def test_kj_per_mol_formation_energy_is_rejected_by_bounds(self):
        """-411 kJ/mol accidentally passed through as eV/atom must not survive."""
        with pytest.raises(ProvenanceError, match="plausibility bound"):
            _record(Source.OQMD, -411.12)

    def test_nan_is_rejected(self):
        with pytest.raises(ProvenanceError, match="finite"):
            _record(Source.OQMD, float("nan"))

    def test_unit_classifier_identifies_ev_per_atom(self):
        verdict = unit_checks.classify_units("NaCl", -2.0501)
        assert verdict.verdict == "eV/atom"
        assert verdict.passed

    def test_unit_classifier_catches_per_formula_unit(self):
        """NaCl has 2 atoms per formula unit, so a per formula unit value doubles."""
        verdict = unit_checks.classify_units("NaCl", -4.10)
        assert verdict.verdict != "eV/atom"
        assert not verdict.passed
        assert "eV per formula unit" in verdict.consistent_hypotheses

    def test_unit_classifier_catches_kj_per_mol(self):
        verdict = unit_checks.classify_units("NaCl", -411.12)
        assert verdict.verdict != "eV/atom"
        assert not verdict.passed

    def test_literature_conversion_round_trips(self):
        """-411.12 kJ/mol over 2 atoms is -2.13 eV/atom."""
        value = unit_checks.literature_ev_per_atom("NaCl")
        assert value is not None
        assert math.isclose(value, -411.12 / 96.485 / 2, rel_tol=1e-9)
        assert math.isclose(value, -2.1305, abs_tol=1e-3)

    def test_sign_convention_check_catches_positive_values(self):
        problems = unit_checks.check_sign_convention([("NaCl", 2.05)])
        assert len(problems) == 1
        assert "sign convention" in problems[0]


# ---------------------------------------------------------------------------
# Rule 2.3, compare like with like, and the +U attribution tables
# ---------------------------------------------------------------------------


class TestFunctionalAndHubbard:
    def test_functional_mismatch_is_flagged(self):
        records = [
            _record(Source.MATERIALS_PROJECT, -1.65, functional=Functional.PBE_PLUS_U),
            _record(Source.OQMD, -1.60, functional=Functional.PBE),
        ]
        flags = checks.check_functional_consistency(records)
        assert FlagCode.FUNCTIONAL_MISMATCH in {f.code for f in flags}

    def test_matching_functionals_raise_no_mismatch(self):
        records = [
            _record(Source.MATERIALS_PROJECT, -3.31),
            _record(Source.OQMD, -3.30),
        ]
        codes = {f.code for f in checks.check_functional_consistency(records)}
        assert FlagCode.FUNCTIONAL_MISMATCH not in codes

    def test_correction_scheme_mismatch_is_flagged_separately(self):
        records = [
            _record(Source.MATERIALS_PROJECT, -3.31, correction="MP2020"),
            _record(Source.OQMD, -3.30, correction="OQMD fitted references"),
        ]
        codes = {f.code for f in checks.check_functional_consistency(records)}
        assert FlagCode.CORRECTION_SCHEME_MISMATCH in codes

    def test_inferred_functional_is_flagged(self):
        records = [
            _record(
                Source.OQMD,
                -3.30,
                extras={"functional_determined_by": "documented methodology"},
            ),
            _record(Source.MATERIALS_PROJECT, -3.31),
        ]
        codes = {f.code for f in checks.check_functional_consistency(records)}
        assert FlagCode.FUNCTIONAL_INFERRED in codes

    def test_fluoride_gets_u_at_mp_but_not_oqmd(self):
        """MP applies +U to oxides and fluorides, OQMD only to oxygen compounds."""
        cmp = hubbard.compare_hubbard_treatment("FeF3")
        assert "Fe" in cmp.only_mp
        assert not cmp.only_oqmd
        assert not cmp.agrees
        assert "fluorides" in cmp.explanation()

    def test_copper_oxide_gets_u_at_oqmd_but_not_mp(self):
        """Cu is in OQMD's +U table and not in the Materials Project one."""
        cmp = hubbard.compare_hubbard_treatment("CuO")
        assert "Cu" in cmp.only_oqmd
        assert "Cu" not in cmp.mp_u
        assert not cmp.agrees

    def test_molybdenum_oxide_gets_u_at_mp_but_not_oqmd(self):
        cmp = hubbard.compare_hubbard_treatment("MoO3")
        assert "Mo" in cmp.only_mp
        assert not cmp.agrees

    def test_iron_oxide_gets_u_at_both_with_different_values(self):
        cmp = hubbard.compare_hubbard_treatment("Fe2O3")
        assert "Fe" in cmp.differing_values
        assert cmp.differing_values["Fe"] == (5.3, 4.0)
        assert not cmp.agrees

    def test_iron_metal_gets_no_u_anywhere(self):
        """+U is applied only in the presence of the relevant anion."""
        cmp = hubbard.compare_hubbard_treatment("Fe")
        assert not cmp.mp_u and not cmp.oqmd_u
        assert cmp.agrees
        assert "Neither database" in cmp.explanation()

    def test_sodium_chloride_gets_no_u_anywhere(self):
        cmp = hubbard.compare_hubbard_treatment("NaCl")
        assert cmp.agrees

    def test_oqmd_spin_polarises_3d_compounds(self):
        assert hubbard.oqmd_expected_spin_polarised("TiO2")
        assert hubbard.oqmd_expected_spin_polarised("Fe2O3")
        assert not hubbard.oqmd_expected_spin_polarised("NaCl")
        assert not hubbard.oqmd_expected_spin_polarised("MgO")


# ---------------------------------------------------------------------------
# Rule 2.5, magnetic state
# ---------------------------------------------------------------------------


class TestMagneticChecks:
    def test_magnetic_mismatch_is_flagged(self):
        records = [
            _record(
                Source.MATERIALS_PROJECT, -1.65, magnetic=MagneticState.ANTIFERROMAGNETIC
            ),
            _record(Source.OQMD, -1.60, magnetic=MagneticState.FERROMAGNETIC),
        ]
        codes = {f.code for f in checks.check_magnetic_consistency(records)}
        assert FlagCode.MAGNETIC_MISMATCH in codes

    def test_unknown_magnetic_state_is_flagged(self):
        records = [
            _record(Source.MATERIALS_PROJECT, -1.65, magnetic=MagneticState.UNKNOWN),
            _record(Source.OQMD, -1.60, magnetic=MagneticState.FERROMAGNETIC),
        ]
        codes = {f.code for f in checks.check_magnetic_consistency(records)}
        assert FlagCode.MAGNETIC_UNKNOWN in codes

    def test_agreeing_magnetic_states_raise_no_mismatch(self):
        records = [
            _record(Source.MATERIALS_PROJECT, -3.31, magnetic=MagneticState.NON_MAGNETIC),
            _record(Source.OQMD, -3.30, magnetic=MagneticState.NON_MAGNETIC),
        ]
        codes = {f.code for f in checks.check_magnetic_consistency(records)}
        assert FlagCode.MAGNETIC_MISMATCH not in codes


# ---------------------------------------------------------------------------
# Flags: single source, hypothetical, large disagreement
# ---------------------------------------------------------------------------


class TestSpreadAndFlags:
    def test_single_source_is_flagged_and_spread_is_none(self):
        spread = checks.compute_spread([_record(Source.OQMD, -3.30)])
        assert spread.cross_source_spread is None
        codes = {f.code for f in checks.check_single_source(spread)}
        assert FlagCode.SINGLE_SOURCE in codes

    def test_intra_source_scatter_is_not_cross_source_disagreement(self):
        """Duplicate entries within one database must not inflate disagreement."""
        records = [
            _record(Source.OQMD, -2.0509, source_id="a"),
            _record(Source.OQMD, -2.0501, source_id="b"),
            _record(Source.OQMD, -2.0499, source_id="c"),
            _record(Source.MATERIALS_PROJECT, -2.0500, source_id="mp-22862"),
        ]
        spread = checks.compute_spread(records)
        assert spread.per_source[Source.OQMD].n == 3
        assert spread.per_source[Source.OQMD].intra_spread == pytest.approx(0.001, abs=1e-6)
        assert spread.cross_source_spread == pytest.approx(0.0001, abs=1e-6)

    def test_large_disagreement_flagged_above_threshold(self):
        records = [
            _record(Source.MATERIALS_PROJECT, -1.65),
            _record(Source.OQMD, -1.40),
        ]
        spread = checks.compute_spread(records)
        codes = {f.code for f in checks.check_large_disagreement(spread)}
        assert FlagCode.LARGE_DISAGREEMENT in codes

    def test_small_disagreement_not_flagged(self):
        records = [
            _record(Source.MATERIALS_PROJECT, -3.310),
            _record(Source.OQMD, -3.295),
        ]
        spread = checks.compute_spread(records)
        assert checks.check_large_disagreement(spread) == []

    def test_hypothetical_structure_is_flagged(self):
        records = [
            _record(Source.OQMD, -3.30, icsd=False),
            _record(Source.MATERIALS_PROJECT, -3.31, icsd=False, source_id="mp-x"),
        ]
        codes = {f.code for f in checks.check_hypothetical(records)}
        assert FlagCode.HYPOTHETICAL in codes

    def test_experimentally_observed_structure_is_not_flagged_hypothetical(self):
        records = [_record(Source.OQMD, -3.30, icsd=True)]
        assert checks.check_hypothetical(records) == []

    def test_mixed_properties_in_one_spread_is_rejected(self):
        with pytest.raises(ValueError, match="mix properties"):
            checks.compute_spread(
                [
                    _record(Source.OQMD, -3.30),
                    _record(Source.OQMD, 1.75, prop=Property.BAND_GAP),
                ]
            )


# ---------------------------------------------------------------------------
# Confidence band, brief section 3.1D
# ---------------------------------------------------------------------------


class TestConfidenceBand:
    def test_single_source_is_not_assessable(self):
        spread = checks.compute_spread([_record(Source.OQMD, -3.30)])
        assessment = assess_confidence(spread, checks.check_single_source(spread))
        assert assessment.band is ConfidenceBand.NOT_ASSESSABLE

    def test_close_agreement_with_clean_provenance_is_high(self):
        records = [
            _record(Source.MATERIALS_PROJECT, -3.3010),
            _record(Source.OQMD, -3.3000),
        ]
        spread = checks.compute_spread(records)
        assessment = assess_confidence(spread, [])
        assert assessment.band is ConfidenceBand.HIGH

    def test_large_disagreement_gives_low(self):
        records = [
            _record(Source.MATERIALS_PROJECT, -1.65),
            _record(Source.OQMD, -1.40),
        ]
        spread = checks.compute_spread(records)
        flags = checks.check_large_disagreement(spread)
        assert assess_confidence(spread, flags).band is ConfidenceBand.LOW

    def test_caveats_cap_high_at_moderate(self):
        records = [
            _record(Source.MATERIALS_PROJECT, -3.3010),
            _record(Source.OQMD, -3.3000, magnetic=MagneticState.UNKNOWN),
        ]
        spread = checks.compute_spread(records)
        flags = checks.check_magnetic_consistency(records)
        assessment = assess_confidence(spread, flags)
        assert assessment.band is ConfidenceBand.MODERATE
        assert any("capped at moderate" in s for s in assessment.steps)

    def test_derivation_is_always_recorded(self):
        """Brief 3.1D forbids a score that cannot be interrogated."""
        records = [
            _record(Source.MATERIALS_PROJECT, -3.31),
            _record(Source.OQMD, -3.30),
        ]
        spread = checks.compute_spread(records)
        assessment = assess_confidence(spread, [])
        assert assessment.steps
        payload = assessment.to_dict()
        assert payload["derivation"]
        assert "rule" in payload
        assert payload["inputs"]["cross_source_spread"] is not None

    def test_band_is_deterministic(self):
        records = [
            _record(Source.MATERIALS_PROJECT, -1.65),
            _record(Source.OQMD, -1.62),
        ]
        spread = checks.compute_spread(records)
        first = assess_confidence(spread, [])
        second = assess_confidence(spread, [])
        assert first.band is second.band
        assert first.steps == second.steps

    def test_other_polymorphs_existing_does_not_demote_a_matched_comparison(self):
        """The whole point of matching structures is that other polymorphs of the
        same composition are irrelevant to this comparison.

        A composition having several polymorphs is context, and the code reports it
        as such, but the two sources here agree about one structure that was
        successfully matched. Demoting for that would penalise exactly the case the
        matching machinery exists to handle, and would drag down every band in a
        benchmark of polymorph-rich oxides.
        """
        records = [
            _record(Source.MATERIALS_PROJECT, -3.3010),
            _record(Source.OQMD, -3.3000),
        ]
        spread = checks.compute_spread(records)
        flags = checks.check_polymorph_ambiguity(None, competing_groups=12)
        assert [f.severity for f in flags] == [Severity.INFO]
        assessment = assess_confidence(spread, flags)
        assert assessment.band is ConfidenceBand.HIGH
        assert not any("demoted" in s for s in assessment.steps)
        assert assessment.inputs["demoting_flags"] == []

    def test_unmatchable_structure_does_demote(self):
        """The same flag code at warning severity must still demote, because there
        the structural identity genuinely was not established."""
        records = [
            _record(Source.MATERIALS_PROJECT, -3.3010),
            _record(Source.OQMD, -3.3000),
        ]
        spread = checks.compute_spread(records)
        flags = checks.check_polymorph_ambiguity(
            None, unstructured=[_record(Source.OQMD, -3.29, structure=None)]
        )
        assert any(f.severity is Severity.WARNING for f in flags)
        assessment = assess_confidence(spread, flags)
        assert assessment.band is not ConfidenceBand.HIGH
        assert any("demoted" in s for s in assessment.steps)

    def test_inferred_non_magnetic_state_does_not_cap_the_band(self):
        """A non-magnetic inference for a compound with no partially filled d or f
        shell is safe, so it is reported without penalising the band.

        Without this, every comparison against OQMD would be capped at moderate,
        since OQMD exposes no magnetic metadata for any entry.
        """
        records = [
            _record(Source.MATERIALS_PROJECT, -3.3010, magnetic=MagneticState.NON_MAGNETIC),
            _record(
                Source.OQMD,
                -3.3000,
                magnetic=MagneticState.NON_MAGNETIC,
                extras={"magnetic_state_determined_by": "documented methodology"},
            ),
        ]
        spread = checks.compute_spread(records)
        flags = checks.check_magnetic_consistency(records)
        inferred = [f for f in flags if f.code is FlagCode.MAGNETIC_INFERRED]
        assert len(inferred) == 1
        assert inferred[0].severity is Severity.INFO
        assert "non-magnetic" in inferred[0].message
        assert assess_confidence(spread, flags).band is ConfidenceBand.HIGH

    def test_inferred_ferromagnetic_state_does_cap_the_band(self):
        """Assuming ferromagnetic order when the truth may be antiferromagnetic is a
        real risk, and OQMD documents the size of the resulting energy error."""
        records = [
            _record(Source.MATERIALS_PROJECT, -1.7010, magnetic=MagneticState.FERROMAGNETIC),
            _record(
                Source.OQMD,
                -1.7000,
                magnetic=MagneticState.FERROMAGNETIC,
                extras={"magnetic_state_determined_by": "documented methodology"},
            ),
        ]
        spread = checks.compute_spread(records)
        flags = checks.check_magnetic_consistency(records)
        inferred = [f for f in flags if f.code is FlagCode.MAGNETIC_INFERRED]
        assert len(inferred) == 1
        assert inferred[0].severity is Severity.CAVEAT
        assert "antiferromagnetic" in inferred[0].message
        assessment = assess_confidence(spread, flags)
        assert assessment.band is ConfidenceBand.MODERATE
        assert any("capped at moderate" in s for s in assessment.steps)


# ---------------------------------------------------------------------------
# Rule 2.4, DFT gaps are DFT gaps
# ---------------------------------------------------------------------------


class TestDftVersusExperiment:
    def test_pbe_gap_underestimates_silicon(self):
        """Silicon: PBE gives roughly 0.6 eV against a measured 1.17 eV.

        The sign of the error is the headline finding of the whole benchmark, so
        the machinery that computes it is asserted here on known numbers.
        """
        from materials_trust.sources.experimental import compare_gap

        computed = [
            _record(
                Source.MATERIALS_PROJECT,
                0.61,
                prop=Property.BAND_GAP,
                formula="Si",
                source_id="mp-149",
                extras={"energy_above_hull": 0.0},
            )
        ]
        comparison = compare_gap("Si", 1.17, computed, likely_mpid="mp-149")
        assert comparison.signed_error_ev is not None
        assert comparison.signed_error_ev < 0, "PBE must underestimate the silicon gap"
        assert comparison.signed_error_ev == pytest.approx(-0.56, abs=1e-6)
        # The comparison must never claim to be structure matched.
        assert comparison.to_dict()["comparison_level"] == (
            "composition, not structure matched"
        )

    def test_zero_computed_gap_does_not_assert_metallicity(self):
        """Brief 2.4: a computed gap of 0.0 eV does not establish a metal.

        The comparison must still record that experiment measured a finite gap
        rather than quietly agreeing with the calculation.
        """
        from materials_trust.sources.experimental import compare_gap

        computed = [
            _record(
                Source.MATERIALS_PROJECT,
                0.0,
                prop=Property.BAND_GAP,
                formula="Ge",
                source_id="mp-32",
                extras={"energy_above_hull": 0.0},
            )
        ]
        comparison = compare_gap("Ge", 0.67, computed)
        assert comparison.computed_predicts_metal is True
        assert comparison.measured_as_metal is False
        assert comparison.signed_error_ev == pytest.approx(-0.67, abs=1e-6)

    def test_polymorph_spread_marks_comparison_unclean(self):
        from materials_trust.sources.experimental import compare_gap

        computed = [
            _record(
                Source.MATERIALS_PROJECT,
                1.75,
                prop=Property.BAND_GAP,
                source_id="mp-2657",
                extras={"energy_above_hull": 0.0},
            ),
            _record(
                Source.MATERIALS_PROJECT,
                3.10,
                prop=Property.BAND_GAP,
                source_id="mp-390",
                extras={"energy_above_hull": 0.05},
            ),
        ]
        comparison = compare_gap("TiO2", 3.0, computed)
        assert comparison.polymorph_spread_ev == pytest.approx(1.35, abs=1e-6)
        assert not comparison.clean

    def test_single_polymorph_comparison_is_clean(self):
        from materials_trust.sources.experimental import compare_gap

        computed = [
            _record(
                Source.MATERIALS_PROJECT,
                0.61,
                prop=Property.BAND_GAP,
                formula="Si",
                source_id="mp-149",
                extras={"energy_above_hull": 0.0},
            )
        ]
        assert compare_gap("Si", 1.17, computed).clean
