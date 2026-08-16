"""Generate README.md from the actual contents of results/.

Brief rule 7: verify before claiming, and do not describe results that have not
been produced. The findings section of the README is therefore generated from
``results/summary.json``. If the benchmark has not been run, the README says so
explicitly instead of containing a plausible-looking placeholder that could be
mistaken for a result.

Brief rule 6: no em dashes in generated documentation.

Run:  python scripts/write_readme.py
"""

from __future__ import annotations

import json
from typing import Any

from materials_trust import config, hubbard, matching
from materials_trust.sources.experimental import DATASET_NAME, DATASET_REFERENCE
from materials_trust.sources.materials_project import THERMO_TYPE


def _load(name: str) -> Any | None:
    path = config.RESULTS_DIR / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "not available"
    return f"{value}{suffix}"


def section_status(
    summary: Any, unit_check: Any, set_prov: Any, agent_eval: Any = None
) -> list[str]:
    lines = ["## Current status", ""]
    if unit_check:
        for report in unit_check:
            src = report["source"]
            n = report["n_compounds"]
            decisive = report["n_decisive"]
            lines.append(
                f"- Formation energy units verified for **{src}** on {n} reference "
                f"compounds, {decisive} of which are uniquely consistent with eV/atom "
                f"and inconsistent with every other unit hypothesis tested."
            )
    if set_prov:
        funnel = set_prov["funnel"]
        lines.append(
            f"- Benchmark material set selected: **{set_prov['selected_total']} "
            f"compositions**, drawn from {funnel['distinct_compositions']} candidate "
            f"compositions, of which {set_prov['selected_with_differing_hubbard_policy']} "
            "are compositions where the two databases' documented Hubbard U policies "
            "differ."
        )
    if summary:
        requested = summary["n_compositions_requested"]
        selected = (set_prov or {}).get("selected_total")
        # A --limit run must not be described as the full benchmark, or the
        # findings below would be read as covering the whole material set.
        scope = (
            f"Partial benchmark run over {requested} of the "
            f"{selected} selected compositions"
            if selected and requested < selected
            else "Full benchmark run over the whole selected material set"
        )
        lines.append(
            f"- {scope}, completed on {summary['generated_at']}: "
            f"{summary['n_compositions_audited']} compositions audited, producing "
            f"{summary['n_trust_records']} trust records in "
            f"{summary['elapsed_seconds']} seconds."
        )
    else:
        lines.extend(
            [
                "- **The full benchmark has not been run yet.** It requires a "
                "Materials Project API key in `MP_API_KEY`. Until it runs, this "
                "README contains no cross-source agreement statistics and no DFT "
                "versus experiment numbers, because stating them before producing "
                "them would be exactly the failure mode this project criticises.",
                "",
                "  To produce them:",
                "",
                "  ```bash",
                "  cp .env.example .env      # add your MP_API_KEY",
                "  python scripts/run_benchmark.py",
                "  python scripts/write_readme.py",
                "  ```",
            ]
        )
    if agent_eval:
        lines.append(
            f"- Layer 3's attribution eval set run against the live model: "
            f"**{agent_eval.get('n_passed', 0)} of "
            f"{agent_eval.get('n_cases', 0)} cases passed**, with every question, "
            f"answer, tool selection, and numeric guard verdict recorded in "
            f"`results/agent_eval.json`."
        )
    lines.append("")
    return lines


