"""Tests for the statistics and plotting layer.

Plotting code that has never been executed is a liability in a project whose
output is a report, so these tests build synthetic trust records and drive the
whole reporting path, checking both the numbers and that the figures are
actually written.

The statistics are checked against values computed by hand in the assertions, so
a change in how a spread or an error is defined will fail here rather than
quietly altering a headline finding.
"""

from __future__ import annotations

import pytest
from pymatgen.core import Lattice, Structure

from materials_trust import checks, config, report
from materials_trust.audit import ConfidenceBand, TrustRecord, assess_confidence
from materials_trust.checks import Flag, FlagCode, Severity
from materials_trust.matching import MATCHER_DESCRIPTION
from materials_trust.records import (
    Functional,
    MagneticState,
    Property,
    PropertyRecord,
    Source,
    ValueKind,
)
from materials_trust.sources.experimental import GapComparison


def _structure() -> Structure:
    return Structure(Lattice.cubic(4.0), ["Na", "Cl"], [[0, 0, 0], [0.5, 0.5, 0.5]])


def _rec(source: Source, value: float, prop: Property, formula: str = "NaCl", sid: str = "x"):
    return PropertyRecord(
        source=source,
        source_id=sid,
        formula=formula,
        property_name=prop,
        value=value,
        units="eV/atom" if prop is Property.FORMATION_ENERGY_PER_ATOM else "eV",
        functional=Functional.PBE,
        correction_scheme="none",
        magnetic_state=MagneticState.NON_MAGNETIC,
        value_kind=ValueKind.COMPUTED,
        structure_is_icsd_derived=True,
        structure=_structure(),
    )


def _trust(
    mp_value: float,
    oqmd_value: float | None,
    prop: Property = Property.FORMATION_ENERGY_PER_ATOM,
    formula: str = "NaCl",
    flags: list[Flag] | None = None,
) -> TrustRecord:
    records = [_rec(Source.MATERIALS_PROJECT, mp_value, prop, formula, "mp-1")]
    if oqmd_value is not None:
        records.append(_rec(Source.OQMD, oqmd_value, prop, formula, "1"))
    spread = checks.compute_spread(records)
    flags = flags or []
    return TrustRecord(
        formula=formula,
        property_name=prop,
        group_id=0,
        structure_fingerprint=f"{formula} Fm-3m (#225) n=2",
        records=records,
        spread=spread,
        flags=flags,
        confidence=assess_confidence(spread, flags),
        matcher_description=MATCHER_DESCRIPTION,
    )


def _hubbard_flag() -> Flag:
    return Flag(
        code=FlagCode.HUBBARD_U_MISMATCH,
        severity=Severity.WARNING,
        message="policies differ",
        evidence={},
    )


class TestPairedDifferences:
    def test_only_multi_source_records_are_paired(self):
        records = [_trust(-2.05, -2.04), _trust(-3.31, None)]
        pairs = report.paired_differences(records, Property.FORMATION_ENERGY_PER_ATOM)
        assert len(pairs) == 1
        assert pairs[0].signed_difference == pytest.approx(-0.01, abs=1e-9)

    def test_sign_convention_is_mp_minus_oqmd(self):
        pairs = report.paired_differences(
            [_trust(-1.85, -1.65)], Property.FORMATION_ENERGY_PER_ATOM
        )
        assert pairs[0].signed_difference == pytest.approx(-0.20, abs=1e-9)

    def test_property_filter_is_respected(self):
        records = [
            _trust(-2.05, -2.04, Property.FORMATION_ENERGY_PER_ATOM),
            _trust(5.2, 5.1, Property.BAND_GAP),
        ]
        assert len(report.paired_differences(records, Property.BAND_GAP)) == 1
        assert (
            len(report.paired_differences(records, Property.FORMATION_ENERGY_PER_ATOM))
            == 1
        )


