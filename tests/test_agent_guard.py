"""Tests for the numeric guard that enforces layer 3's hard boundary.

The boundary is that a language model may never compute, estimate, adjust, or
invent a numerical value. A prompt asking for that is not enforcement, so the
guard is what actually enforces it, and these tests are what establish that the
guard works. They run offline with no model involved.
"""

from __future__ import annotations

import json

from materials_trust.agent import (
    bound_tool_result,
    extract_numbers,
    extract_structure_mislabels,
    verify_numeric_claims,
)

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


class TestTypographyDoesNotDefeatTheGuard:
    """Regressions found by running the eval set against a live model.

    Both of these were false positives: the model quoted the tools correctly and
    the guard called it invention. A guard that cries wolf gets ignored, which
    costs as much as a guard that misses a fabrication.
    """

    def test_unicode_minus_is_read_as_a_minus_sign(self):
        result = verify_numeric_claims(
            "Materials Project reports \u22121.8492 eV/atom.", [TOOL_OUTPUT]
        )
        assert result.passed, result.unverified

    def test_unicode_minus_cannot_launder_a_sign(self):
        """The fix must not let a negated claim match a positive source value."""
        result = verify_numeric_claims(
            "The spread is \u22120.1994 eV/atom.", [TOOL_OUTPUT]
        )
        assert not result.passed
        assert "-0.1994" in result.unverified

    def test_markdown_list_marker_is_not_a_numeric_claim(self):
        answer = "Findings:\n\n1. The functional is PBE.\n4. No +U was applied.\n"
        result = verify_numeric_claims(answer, [TOOL_OUTPUT])
        assert result.passed, result.unverified

    def test_a_value_cannot_hide_behind_a_list_marker(self):
        """Only an integer with a period and then whitespace is a marker."""
        result = verify_numeric_claims("4.7321 eV is the gap.", [TOOL_OUTPUT])
        assert not result.passed
        assert "4.7321" in result.unverified

    def test_citing_an_instruction_rule_is_not_a_numeric_claim(self):
        result = verify_numeric_claims(
            "According to my instructions (rule 7) I cannot attach mineral names.",
            [TOOL_OUTPUT],
        )
        assert result.passed, result.unverified

    def test_a_real_seven_is_still_caught(self):
        result = verify_numeric_claims("The gap is 7 eV.", [TOOL_OUTPUT])
        assert not result.passed
        assert "7" in result.unverified


class TestGuardCatchesRatiosWrittenInWords:
    """A live model computed a ratio and rendered it in words, evading a
    digit-only guard entirely. Words are arithmetic too."""

    def test_spelled_out_multiple_is_caught(self):
        result = verify_numeric_claims(
            "The spread of 0.1994 eV/atom is more than five times the threshold.",
            [TOOL_OUTPUT],
        )
        assert not result.passed
        assert any("five times" in u.lower() for u in result.unverified)

    def test_twice_is_caught(self):
        result = verify_numeric_claims(
            "OQMD reports twice as many entries.", [TOOL_OUTPUT]
        )
        assert not result.passed

    def test_order_of_magnitude_is_caught(self):
        result = verify_numeric_claims(
            "The disagreement is an order of magnitude larger.", [TOOL_OUTPUT]
        )
        assert not result.passed

    def test_a_plain_count_is_not_a_ratio(self):
        """"two databases" is a count the tools support, not arithmetic."""
        result = verify_numeric_claims(
            "The two databases disagree because of the +U treatment.", [TOOL_OUTPUT]
        )
        assert result.passed, result.unverified

    def test_a_ratio_the_user_supplied_may_be_echoed(self):
        result = verify_numeric_claims(
            "No, it is not twice the threshold.",
            [TOOL_OUTPUT],
            question="is the spread twice the threshold",
        )
        assert result.passed, result.unverified


class TestGuardCatchesStructuralMislabels:
    """A live model labelled an I4_1/amd TiO2 entry Rutile. Rutile is P4_2/mnm;
    I4_1/amd is anatase. Every number in that answer traced, so a digit-only
    guard passed a claim about the wrong material.

    The detector is deliberately conservative. It fires only when a mineral name
    that denotes one space group sits next to a different space group from the
    same small table. A name used alone, a space group used alone, two correctly
    paired names in one sentence, and a name far from an unrelated symbol must
    all pass, or the guard will cry wolf on ordinary explanations.
    """

    def test_the_observed_failure_is_caught(self):
        result = verify_numeric_claims(
            "The I4_1/amd structure (Rutile) has a spread of 0.1994 eV.",
            [TOOL_OUTPUT],
        )
        assert not result.passed
        assert result.mislabelled_structures
        assert any("rutile" in m.lower() for m in result.mislabelled_structures)

    def test_a_unicode_subscript_does_not_hide_the_same_error(self):
        problems = extract_structure_mislabels("Structure: TiO2 I4\u2081/amd (Rutile)")
        assert problems

    def test_correct_pairings_in_one_sentence_pass(self):
        result = verify_numeric_claims(
            "Rutile (P4_2/mnm) differs from anatase (I4_1/amd) in density.",
            [TOOL_OUTPUT],
        )
        assert result.passed, result.problems
        assert result.mislabelled_structures == []

    def test_a_name_used_alone_is_not_a_mislabelling(self):
        problems = extract_structure_mislabels(
            "TiO2 exists as rutile, anatase and brookite."
        )
        assert problems == []

    def test_a_space_group_used_alone_is_not_a_mislabelling(self):
        problems = extract_structure_mislabels(
            "The P4_2/mnm entry has a gap of 1.8 eV."
        )
        assert problems == []

    def test_a_distant_name_does_not_attach_to_an_unrelated_symbol(self):
        problems = extract_structure_mislabels(
            "Rutile is one polymorph. "
            + ("x" * 200)
            + " The I4_1/amd entry is listed separately."
        )
        assert problems == []

    def test_an_element_symbol_is_not_read_as_a_space_group(self):
        """A general Hermann-Mauguin pattern would match Ba and Fm."""
        problems = extract_structure_mislabels(
            "Rutile contains Ba and Ca impurities in this sample."
        )
        assert problems == []

    def test_serialisation_includes_the_mislabels(self):
        payload = verify_numeric_claims(
            "The I4_1/amd structure (Rutile) is listed.", [TOOL_OUTPUT]
        ).to_dict()
        assert payload["passed"] is False
        assert payload["mislabelled_structures"]


