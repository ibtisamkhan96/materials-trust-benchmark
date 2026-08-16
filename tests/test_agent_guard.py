"""Tests for the numeric guard that enforces layer 3's hard boundary.

The boundary is that a language model may never compute, estimate, adjust, or
invent a numerical value. A prompt asking for that is not enforcement, so the
guard is what actually enforces it, and these tests are what establish that the
guard works. They run offline with no model involved.
"""

from __future__ import annotations

import json

from materials_trust.agent import extract_numbers, verify_numeric_claims

TOOL_OUTPUT = json.dumps(
    {
        "formula": "Fe2O3",
        "values": [
            {"source": "materials_project", "source_id": "mp-24972", "value": -1.8492},
            {"source": "oqmd", "source_id": "92501", "value": -1.6498},
        ],
        "spread": {"cross_source_spread": 0.1994, "n_sources": 2},
        "confidence": {"band": "low"},
    }
)


class TestNumberExtraction:
    def test_finds_signed_and_decimal_numbers(self):
        found = extract_numbers("the value is -1.8492 eV/atom, spread 0.1994")
        assert "-1.8492" in found
        assert "0.1994" in found

    def test_identifier_hyphen_is_not_read_as_a_minus_sign(self):
        assert extract_numbers("see mp-24972") == ["24972"]

    def test_formula_subscripts_are_not_numeric_claims(self):
        assert extract_numbers("Fe2O3 and TiO2 disagree") == []

    def test_space_group_symbols_are_not_numeric_claims(self):
        assert extract_numbers("the phase is P4_2/mnm") == []

    def test_prose_without_numbers_yields_nothing(self):
        assert extract_numbers("the databases disagree because of the +U treatment") == []


class TestGuardAcceptsTraceableClaims:
    def test_exact_value_passes(self):
        result = verify_numeric_claims(
            "Materials Project reports -1.8492 eV/atom.", [TOOL_OUTPUT]
        )
        assert result.passed
        assert result.unverified == []

    def test_honest_rounding_passes(self):
        result = verify_numeric_claims(
            "Materials Project reports -1.85 eV/atom and OQMD reports -1.65 eV/atom.",
            [TOOL_OUTPUT],
        )
        assert result.passed

    def test_identifier_digits_pass(self):
        result = verify_numeric_claims(
            "The Materials Project entry is mp-24972 and the OQMD entry is 92501.",
            [TOOL_OUTPUT],
        )
        assert result.passed

    def test_answer_with_no_numbers_passes(self):
        result = verify_numeric_claims(
            "The two databases disagree because they apply +U differently.",
            [TOOL_OUTPUT],
        )
        assert result.passed
        assert result.n_claims == 0
        assert "no numeric claims" in result.note

    def test_number_from_the_question_passes(self):
        result = verify_numeric_claims(
            "You asked about a 0.5 eV threshold, and the spread is 0.1994 eV/atom.",
            [TOOL_OUTPUT],
            question="is a disagreement above 0.5 eV significant",
        )
        assert result.passed


class TestGuardRejectsUntraceableClaims:
    def test_invented_value_is_caught(self):
        result = verify_numeric_claims(
            "Materials Project reports -1.8492 eV/atom and the experimental value "
            "is -1.7085 eV/atom.",
            [TOOL_OUTPUT],
        )
        assert not result.passed
        assert "-1.7085" in result.unverified

    def test_computed_percentage_is_caught(self):
        """The model must not derive a quantity, even a correct one."""
        result = verify_numeric_claims(
            "The two values differ by 10.8 percent.", [TOOL_OUTPUT]
        )
        assert not result.passed
        assert "10.8" in result.unverified

    def test_computed_difference_is_caught(self):
        result = verify_numeric_claims(
            "The gap between them works out to 0.19940001 eV/atom.", [TOOL_OUTPUT]
        )
        assert not result.passed

    def test_bare_integer_does_not_match_an_arbitrary_rounding(self):
        """Rounding to zero decimals is not accepted, or the guard would be empty."""
        result = verify_numeric_claims("The formation energy is about -2 eV/atom.", [TOOL_OUTPUT])
        assert not result.passed
        assert "-2" in result.unverified

    def test_failure_note_explains_the_boundary(self):
        result = verify_numeric_claims("The value is 42.4242 eV.", [TOOL_OUTPUT])
        assert not result.passed
        assert "hard boundary" in result.note
        assert "unsupported" in result.note

    def test_all_untraceable_numbers_are_reported_not_just_the_first(self):
        result = verify_numeric_claims(
            "Values of 11.1111, 22.2222 and 33.3333 eV were found.", [TOOL_OUTPUT]
        )
        assert not result.passed
        assert set(result.unverified) == {"11.1111", "22.2222", "33.3333"}

    def test_no_tool_output_means_no_number_is_traceable(self):
        result = verify_numeric_claims("The band gap is 1.75 eV.", [])
        assert not result.passed
        assert "1.75" in result.unverified


class TestGuardReporting:
    def test_result_serialises_for_the_record(self):
        payload = verify_numeric_claims("The value is 99.9999 eV.", [TOOL_OUTPUT]).to_dict()
        assert payload["passed"] is False
        assert payload["n_numeric_claims"] >= 1
        assert payload["unverified_numbers"]
        assert payload["note"]

    def test_claim_count_is_reported(self):
        result = verify_numeric_claims(
            "-1.8492 and -1.6498 give a spread of 0.1994.", [TOOL_OUTPUT]
        )
        assert result.n_claims == 3
        assert result.passed