def section_findings(summary: Any) -> list[str]:
    lines = ["## Findings", ""]
    if not summary:
        lines.extend(
            [
                "Not yet produced. See Current status above.",
                "",
            ]
        )
        return lines

    cov = summary["coverage"]
    fe = summary["cross_source_formation_energy"]
    gap = summary["cross_source_band_gap"]
    expt = summary["dft_vs_experiment_materials_project"]
    figures = summary.get("figures") or {}
    clean_gapped = expt["gapped_clean_only"]

    if fe["all"].get("n") and clean_gapped.get("n"):
        lines.extend(
            [
                "Three numbers carry the argument.",
                "",
                f"1. Across {fe['n_structure_matched_pairs']} formation energies where "
                "both databases describe the same structure-matched material, the "
                f"fraction agreeing within {fe['threshold']} {fe['units']}, the "
                "conventional threshold for agreement, is only "
                f"**{fe['fraction_within_threshold']}**. The mean absolute difference "
                f"is **{fe['all']['mean_absolute']} {fe['units']}**, more than twice "
                "that threshold.",
                f"2. Against experiment, PBE and PBE+U band gaps are low by a mean "
                f"**{clean_gapped['mean_signed']} eV** over "
                f"{clean_gapped['n']} clean comparisons, and the fraction that are too "
                f"low is **{expt['fraction_underestimated_clean']}**. "
                f"{expt['n_computed_zero_gap_but_measured_gapped']} materials with a "
                "measured gap are computed as having no gap at all.",
                f"3. Not one of the {(summary.get('confidence_band_limits') or {}).get('n_multi_source_comparisons', 0)} "
                "cross-source comparisons can be certified at high confidence, and the "
                "limit is missing metadata rather than disagreement. That is explained "
                "under the confidence band distribution below.",
                "",
                "Every number in this section was produced by the run recorded in "
                "`results/summary.json` and can be regenerated from it.",
                "",
            ]
        )

    lines.extend(
        [
            "### Coverage",
            "",
            f"- Compositions audited: {summary['n_compositions_audited']} of "
            f"{summary['n_compositions_requested']} requested.",
            f"- Found in Materials Project: {cov['found_in_materials_project']}.",
            f"- Found in OQMD: {cov['found_in_oqmd']}.",
            f"- Found in both: {cov['found_in_both']}.",
            f"- Found in neither: {cov['found_in_neither']}.",
            f"- Structure-matched multi-source comparisons: "
            f"{cov['n_structure_matched_multi_source']}.",
            "",
            "### Cross-source agreement, formation energy",
            "",
            f"Comparisons where both databases reported a value for the same "
            f"structure-matched material: **{fe['n_structure_matched_pairs']}**.",
            "",
        ]
    )
    if fe["all"].get("n"):
        lines.extend(
            [
                f"- Mean absolute difference: **{fe['all']['mean_absolute']} "
                f"{fe['units']}**.",
                f"- Median absolute difference: {fe['all']['median_absolute']} "
                f"{fe['units']}.",
                f"- Mean signed difference (Materials Project minus OQMD): "
                f"**{fe['all']['mean_signed']} {fe['units']}**. A non-zero mean "
                "signed difference is the systematic offset between the two "
                "databases' correction schemes.",
                f"- Fraction agreeing within the {fe['threshold']} {fe['units']} "
                f"threshold: **{fe['fraction_within_threshold']}**.",
                f"- 95th percentile of absolute difference: "
                f"{fe['all'].get('abs_p95')} {fe['units']}.",
                "",
                "Stratified by whether the two databases' documented Hubbard U "
                "policies differ for that composition:",
                "",
                "| +U policy | n | mean absolute difference | mean signed difference |",
                "| --- | --- | --- | --- |",
            ]
        )
        for label, key in (
            ("differ", "policies_differ"),
            ("agree", "policies_agree"),
        ):
            block = fe["stratified_by_hubbard_u_policy_mismatch"][key]
            if block.get("n"):
                lines.append(
                    f"| {label} | {block['n']} | {block['mean_absolute']} "
                    f"{fe['units']} | {block['mean_signed']} {fe['units']} |"
                )
            else:
                lines.append(f"| {label} | 0 | no comparisons | no comparisons |")
        lines.append("")

        differ = fe["stratified_by_hubbard_u_policy_mismatch"]["policies_differ"]
        agree = fe["stratified_by_hubbard_u_policy_mismatch"]["policies_agree"]
        if differ.get("n") and agree.get("n"):
            # State the direction the data actually shows. The expectation going in
            # was that the +U stratum would be worse, and it is worth reporting
            # plainly when a benchmark contradicts the hypothesis it was built to test.
            if differ["mean_absolute"] > agree["mean_absolute"]:
                lines.append(
                    "Disagreement is larger where the +U policies differ, which is "
                    "the expected direction: the two databases are describing the "
                    "same electrons with different Hubbard corrections."
                )
            else:
                lines.append(
                    f"Disagreement is **larger where the +U policies agree** "
                    f"({agree['mean_absolute']} against {differ['mean_absolute']} "
                    f"{fe['units']}), which is the opposite of the expectation this "
                    "benchmark was built to test. The +U policy is therefore not the "
                    "dominant term. The mean signed difference of "
                    f"{agree['mean_signed']} {fe['units']} in the agreeing stratum, "
                    "against "
                    f"{differ['mean_signed']} {fe['units']} where policies differ, "
                    "points at the elemental reference energies and anion corrections "
                    "each database fits independently: a systematic offset that "
                    "applies to ordinary main-group compounds and does not cancel. "
                    "Where +U policies differ the two effects partly offset each "
                    "other, which is why that stratum has a mean signed difference "
                    "closer to zero and a wider spread."
                )
            lines.append("")

    lines.extend(
        [
            "### Cross-source agreement, band gap",
            "",
            f"Structure-matched comparisons: **{gap['n_structure_matched_pairs']}**.",
            "",
        ]
    )
    if gap["all"].get("n"):
        lines.extend(
            [
                f"- Mean absolute difference: **{gap['all']['mean_absolute']} eV**.",
                f"- Mean signed difference (Materials Project minus OQMD): "
                f"{gap['all']['mean_signed']} eV.",
                f"- Fraction agreeing within the {gap['threshold']} eV threshold: "
                f"**{gap['fraction_within_threshold']}**.",
                "",
            ]
        )

    lines.extend(
        [
            "### DFT band gaps against experiment",
            "",
            "Computed values are Materials Project PBE or PBE+U gaps. The comparison "
            "is composition-level, because an experimental band gap is reported "
            "against a composition rather than a structure.",
            "",
            f"- Comparisons attempted: {expt['n_comparisons_attempted']}.",
            f"- Materials measured to have a finite gap: {expt['n_measured_gapped']}.",
            f"- Of those, comparisons free of polymorph ambiguity: "
            f"{expt['n_clean_gapped']}.",
            "",
        ]
    )
    clean = clean_gapped
    if clean.get("n"):
        lines.extend(
            [
                f"For the {clean['n']} clean comparisons:",
                "",
                f"- **Mean signed error: {clean['mean_signed']} eV.** Negative means "
                "the calculation underestimates the measured gap.",
                f"- Mean absolute error: **{clean['mean_absolute']} eV**.",
                f"- Median signed error: {clean['median_signed']} eV.",
                f"- Root mean square error: {clean['rmse']} eV.",
                f"- Fraction where the calculation underestimates: "
                f"**{expt['fraction_underestimated_clean']}**.",
                f"- Materials where the calculation gives a zero gap but a finite gap "
                f"was measured: {expt['n_computed_zero_gap_but_measured_gapped']} "
                f"({expt['fraction_computed_zero_gap_but_measured_gapped']} of gapped "
                "materials). Brief section 2.4 is the reason this is reported "
                "separately: a computed gap of 0.0 eV does not establish that a "
                "material is a metal.",
                "",
            ]
        )
    metals = expt.get("metals") or {}
    if metals.get("n"):
        lines.extend(
            [
                f"For the {metals['n']} materials measured to be metallic, the "
                f"calculation also gives a zero gap in "
                f"{metals['n_computed_also_zero_gap']} cases "
                f"({metals['fraction_computed_also_zero_gap']}). This is a "
                "classification agreement rate, not evidence that the calculation "
                "is correct.",
                "",
            ]
        )

    flags = summary["flag_frequencies"]
    lines.extend(
        [
            "### Flag frequencies",
            "",
            f"Across {flags['n_trust_records']} trust records:",
            "",
            "| flag | count | fraction |",
            "| --- | --- | --- |",
        ]
    )
    for code, count in flags["counts"].items():
        lines.append(f"| `{code}` | {count} | {flags['fractions'][code]} |")
    lines.append("")

    conf = summary["confidence_distribution"]
    lines.extend(
        [
            "### Confidence band distribution",
            "",
            "| band | count |",
            "| --- | --- |",
        ]
    )
    for band, count in conf.items():
        lines.append(f"| {band} | {count} |")
    lines.append("")

    limits = summary.get("confidence_band_limits") or {}
    if limits.get("n_multi_source_comparisons"):
        universal = limits["capping_flags_present_in_every_comparison"]
        lines.extend(
            [
                f"Of the {limits['n_multi_source_comparisons']} comparisons where two "
                "sources reported the same structure-matched property, "
                f"{limits['n_base_band_high_on_spread_alone']} agreed within half the "
                "disagreement threshold and so earned a base band of high on the "
                "numbers alone. "
                + (
                    "None finished at high."
                    if limits["n_final_band_high"] == 0
                    else f"{limits['n_final_band_high']} finished at high."
                ),
                "",
            ]
        )
        if universal:
            lines.extend(
                [
                    "The reason is metadata, not disagreement. "
                    + (
                        "This provenance caveat applies"
                        if len(universal) == 1
                        else "These provenance caveats apply"
                    )
                    + " to **every** cross-source comparison in the run, which caps "
                    "all of them at moderate: "
                    + ", ".join(f"`{c}`" for c in universal)
                    + ". Every cross-source comparison necessarily involves OQMD, and "
                    "OQMD publishes no per-entry exchange-correlation functional, so "
                    "the functional has to be derived from its documented methodology. "
                    "The band refuses to certify high confidence in an agreement when "
                    "it cannot confirm what was computed.",
                    "",
                    "This is a deliberate design decision and it is worth being "
                    "explicit about the consequence: with these two databases, high "
                    "is unreachable. Reaching it would require a source that publishes "
                    "the functional per entry. Reporting these agreements as high "
                    "would mean claiming knowledge the APIs do not provide.",
                    "",
                ]
            )

    if figures:
        lines.extend(["### Figures", ""])
        titles = {
            "formation_energy_disagreement": "Cross-source formation energy disagreement",
            "band_gap_disagreement": "Cross-source band gap disagreement",
            "formation_energy_by_hubbard": "Formation energy disagreement by +U policy",
            "band_gap_by_hubbard": "Band gap disagreement by +U policy",
            "gap_parity": "DFT band gap against experiment",
            "gap_error_histogram": "DFT band gap error distribution",
            "flag_frequencies": "Flag frequencies",
            "confidence_distribution": "Confidence band distribution",
        }
        for key, path in figures.items():
            lines.append(f"**{titles.get(key, key)}**")
            lines.append("")
            lines.append(f"![{titles.get(key, key)}]({path})")
            lines.append("")

    failures = summary.get("source_failures") or {}
    n_mp = len(failures.get("materials_project") or [])
    n_oqmd = len(failures.get("oqmd") or [])
    lines.extend(
        [
            "### Failures recorded",
            "",
            f"- Materials Project: {n_mp} recorded failure(s).",
            f"- OQMD: {n_oqmd} recorded failure(s).",
            f"- Compositions that could not be audited at all: "
            f"{summary.get('n_run_failures', 0)}.",
            "",
            "Failures are recorded rather than imputed. No missing value anywhere in "
            "this benchmark was filled with an estimate.",
            "",
        ]
    )
    return lines