class TestToolResultsAreBoundedWithoutDistortion:
    """The Si eval case built a prompt larger than the model's context and got
    no answer at all. Capping the result is the fix, but a cap that quietly
    shortens a list is worse than the overflow, so these tests pin down that
    nothing is altered and that the omission is declared."""

    def _payload(self, n: int) -> dict:
        return {
            "composition": "Si",
            "n_structure_matched_groups": n,
            "comparisons": [
                {"group_id": i, "value": -1.0 - i / 1000, "note": "x" * 500}
                for i in range(n)
            ],
        }

    def test_a_small_result_is_returned_byte_for_byte(self):
        payload = self._payload(2)
        assert bound_tool_result(payload) == json.dumps(payload)

    def test_an_oversized_result_is_capped(self):
        text = bound_tool_result(self._payload(400), budget=20_000)
        assert len(text) <= 20_000

    def test_the_omission_is_declared_with_a_count(self):
        payload = self._payload(400)
        out = json.loads(bound_tool_result(payload, budget=20_000))
        assert "truncated" in out
        kept = len(out["comparisons"])
        assert out["truncated"]["entries_omitted"]["comparisons"] == 400 - kept

    def test_retained_entries_are_unaltered_and_scalars_survive(self):
        payload = self._payload(400)
        out = json.loads(bound_tool_result(payload, budget=20_000))
        # The count field still reports the true total, not the kept total.
        assert out["n_structure_matched_groups"] == 400
        assert out["composition"] == "Si"
        for kept, original in zip(out["comparisons"], payload["comparisons"]):
            assert kept == original

    def test_one_entry_survives_even_when_a_single_entry_is_oversized(self):
        payload = {"comparisons": [{"note": "x" * 50_000}, {"note": "y" * 50_000}]}
        out = json.loads(bound_tool_result(payload, budget=1_000))
        assert len(out["comparisons"]) == 1
        assert out["truncated"]["entries_omitted"]["comparisons"] == 1


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


class TestFailedEvalRunDoesNotDestroyEvidence:
    """An exhausted credit balance makes every case raise before answering.
    Overwriting a completed run with that would erase the only live evidence
    the guard has. The failed run still has to be kept, because it can also be
    a genuine total regression.
    """

    def _write_eval_results(self):
        import importlib.util

        from materials_trust import config

        path = config.PROJECT_ROOT / "scripts" / "verify_agent.py"
        spec = importlib.util.spec_from_file_location("verify_agent_under_test", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module.write_eval_results

    def test_an_all_failed_run_is_written_beside_existing_results(
        self, tmp_path, monkeypatch
    ):
        from materials_trust import config

        monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
        existing = {"n_cases": 12, "n_passed": 11, "results": [{"passed": True}]}
        (tmp_path / "agent_eval.json").write_text(
            json.dumps(existing), encoding="utf-8"
        )
        failed = {
            "n_cases": 12,
            "n_passed": 0,
            "results": [
                {"id": "a", "passed": False, "error": "BadRequestError: billing"},
                {"id": "b", "passed": False, "error": "BadRequestError: billing"},
            ],
        }
        written = self._write_eval_results()(failed)
        assert written.name == "agent_eval_failed_run.json"
        kept = json.loads((tmp_path / "agent_eval.json").read_text(encoding="utf-8"))
        assert kept == existing
        saved = json.loads(written.read_text(encoding="utf-8"))
        assert saved["n_passed"] == 0
        assert "infrastructure failure" in saved["note"]

    def test_a_real_eval_run_still_overwrites(self, tmp_path, monkeypatch):
        from materials_trust import config

        monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
        (tmp_path / "agent_eval.json").write_text("{}", encoding="utf-8")
        payload = {
            "n_cases": 1,
            "n_passed": 1,
            "results": [{"id": "a", "passed": True}],
        }
        written = self._write_eval_results()(payload)
        assert written.name == "agent_eval.json"
        assert json.loads(written.read_text(encoding="utf-8"))["n_passed"] == 1
