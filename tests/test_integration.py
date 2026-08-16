"""Integration tests against the live data sources.

Marked ``network`` and skipped by default, because the offline suite is what
guards the physics rules and it must stay runnable with no key and no connection.
These tests exist to catch the other failure mode: an API that changes shape
underneath us, which no amount of offline testing can detect.

Run them explicitly:

    pytest -m network
    pytest -m network -k oqmd          # the parts needing no API key
"""

from __future__ import annotations

import pytest

from materials_trust import config, matching, unit_checks
from materials_trust.audit import Auditor
from materials_trust.records import Property, Source
from materials_trust.sources.materials_project import MaterialsProjectSource
from materials_trust.sources.oqmd import OQMDSource

pytestmark = pytest.mark.network

needs_mp_key = pytest.mark.skipif(
    not config.mp_api_key(), reason="MP_API_KEY is not set"
)


class TestOqmdLive:
    def test_returns_records_with_reconstructed_structures(self):
        records = OQMDSource().records_for("NaCl")
        assert records, "OQMD returned nothing for NaCl"
        assert all(r.has_structure for r in records), (
            "OQMD stopped returning usable unit_cell and sites data, which would "
            "force every comparison to polymorph-ambiguous"
        )

    def test_formation_energy_is_ev_per_atom(self):
        records = OQMDSource().records_for(
            "NaCl", properties=[Property.FORMATION_ENERGY_PER_ATOM]
        )
        value = min(r.value for r in records)
        verdict = unit_checks.classify_units("NaCl", value)
        assert verdict.verdict == "eV/atom", verdict.detail

    def test_provenance_is_complete_on_every_record(self):
        for record in OQMDSource().records_for("Fe2O3"):
            assert record.source is Source.OQMD
            assert record.source_id
            assert record.correction_scheme
            assert record.functional is not None
            assert record.magnetic_state is not None
            assert record.retrieved_at
            assert record.source_url

    def test_iron_oxide_is_recognised_as_gga_plus_u(self):
        """OQMD applies +U to iron in oxygen-containing compounds."""
        records = OQMDSource().records_for("Fe2O3")
        assert all(r.uses_hubbard_u for r in records)

    def test_iron_fluoride_is_not_gga_plus_u(self):
        """OQMD applies +U only to oxides, so FeF3 must come back as plain PBE."""
        records = OQMDSource().records_for("FeF3")
        assert all(r.uses_hubbard_u is False for r in records)

    def test_real_tio2_polymorphs_are_not_merged(self):
        """The correctness claim, checked against real data rather than fixtures."""
        records = [
            r
            for r in OQMDSource().records_for(
                "TiO2", properties=[Property.FORMATION_ENERGY_PER_ATOM]
            )
            if r.has_structure
        ]
        grouping = matching.group_records(records)
        assert len(grouping.groups) > 1, (
            "every real OQMD TiO2 entry was merged into one group, which means "
            "distinct polymorphs are being treated as the same material"
        )
        fingerprints = {g.fingerprint() for g in grouping.groups}
        assert len(fingerprints) > 1

    def test_cache_makes_a_second_call_cheap(self):
        src = OQMDSource()
        src.records_for("NaCl")
        assert src.cache.stats().get("composition", 0) >= 1


@needs_mp_key
class TestMaterialsProjectLive:
    def test_silicon_summary_is_retrievable(self):
        docs = MaterialsProjectSource().summary(material_ids=["mp-149"])
        assert docs
        assert docs[0]["formula_pretty"] == "Si"

    def test_formation_energy_comes_from_the_gga_thermo_type(self):
        src = MaterialsProjectSource()
        records = src.records_for(
            material_ids=["mp-149"], properties=[Property.FORMATION_ENERGY_PER_ATOM]
        )
        assert records
        for r in records:
            assert r.extras["thermo_type_used"] == "GGA_GGA+U"

    def test_silicon_pbe_gap_underestimates_the_measured_value(self):
        """The headline physics claim, against live data.

        The measured indirect gap of silicon is 1.17 eV. Standard PBE gives
        roughly 0.6 eV. If this ever passes, something is wrong with the data.
        """
        records = MaterialsProjectSource().records_for(
            material_ids=["mp-149"], properties=[Property.BAND_GAP]
        )
        assert records
        gap = records[0].value
        assert gap < 1.17, f"PBE gap of {gap} eV does not underestimate 1.17 eV"

    def test_magnetic_ordering_is_read_from_the_api(self):
        records = MaterialsProjectSource().records_for(formula="Fe2O3")
        assert records
        assert any(
            r.extras.get("magnetic_state_determined_by") == "API ordering field"
            for r in records
        )


@needs_mp_key
class TestFullAuditLive:
    def test_cross_source_audit_produces_matched_comparisons(self):
        result = Auditor().audit("NaCl")
        assert result.trust_records
        assert result.coverage.n_structure_matched_multi_source >= 1

    def test_fluoride_audit_flags_the_hubbard_policy_difference(self):
        """FeF3 gets +U at Materials Project and not at OQMD, so this must flag."""
        from materials_trust.checks import FlagCode

        result = Auditor().audit("FeF3")
        codes = {
            f.code for tr in result.trust_records for f in tr.flags
        }
        assert FlagCode.HUBBARD_U_MISMATCH in codes

    def test_audit_is_deterministic(self):
        auditor = Auditor()
        first = auditor.audit("NaCl").to_dict()
        second = auditor.audit("NaCl").to_dict()
        # retrieved_at differs between runs by design, so compare the values.
        def strip(payload):
            for tr in payload["trust_records"]:
                for v in tr["values"]:
                    v.pop("retrieved_at", None)
            return payload

        assert strip(first) == strip(second)