class TestCrossSourceStats:
    def test_statistics_match_hand_computation(self):
        records = [_trust(-2.00, -2.01), _trust(-1.85, -1.65, formula="FeO")]
        stats = report.cross_source_stats(records, Property.FORMATION_ENERGY_PER_ATOM)
        assert stats["n_structure_matched_pairs"] == 2
        # differences are +0.01 and -0.20, so the mean signed value is -0.095
        assert stats["all"]["mean_signed"] == pytest.approx(-0.095, abs=1e-6)
        assert stats["all"]["mean_absolute"] == pytest.approx(0.105, abs=1e-6)
        # only the first is within 0.05 eV/atom
        assert stats["n_within_threshold"] == 1
        assert stats["fraction_within_threshold"] == pytest.approx(0.5)

    def test_stratification_by_hubbard_policy_separates_records(self):
        records = [
            _trust(-2.00, -2.01),
            _trust(-1.85, -1.65, formula="FeO", flags=[_hubbard_flag()]),
        ]
        stats = report.cross_source_stats(records, Property.FORMATION_ENERGY_PER_ATOM)
        strata = stats["stratified_by_hubbard_u_policy_mismatch"]
        assert strata["policies_differ"]["n"] == 1
        assert strata["policies_agree"]["n"] == 1
        assert strata["policies_differ"]["mean_absolute"] == pytest.approx(0.20, abs=1e-6)

    def test_empty_input_reports_no_data_rather_than_zero(self):
        """Reporting a mean of zero for no comparisons would be a false finding."""
        stats = report.cross_source_stats([], Property.FORMATION_ENERGY_PER_ATOM)
        assert stats["n_structure_matched_pairs"] == 0
        assert stats["all"]["n"] == 0
        assert "no comparisons" in stats["all"]["note"]
        assert stats["fraction_within_threshold"] is None


class TestFlagAndConfidenceSummaries:
    def test_flag_counts_are_per_record_not_per_flag(self):
        records = [
            _trust(-1.85, -1.65, formula="FeO", flags=[_hubbard_flag(), _hubbard_flag()]),
            _trust(-2.00, -2.01),
        ]
        freqs = report.flag_frequencies(records)
        assert freqs["n_trust_records"] == 2
        assert freqs["counts"]["HUBBARD_U_MISMATCH"] == 1
        assert freqs["fractions"]["HUBBARD_U_MISMATCH"] == pytest.approx(0.5)

    def test_confidence_distribution_covers_all_bands(self):
        counts = report.confidence_distribution([_trust(-2.00, -2.01), _trust(-2.0, None)])
        assert set(counts) == {b.value for b in ConfidenceBand}
        assert counts[ConfidenceBand.NOT_ASSESSABLE.value] == 1
        assert sum(counts.values()) == 2

    def test_band_limits_separate_disagreement_from_thin_metadata(self):
        """Zero high-confidence comparisons has two opposite explanations, so the
        report has to say which one applies."""
        capped = _trust(
            -2.000,
            -2.001,
            flags=[
                Flag(
                    code=FlagCode.FUNCTIONAL_INFERRED,
                    severity=Severity.CAVEAT,
                    message="derived from methodology",
                    evidence={},
                )
            ],
        )
        limits = report.confidence_band_limits([capped, _trust(-2.0, None)])
        assert limits["n_multi_source_comparisons"] == 1
        assert limits["n_base_band_high_on_spread_alone"] == 1
        assert limits["n_final_band_high"] == 0
        assert limits["n_with_no_capping_provenance_caveat"] == 0
        assert limits["capping_flags_present_in_every_comparison"] == [
            "FUNCTIONAL_INFERRED"
        ]

    def test_band_limits_report_nothing_when_no_caveats_apply(self):
        limits = report.confidence_band_limits([_trust(-2.000, -2.001)])
        assert limits["n_final_band_high"] == 1
        assert limits["capping_flags_present_in_every_comparison"] == []
        assert limits["n_with_no_capping_provenance_caveat"] == 1

    def test_band_limits_handle_a_run_with_no_multi_source_comparisons(self):
        assert report.confidence_band_limits([_trust(-2.0, None)]) == {
            "n_multi_source_comparisons": 0
        }