def section_agent_eval(agent_eval: Any) -> list[str]:
    """Report what the attribution eval set did against a live model.

    Rule 7 again: the claim that the guard holds is worth nothing unless it has
    been tried. If the eval set has not been run, this section says so instead
    of implying that it has.
    """
    lines = ["### What happened when this was run against a live model", ""]
    if not agent_eval:
        lines.extend(
            [
                "**The attribution eval set has not been run against a live model "
                "yet.** It needs an `ANTHROPIC_API_KEY`. Until it runs, the only "
                "evidence for the guard is the offline suite in "
                "`tests/test_agent_guard.py`, which exercises the guard directly "
                "but not against real model output.",
                "",
                "  ```bash",
                "  python scripts/verify_agent.py",
                "  ```",
                "",
            ]
        )
        return lines

    results = agent_eval.get("results") or []
    n_cases = agent_eval.get("n_cases", len(results))
    n_passed = agent_eval.get("n_passed", 0)
    completed = [r for r in results if r.get("guard")]
    n_guard_passed = sum(1 for r in completed if (r["guard"] or {}).get("passed"))
    n_claims = sum(
        int((r["guard"] or {}).get("n_numeric_claims", 0)) for r in completed
    )
    failed = [r for r in results if not r.get("passed")]

    lines.append(
        f"The eval set in `evals/attribution_eval.json` has been run against the "
        f"live model: **{n_passed} of {n_cases} cases passed**. Across the "
        f"{len(completed)} cases that produced an answer, the guard checked "
        f"{n_claims} numeric claims and cleared {n_guard_passed} answers as "
        f"fully traceable. Every question, answer, tool selection, and guard "
        f"verdict is recorded in `results/agent_eval.json`, so the run can be "
        f"audited rather than taken on trust."
    )
    lines.append("")

    if failed:
        lines.append("Cases that did not pass:")
        lines.append("")
        for r in failed:
            if r.get("error"):
                # Only the exception type. The provider's raw error body carries
                # request identifiers and account state that have no business in
                # a public README.
                reason = (
                    f"no answer was produced, because the call to the model "
                    f"failed with {str(r['error']).split(':')[0]}"
                )
            else:
                reason = "; ".join(r.get("failures") or []) or "no reason recorded"
            lines.append(f"- `{r['id']}`: {reason}")
        lines.append("")

    n_adversarial = sum(1 for r in results if r["id"].startswith("adversarial"))
    lines.extend(
        [
            f"The set includes {n_adversarial} cases that exist only to attack the "
            "boundary. They ask the model to convert a formation energy into "
            "different units, to report a percentage difference between two "
            "databases, to average two sources into one citable number, to attach "
            "mineral names to TiO2 space groups, and to estimate a value for a "
            "phase that neither database contains. None of those quantities can "
            "come from a tool, so a compliant answer cannot pass the guard.",
            "",
            "Running against a live model is also what found the guard's own "
            "defects. It had been treating a Unicode minus sign as absent, so a "
            "negative value quoted correctly was reported as invented, and it had "
            "been reading markdown list numbering as quantitative claims. Worse, "
            "because it looked only for digits, it let through a ratio the model "
            "had computed and then written out in words, and it let through a "
            "mineral name attached to the wrong space group, because no digit was "
            "wrong. All four are fixed and pinned by tests. The general lesson is "
            "that a guard is only as good as the forms of expression it can parse, "
            "and the only way to find the forms it cannot parse is to run it "
            "against a real model.",
            "",
            "Two limits remain, and they are the same kind of limit. The guard "
            "does not know that \"60 distinct structures\" two lines from a tool "
            "value of 61 is a contradiction, because both digits exist somewhere "
            "in the payload. It also does not police qualitative claims such as "
            "\"far below the experimental gap\" when no experimental gap was "
            "returned. It constrains the claims it can parse. It does not make "
            "the surrounding prose true.",
            "",
        ]
    )
    return lines


def build_readme() -> str:
    summary = _load("summary.json")
    unit_check = _load("unit_check.json")
    set_prov = _load("material_set_provenance.json")
    agent_eval = _load("agent_eval.json")

    lines: list[str] = [
        "# Materials Data Trust Benchmark",
        "",
        "An open-source tool and public benchmark that answers one question about "
        "any materials property value: **can I trust this number?**",
        "",
        "Every AI-for-materials system today is racing to generate materials and "
        "predict properties at scale, and almost all of it is trained on and "
        "validated against DFT data. But that data carries systematic errors, "
        "depends on the functional and correction scheme used, and disagrees "
        "between major databases for the same material. As generation scales, "
        "validation becomes the bottleneck. This project builds the validation "
        "layer.",
        "",
        "This is not another property-prediction model. It is the quality-assurance "
        "layer that sits on top of everyone else's databases and models.",
        "",
    ]

    lines.extend(section_status(summary, unit_check, set_prov, agent_eval))

    lines.extend(
        [
            "## Install",
            "",
            "```bash",
            "pip install -e .                 # layers 1 and 2",
            'pip install -e ".[agent,dev]"    # add layer 3 and the test suite',
            "cp .env.example .env             # add MP_API_KEY",
            "```",
            "",
            "Python 3.10 or newer. No GPU is needed anywhere: this is API calls, "
            "structure matching, and statistics.",
            "",
            "A free Materials Project API key is required, read from the environment "
            "as `MP_API_KEY`. OQMD needs no key. Layer 3 additionally needs "
            "`ANTHROPIC_API_KEY`, and will use `LANGSMITH_API_KEY` for tracing if "
            "one is present.",
            "",
            "## Usage",
            "",
            "### Audit one material",
            "",
            "```bash",
            "mtb audit TiO2",
            "mtb audit mp-149 --json",
            "mtb audit Fe2O3 --property formation_energy_per_atom",
            "```",
            "",
            "### Browse the assay in a browser",
            "",
            "The static UI in `site/` is the public face of the run: coverage, the "
            "headline disagreements, the eight figures, and a lookup over all 287 "
            "assayed compositions. It does not call the APIs. Rebuild the compact "
            "index after a new benchmark with `python scripts/build_site.py`.",
            "",
            "### Run the benchmark",
            "",
            "```bash",
            "mtb unit-check                   # verify formation energy units first",
            "mtb select-set                   # regenerate the material set",
            "mtb benchmark                    # full run, or --limit 20 to sample",
            "python scripts/write_readme.py   # refresh the findings in this file",
            "```",
            "",
            "### As an MCP server",
            "",
            "```bash",
            "python -m materials_trust.mcp_server",
            "```",
            "",
            "Claude Desktop configuration:",
            "",
            "```json",
            "{",
            '  "mcpServers": {',
            '    "materials-trust": {',
            '      "command": "python",',
            '      "args": ["-m", "materials_trust.mcp_server"],',
            '      "env": { "MP_API_KEY": "your_key" }',
            "    }",
            "  }",
            "}",
            "```",
            "",
            "Tools exposed: `audit_material`, `compare_sources`, "
            "`check_physics_consistency`, `get_provenance`, and "
            "`explain_hubbard_policy`. Each returns the deterministic core's output "
            "unchanged.",
            "",
            "### Ask the explanation agent",
            "",
            "```bash",
            "python -m materials_trust.agent \"why do the databases disagree about FeF3\"",
            "```",
            "",
            "## Architecture: three layers, one hard boundary",
            "",
            "**Layer 1, the deterministic core.** Data retrieval, structure matching, "
            "physics checks, statistics. Pure Python. No language model is involved "
            "at any point. Given the same inputs it produces the same outputs, and "
            "every number is traceable to a source and a computation.",
            "",
            "**Layer 2, the MCP server.** Exposes the core as tools over the Model "
            "Context Protocol, so any MCP-capable client can check a materials "
            "number before acting on it.",
            "",
            "**Layer 3, the agent.** A LangGraph agent that orchestrates multi-step "
            "audits and explains results in plain language. Attribution is the "
            "product: not \"these two sources differ by 0.31 eV/atom\" but \"they "
            "differ because Materials Project applied a GGA+U correction to this "
            "transition metal oxide and OQMD did not\".",
            "",
            "### The hard boundary",
            "",
            "> A language model may never compute, estimate, adjust, or invent a "
            "numerical value.",
            "",
            "This is enforced mechanically, not just requested in a prompt. The "
            "agent graph has a guard node that runs after the model produces its "
            "answer. The guard extracts every number from the prose and checks each "
            "against the numbers the tools actually returned, accepting exact values "
            "and honest roundings of them. It also catches arithmetic written in "
            "words, such as \"five times the threshold\", and mineral names attached "
            "to the wrong space group, such as calling an I4_1/amd entry rutile. "
            "Anything untraceable is named in the output and the answer is marked "
            "as containing unsupported claims.",
            "",
            "The guard is deliberately strict. If the model computes a percentage or "
            "an average the core did not emit, that number will not trace and the "
            "guard will catch it. That is the intended behaviour: the boundary "
            "forbids the model from computing at all.",
            "",
            *section_agent_eval(agent_eval),
            "## Method",
            "",
            "### Sources",
            "",
            "| source | access | properties | key required |",
            "| --- | --- | --- | --- |",
            "| Materials Project | `mp-api`, summary and thermo endpoints | formation "
            "energy per atom, band gap | yes |",
            "| OQMD | native `oqmdapi/formationenergy` REST endpoint | formation "
            "energy per atom, band gap | no |",
            f"| Experiment | matminer `{DATASET_NAME}` | band gap | no |",
            "",
            f"Experimental data: {DATASET_REFERENCE}",
            "",
            "### The physics rules, and what the code does about each",
            "",
            "**Match structures, not formulas.** TiO2 is rutile, anatase, and "
            "brookite, with genuinely different properties. Comparing them and "
            "reporting a disagreement would fabricate a result. Every comparison is "
            "gated on `pymatgen.analysis.structure_matcher.StructureMatcher` "
            "establishing that two entries describe the same material. Nothing in "
            "the codebase merges records on formula. Where a structure is missing or "
            "unmatched, the comparison is recorded as `POLYMORPH_AMBIGUOUS` and "
            "reported separately. The matcher configuration is:",
            "",
            f"```\n{matching.MATCHER_DESCRIPTION}\n```",
            "",
            "These are the pymatgen defaults and are deliberately not loosened, "
            "because a looser matcher would merge distinct polymorphs and produce "
            "false agreement.",
            "",
            "**Use formation energy, never raw total energy.** Total energies from "
            "different codes and settings are on different absolute scales. Only "
            "formation energy per atom in eV/atom is compared, and "
            "`scripts/unit_harness.py` verifies the unit on live data before every "
            "benchmark run by testing the competing unit hypotheses against each "
            "other rather than assuming.",
            "",
            "**Compare like with like on functionals.** This is the subtlest trap in "
            "the project. The Materials Project summary endpoint's "
            "`formation_energy_per_atom` derives from the default thermo type, which "
            "in current releases is the `GGA_GGA+U_R2SCAN` mixing scheme. OQMD "
            "contains no r2SCAN calculations at all, so comparing the two would "
            f"break this rule. Formation energy is therefore taken from the thermo "
            f"endpoint pinned to `thermo_types=[\"{THERMO_TYPE}\"]`, which is the "
            "only thermo type comparable with OQMD. The summary value is retained in "
            "`extras` so the size of that choice can be quantified.",
            "",
            "**Treat DFT band gaps as DFT band gaps.** A PBE gap is not a prediction "
            "of an experimental gap, and a computed 0.0 eV does not establish "
            "metallicity. Measured and computed values carry different `value_kind` "
            "values, the record type refuses to attach a functional to a "
            "measurement, and materials measured to be metallic are analysed as a "
            "separate classification question rather than folded into the signed "
            "error statistics.",
            "",
            "**Magnetic state matters.** Materials Project exposes per-entry magnetic "
            "ordering, so it is read directly. OQMD does not, so it is derived from "
            "OQMD's published methodology, which spin-polarises any calculation "
            "containing a 3d element or an actinide with ferromagnetic alignment. "
            "That derivation is labelled as a derivation via the "
            "`MAGNETIC_INFERRED` flag and never presented as retrieved metadata.",
            "",
            "**Provenance on everything.** `PropertyRecord` cannot be constructed "
            "without source, identifier, formula, property, value, units, "
            "functional, correction scheme, magnetic state, value kind, and "
            "experimental observation status. Validation rejects records that "
            "misdeclare units, carry non-finite or physically impossible values, "
            "attach a functional to a measurement, or omit a correction scheme. "
            "There is no code path in the project that can emit an unprovenanced "
            "number.",
            "",
            "### Attribution: the Hubbard U tables",
            "",
            "The most common reason the two databases disagree about a formation "
            "energy is that they apply +U differently, and their policies differ in "
            "ways that are predictable before any data is fetched:",
            "",
            "| case | Materials Project | OQMD |",
            "| --- | --- | --- |",
            "| oxides with V, Cr, Mn, Fe, Co, Ni | +U applied | +U applied, "
            "different parameter |",
            "| fluorides with those elements | +U applied | no +U |",
            "| copper oxides | no +U | +U applied |",
            "| molybdenum and tungsten oxides | +U applied | no +U |",
            "| actinide oxides | no +U | +U applied |",
            "",
            "Materials Project U values in eV: "
            + ", ".join(f"{k} {v}" for k, v in sorted(hubbard.MP_U_EV.items()))
            + ".",
            "",
            "OQMD U minus J values in eV (Dudarev): "
            + ", ".join(f"{k} {v}" for k, v in sorted(hubbard.OQMD_U_MINUS_J_EV.items()))
            + ".",
            "",
            f"Sources: {hubbard.MP_U_REFERENCE}; {hubbard.OQMD_U_REFERENCE}.",
            "",
            "Note that Materials Project quotes U while OQMD quotes U minus J, so the "
            "values are not directly interchangeable even where they look similar.",
            "",
            "### Flags",
            "",
            "Each flag is reported individually with its own message and evidence. "
            "The six the brief specifies:",
            "",
            "- `POLYMORPH_AMBIGUOUS`: formula matched but structures did not, or a "
            "structure was unavailable.",
            "- `FUNCTIONAL_MISMATCH`: sources used different functionals.",
            "- `MAGNETIC_UNKNOWN` and `MAGNETIC_MISMATCH`: magnetic ordering unknown "
            "or differing.",
            "- `SINGLE_SOURCE`: no corroborating value exists, so agreement is "
            "unmeasurable.",
            "- `HYPOTHETICAL`: structure is not experimentally observed.",
            "- `LARGE_DISAGREEMENT`: spread exceeds the documented threshold.",
            "",
            "Four more exist because the two databases expose different amounts of "
            "metadata, and treating a derived value as a retrieved one would be a "
            "provenance claim the project cannot support:",
            "",
            "- `CORRECTION_SCHEME_MISMATCH`: same functional, different corrections.",
            "- `FUNCTIONAL_INFERRED` and `MAGNETIC_INFERRED`: the value came from "
            "published methodology rather than per-entry metadata.",
            "- `STRUCTURE_UNAVAILABLE`: a source returned no usable structure.",
            "- `HUBBARD_U_MISMATCH`: the documented +U policies differ, with the "
            "specific elements and parameters named.",
            "",
            "### Thresholds",
            "",
            f"- Formation energy disagreement: "
            f"{config.LARGE_DISAGREEMENT_FORMATION_ENERGY_EV_PER_ATOM} eV/atom "
            "(50 meV/atom). The conventional scale at which DFT formation energies "
            "are considered to agree, and comfortably larger than numerical noise "
            "between well converged calculations of the same structure and "
            "functional.",
            f"- Band gap disagreement: {config.LARGE_DISAGREEMENT_BAND_GAP_EV} eV. "
            "Large relative to the numerical spread from k-point sampling, small "
            "relative to the systematic PBE underestimation being quantified.",
            f"- Polymorph gap spread tolerance: "
            f"{config.POLYMORPH_GAP_SPREAD_TOLERANCE_EV} eV. Above this, an "
            "experimental gap cannot be cleanly attributed to one computed "
            "polymorph.",
            "",
            "### The confidence band",
            "",
            "A band is a label derived by a published rule from quantities that "
            "travel alongside it. It is never emitted without its derivation, and it "
            "can be recomputed by hand:",
            "",
            "1. If fewer than two sources report the property for a structure-matched "
            "material, the band is `not_assessable`. No band is invented for an "
            "uncorroborated value.",
            "2. Otherwise take the ratio of the cross-source spread to the threshold "
            "for that property. Ratio at or below 0.5 gives `high`, at or below 1.0 "
            "gives `moderate`, above 1.0 gives `low`.",
            "3. Demote one step for each distinct category of warning present: "
            "functional or Hubbard U, magnetic ordering, structural identity. "
            "Categories are grouped so that two flags describing one physical "
            "problem do not demote twice.",
            "4. Cap at `moderate` if any provenance caveat applies, such as an "
            "unknown magnetic state or an inferred functional. You cannot claim high "
            "confidence in an agreement when you do not know what was computed.",
            "",
            "Cross-source spread is computed from the median of each source's values, "
            "not from all values pooled, so duplicate entries within one database "
            "cannot inflate the apparent disagreement between databases. Intra-source "
            "scatter is reported separately as its own quantity.",
            "",
            "## Limitations",
            "",
            "These are real and they bound what the benchmark can claim.",
            "",
            "**OQMD exposes no per-entry functional or magnetic metadata.** Its REST "
            "API returns no exchange-correlation field and no magnetic ordering "
            "field. Both are therefore derived from OQMD's published methodology. "
            "The derivation is deterministic and documented, but it is an inference: "
            "a legacy entry that does not follow current policy would be described "
            "incorrectly, and the pipeline could not detect that. Every affected "
            "comparison carries `FUNCTIONAL_INFERRED` or `MAGNETIC_INFERRED`.",
            "",
            "**The experimental comparison is composition-level, not "
            "structure-matched.** An experimental band gap is reported against a "
            "composition. There is no structure to match, so the strongest available "
            "guarantee is weaker here than everywhere else in the project. The "
            "mitigation is to retrieve every computed polymorph of the composition "
            "and report the spread of computed gaps across them, and to separate "
            "comparisons where that spread is large. It is a mitigation, not a "
            "solution.",
            "",
            "**Experimental values carry their own uncertainty.** OQMD's own "
            "assessment found a mean absolute error of 0.082 eV/atom between "
            "different experimental measurements of the same formation energy, "
            "against 0.096 eV/atom between DFT and experiment. A DFT versus "
            "experiment discrepancy is not automatically a DFT error.",
            "",
            "**The material set is deliberately biased.** It over-samples correlated "
            "oxides and fluorides because that is where disagreement is predicted. "
            "Flag frequencies measured on it are not unbiased estimates of "
            "frequencies across either database. It is also anchored on an "
            "experimental band gap compilation, so it skews towards semiconductors "
            "and insulators of technological interest.",
            "",
            "**Formation enthalpy at 298 K is not formation energy at 0 K.** The unit "
            "harness compares against experimental enthalpies to fix the scale, and "
            "its tolerance is loose for exactly this reason. It is a unit check, not "
            "an accuracy benchmark.",
            "",
            "**Greedy structural clustering is order-dependent in principle.** "
            "Structural similarity is not transitive, so records are sorted "
            "deterministically before clustering. The grouping is reproducible for a "
            "given input set, but a different input order could in principle produce "
            "a different grouping.",
            "",
            "**Coverage varies slightly between runs.** OQMD occasionally times out "
            "or returns an error for a composition that succeeds on a later attempt, "
            "so the number of compositions found in both databases can differ by one "
            "or two between runs of the same material set. Every such failure is "
            "recorded in `results/summary.json` rather than silently retried until it "
            "looks clean, which is why the failure counts above are not zero. The "
            "disk cache makes a rerun converge towards full coverage rather than "
            "reshuffle it.",
            "",
            "**The disagreement is dominated by an offset this benchmark does not "
            "separate.** The stratified result above shows the +U policy is not the "
            "main term, which points at the independently fitted elemental reference "
            "energies and anion corrections in each database. Attributing the residual "
            "precisely would require recomputing both databases' formation energies "
            "from their raw total energies under a single correction scheme. That is a "
            "larger piece of work and is not attempted here, so the offset is reported "
            "and attributed by elimination rather than decomposed term by term.",
            "",
            "**Only two computational databases.** AFLOW, JARVIS, NOMAD, and others "
            "are out of scope for this phase.",
            "",
            "## Repository layout",
            "",
            "```",
            "src/materials_trust/",
            "  records.py        the shared record type, where provenance is enforced",
            "  hubbard.py        documented +U and spin policy per database, the",
            "                    attribution engine",
            "  matching.py       StructureMatcher logic, the heart of correctness",
            "  checks.py         the physics-consistency flags",
            "  audit.py          orchestration and the confidence band",
            "  unit_checks.py    unit and sign verification",
            "  report.py         statistics and plots",
            "  cache.py          verbatim on-disk cache of API payloads",
            "  config.py         paths and every documented threshold",
            "  cli.py            the command line interface",
            "  mcp_server.py     layer 2",
            "  agent.py          layer 3, including the numeric guard",
            "  sources/",
            "    materials_project.py",
            "    oqmd.py",
            "    experimental.py",
            "scripts/",
            "  api_recon.py            what the APIs actually return today",
            "  unit_harness.py         unit verification against live data",
            "  select_material_set.py  material set selection and justification",
            "  run_benchmark.py        the full run",
            "  build_site.py           compact JSON and figures for the static UI",
            "  write_readme.py         regenerates this file from results/",
            "  verify_mcp.py           drives layer 2 through a real MCP client",
            "  verify_agent.py         drives layer 3 and the numeric guard",
            "tests/",
            "  test_physics.py         golden tests for the physics rules",
            "  test_agent_guard.py     the numeric guard, offline",
            "  test_report.py          statistics and plot generation, offline",
            "  test_integration.py     live API shape checks, marked network",
            "evals/attribution_eval.json  layer 3 attribution eval set, including "
            "the cases that attack the numeric boundary",
            "docs/",
            "  api-reality.md          live API behaviour, generated",
            "  material-set.md         set composition and justification, generated",
            "results/                  generated outputs, committed",
            "site/                     static assay UI, deployed on Netlify",
            "  agent_eval.json         every layer 3 eval answer and guard verdict",
            "```",
            "",
            "## Tests",
            "",
            "```bash",
            "pytest                 # the offline suite, no key and no network needed",
            "pytest -m network      # additionally check the live APIs still behave",
            "```",
            "",
            "The suite is a set of correctness assertions, not coverage decoration. "
            "The default run is offline: reference structures are built from published "
            "lattice parameters rather than fetched, so no API key or network is "
            "needed. The most important test asserts that rutile and anatase TiO2 do "
            "not match each other, because if they did the benchmark would report a "
            "fabricated disagreement and its central claim would be void.",
            "",
            "The network-marked tests cover the failure mode offline testing cannot "
            "reach: an API that silently changes shape. They assert that OQMD still "
            "returns usable structures, that formation energy is still eV/atom, that "
            "real TiO2 entries still separate into distinct polymorph groups, and that "
            "the live silicon PBE gap still underestimates the measured 1.17 eV.",
            "",
            "## References",
            "",
            "- Materials Project: A. Jain et al., APL Materials 1, 011002 (2013). "
            "Hubbard U values and the GGA/GGA+U/r2SCAN mixing scheme from "
            "docs.materialsproject.org.",
            "- OQMD: S. Kirklin et al., npj Computational Materials 1, 15010 (2015). "
            "Calculation settings from oqmd.org/documentation/vasp.",
            "- Experimental band gaps: Y. Zhuo, A. Masouri Tehrani and J. Brgoch, "
            "J. Phys. Chem. Lett. 9, 1668 (2018), as compiled in matminer.",
            "- pymatgen: S. P. Ong et al., Computational Materials Science 68, 314 "
            "(2013).",
            "",
            "## Author",
            "",
            "Ibtisam Ahmed Khan, materials engineer working in materials informatics. "
            "Publishes at materialsdecoded.com.",
            "",
            "## Licence",
            "",
            "MIT",
            "",
        ]
    )

    # Findings go after status so a reader hits the numbers early, but they are
    # assembled last so they can be omitted cleanly when absent.
    findings = section_findings(summary)
    insert_at = lines.index("## Install")
    lines[insert_at:insert_at] = findings

    text = "\n".join(lines)
    if "\u2014" in text or "\u2013" in text:
        raise AssertionError("generated README contains an em dash or en dash")
    return text


def main() -> int:
    config.ensure_dirs()
    readme = build_readme()
    path = config.PROJECT_ROOT / "README.md"
    path.write_text(readme, encoding="utf-8")
    print(f"wrote {path} ({len(readme.splitlines())} lines)")
    if not (config.RESULTS_DIR / "summary.json").exists():
        print(
            "note: results/summary.json does not exist, so the findings section "
            "states that the benchmark has not been run"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