class TestExperimentStats:
    def _cmp(self, expt: float, computed: float | None, spread: float | None = None):
        return GapComparison(
            formula="Si",
            experimental_gap_ev=expt,
            measured_as_metal=expt <= 0.0,
            computed_gap_ev=computed,
            computed_material_id="mp-149",
            computed_source="materials_project",
            computed_functional="PBE",
            polymorph_spread_ev=spread,
            n_polymorphs=1,
            likely_mpid="mp-149",
            likely_mpid_gap_ev=computed,
        )

    def test_metals_are_excluded_from_signed_error(self):
        stats = report.dft_vs_experiment_stats(
            [self._cmp(1.17, 0.61), self._cmp(0.0, 0.0)]
        )
        assert stats["n_measured_gapped"] == 1
        assert stats["n_measured_metallic"] == 1
        assert stats["gapped_clean_only"]["n"] == 1
        assert stats["gapped_clean_only"]["mean_signed"] == pytest.approx(-0.56, abs=1e-6)

    def test_metal_classification_rate_is_reported(self):
        stats = report.dft_vs_experiment_stats(
            [self._cmp(0.0, 0.0), self._cmp(0.0, 1.2)]
        )
        assert stats["metals"]["n"] == 2
        assert stats["metals"]["n_computed_also_zero_gap"] == 1
        assert stats["metals"]["fraction_computed_also_zero_gap"] == pytest.approx(0.5)

    def test_polymorph_ambiguous_comparisons_excluded_from_clean_statistics(self):
        stats = report.dft_vs_experiment_stats(
            [self._cmp(1.17, 0.61), self._cmp(3.0, 1.75, spread=1.35)]
        )
        assert stats["n_measured_gapped"] == 2
        assert stats["n_clean_gapped"] == 1
        assert stats["gapped_all_polymorph_states"]["n"] == 2
        assert stats["gapped_clean_only"]["n"] == 1

    def test_zero_computed_gap_against_measured_gap_is_counted(self):
        stats = report.dft_vs_experiment_stats([self._cmp(0.67, 0.0)])
        assert stats["n_computed_zero_gap_but_measured_gapped"] == 1
        assert stats["fraction_computed_zero_gap_but_measured_gapped"] == pytest.approx(1.0)

    def test_underestimation_fraction(self):
        stats = report.dft_vs_experiment_stats(
            [self._cmp(1.17, 0.61), self._cmp(1.0, 1.5), self._cmp(2.0, 1.0)]
        )
        # Reported fractions are rounded to four decimals for the summary file.
        assert stats["fraction_underestimated_clean"] == pytest.approx(2 / 3, abs=1e-4)


class TestPlots:
    def test_all_plots_render_to_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "FIGURES_DIR", tmp_path)
        records = [
            _trust(-2.00, -2.01),
            _trust(-1.85, -1.65, formula="FeO", flags=[_hubbard_flag()]),
        ]
        pairs = report.paired_differences(records, Property.FORMATION_ENERGY_PER_ATOM)
        comparisons = [
            GapComparison(
                formula="Si",
                experimental_gap_ev=1.17,
                measured_as_metal=False,
                computed_gap_ev=0.61,
                computed_material_id="mp-149",
                computed_source="materials_project",
                computed_functional="PBE",
                polymorph_spread_ev=None,
                n_polymorphs=1,
                likely_mpid="mp-149",
                likely_mpid_gap_ev=0.61,
            )
        ]

        produced = [
            report.plot_disagreement_histogram(
                pairs, Property.FORMATION_ENERGY_PER_ATOM, "hist.png"
            ),
            report.plot_disagreement_by_hubbard(
                pairs, Property.FORMATION_ENERGY_PER_ATOM, "hub.png"
            ),
            report.plot_gap_parity(comparisons, "parity.png"),
            report.plot_gap_error_histogram(comparisons, "err.png"),
            report.plot_flag_frequencies(report.flag_frequencies(records), "flags.png"),
            report.plot_confidence_distribution(
                report.confidence_distribution(records), "conf.png"
            ),
        ]
        assert all(p is not None for p in produced)
        for name in ("hist.png", "hub.png", "parity.png", "err.png", "flags.png", "conf.png"):
            assert (tmp_path / name).exists(), f"{name} was not written"
            assert (tmp_path / name).stat().st_size > 1000

    def test_plots_return_none_rather_than_empty_figures(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "FIGURES_DIR", tmp_path)
        assert (
            report.plot_disagreement_histogram(
                [], Property.FORMATION_ENERGY_PER_ATOM, "empty.png"
            )
            is None
        )
        assert report.plot_gap_parity([], "empty2.png") is None
        assert report.plot_flag_frequencies({"counts": {}}, "empty3.png") is None
        assert not list(tmp_path.iterdir())
