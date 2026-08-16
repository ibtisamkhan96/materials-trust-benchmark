"""Verify layer 3: the graph wiring, the tools, and the numeric guard.

Structural checks run without an Anthropic key and without any model call: the
tools are invoked directly, the graph is compiled and its nodes inspected, and
the guard is demonstrated catching an invented number.

If ``ANTHROPIC_API_KEY`` is present, the attribution eval set in
``evals/attribution_eval.json`` is also run against the live model, and results
are written to ``results/agent_eval.json``.

Run:  python scripts/verify_agent.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from materials_trust import agent, config

EVAL_PATH = config.PROJECT_ROOT / "evals" / "attribution_eval.json"


def check_tools() -> list[str]:
    """Tools must be callable and must return the core's output unchanged."""
    problems: list[str] = []
    tools = agent.build_tools()
    names = {t.name for t in tools}
    expected = {
        "audit_material",
        "compare_sources",
        "get_provenance",
        "explain_hubbard_policy",
        "check_physics_consistency",
    }
    missing = expected - names
    if missing:
        problems.append(f"tools missing: {sorted(missing)}")
    print(f"tools built: {sorted(names)}")

    for tool in tools:
        if not (tool.description or "").strip():
            problems.append(f"tool {tool.name} has no description for the model to read")

    # A tool needing neither key nor network, so this exercises the wrapper only.
    by_name = {t.name: t for t in tools}
    raw = by_name["explain_hubbard_policy"].invoke({"formula": "FeF3"})
    payload = json.loads(raw)
    from materials_trust import hubbard

    if payload != hubbard.compare_hubbard_treatment("FeF3").to_dict():
        problems.append(
            "the tool wrapper altered the core's output, which breaks the guarantee "
            "that the core is the single source of truth"
        )
    else:
        print("explain_hubbard_policy returns the core's output unchanged")
    return problems


def check_graph() -> list[str]:
    """The graph must compile and must route through a guard node before ending."""
    problems: list[str] = []
    had_key = bool(config.anthropic_api_key())
    if not had_key:
        # A placeholder is enough to construct the client. No request is made, so
        # no credential is used and nothing is sent anywhere.
        os.environ["ANTHROPIC_API_KEY"] = "placeholder-for-structural-check-only"
    try:
        app = agent.build_graph()
    except Exception as exc:
        return [f"graph failed to compile: {type(exc).__name__}: {exc}"]
    finally:
        if not had_key:
            os.environ.pop("ANTHROPIC_API_KEY", None)

    try:
        graph = app.get_graph()
        nodes = set(graph.nodes)
    except Exception as exc:
        return [f"could not inspect the compiled graph: {exc}"]

    print(f"graph compiled with nodes: {sorted(n for n in nodes)}")
    for required in ("agent", "tools", "guard"):
        if required not in nodes:
            problems.append(f"graph is missing the {required!r} node")

    # The guard must be the only path to the end, or the boundary is optional.
    edges = [(e.source, e.target) for e in graph.edges]
    ends = {src for src, dst in edges if dst == "__end__"}
    if ends and ends != {"guard"}:
        problems.append(
            f"nodes other than the guard reach the end of the graph: {sorted(ends)}. "
            "The numeric guard must not be bypassable."
        )
    else:
        print("the guard is the only node that reaches the end of the graph")
    return problems


def check_guard() -> list[str]:
    """The guard must catch an invented number and pass a traceable one."""
    problems: list[str] = []
    tool_output = json.dumps({"value": -1.8492, "spread": 0.1994})

    good = agent.verify_numeric_claims("The value is -1.8492 eV/atom.", [tool_output])
    if not good.passed:
        problems.append("the guard rejected a number that came straight from a tool")

    bad = agent.verify_numeric_claims(
        "The value is -1.8492 eV/atom, which is 11.7 percent off.", [tool_output]
    )
    if bad.passed or "11.7" not in bad.unverified:
        problems.append("the guard failed to catch a computed percentage")
    else:
        print(f"guard caught the invented number: {bad.unverified}")

    # The error a live model actually made: right numbers, wrong material.
    mislabel = agent.verify_numeric_claims(
        "The I4_1/amd structure (Rutile) has a spread of 0.1994 eV.", [tool_output]
    )
    if mislabel.passed or not mislabel.mislabelled_structures:
        problems.append(
            "the guard failed to catch a mineral name attached to the space group "
            "of a different polymorph"
        )
    else:
        print(f"guard caught the mislabelled structure: {mislabel.mislabelled_structures}")

    # And it must not object to a correct pairing, or it is useless in practice.
    correct = agent.verify_numeric_claims(
        "Rutile is P4_2/mnm and anatase is I4_1/amd.", [tool_output]
    )
    if not correct.passed:
        problems.append(
            f"the guard rejected correctly paired structure labels: {correct.problems}"
        )
    return problems


def _concise_error(exc: Exception) -> str:
    """Summarise an exception as a type and a short message.

    Provider SDKs put their whole raw error response into the exception text,
    including a request id and account state. None of that belongs in a results
    file that gets committed and read by someone else, so keep the leading
    human-readable part and drop the payload that follows it.
    """
    message = str(exc).split("{", 1)[0].strip().rstrip("-").strip()
    if len(message) > 200:
        message = message[:197].rstrip() + "..."
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def write_eval_results(payload: dict[str, Any]) -> Path:
    """Persist an eval run without letting a broken run destroy a good one.

    The hazard is concrete. An expired API key or an exhausted credit balance
    makes every case raise before it answers, and writing that over a completed
    run would replace real evidence with a list of billing errors. Deleting the
    failed run instead would be the opposite mistake, since an all-failed run can
    also be a genuine regression that nobody should be able to hide.

    So neither is discarded. A run in which nothing ran is written beside the
    existing results rather than over them, and the reason is stated loudly.
    """
    target = config.RESULTS_DIR / "agent_eval.json"
    results = payload.get("results") or []
    nothing_ran = bool(results) and all(r.get("error") for r in results)

    if nothing_ran and target.exists():
        payload = {
            **payload,
            "note": (
                "every case in this run raised before producing an answer, so this "
                "records an infrastructure failure rather than an evaluation of the "
                "agent. It is kept beside agent_eval.json rather than replacing it, "
                "because the completed run there is real evidence and this is not."
            ),
        }
        target = config.RESULTS_DIR / "agent_eval_failed_run.json"
        print(
            "\nWARNING: no case produced an answer, so results/agent_eval.json was "
            "left untouched and this run was written to "
            f"{target.name} instead. Check the error field: an exhausted credit "
            "balance or an expired key looks identical to a total regression here, "
            "and only one of those is your code."
        )

    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


def _mentions(text: str, alternatives: list[str]) -> bool:
    low = text.lower()
    return any(alt.lower() in low for alt in alternatives)


def run_eval_set() -> tuple[list[dict[str, Any]], list[str]]:
    spec = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = []
    problems: list[str] = []

    for case in spec["cases"]:
        print(f"\n--- {case['id']}")
        print(f"    Q: {case['question']}")
        try:
            explanation = agent.explain(case["question"])
        except Exception as exc:
            summary = _concise_error(exc)
            problems.append(f"{case['id']}: agent raised {summary}")
            # Recorded in the same shape as a completed case, so a reader of the
            # results file does not have to special-case a missing key to find
            # out that this one never produced an answer.
            results.append(
                {
                    "id": case["id"],
                    "question": case["question"],
                    "answer": "",
                    "tools_called": [],
                    "guard": None,
                    "failures": [f"agent raised {summary}"],
                    "passed": False,
                    "error": summary,
                }
            )
            print("    result: FAIL (the agent raised before answering)")
            continue

        called = [c["name"] for c in explanation.tool_calls]
        failures: list[str] = []

        if case["expect_tools_any_of"] and not (
            set(called) & set(case["expect_tools_any_of"])
        ):
            failures.append(
                f"called {called}, expected at least one of "
                f"{case['expect_tools_any_of']}"
            )
        for alternatives in case["must_mention_any_of"]:
            if not _mentions(explanation.answer, alternatives):
                failures.append(f"answer mentions none of {alternatives}")
        for banned in case.get("must_not_mention", []):
            if banned.lower() in explanation.answer.lower():
                failures.append(f"answer mentions the banned phrase {banned!r}")
        if case["guard_must_pass"] and not explanation.guard.passed:
            failures.append(
                f"guard failed, unverified numbers {explanation.guard.unverified}, "
                f"mislabelled structures {explanation.guard.mislabelled_structures}"
            )
        # For a question no tool can answer, silence is the only correct output.
        # The guard alone would also catch a figure here, but stating the
        # expectation makes the case test something rather than rely on a side
        # effect of there being no permitted values.
        if case.get("expect_no_numeric_claims") and explanation.guard.n_claims:
            failures.append(
                f"answer makes {explanation.guard.n_claims} numeric claim(s) for a "
                "question the tools cannot answer, where none should appear"
            )

        print(f"    tools: {called}")
        print(f"    guard: {'passed' if explanation.guard.passed else 'FAILED'}")
        print(f"    result: {'PASS' if not failures else 'FAIL'}")
        for f in failures:
            print(f"      {f}")

        results.append(
            {
                "id": case["id"],
                "question": case["question"],
                "answer": explanation.answer,
                "tools_called": called,
                "guard": explanation.guard.to_dict(),
                "failures": failures,
                "passed": not failures,
            }
        )
        if failures:
            problems.append(f"{case['id']}: {'; '.join(failures)}")
    return results, problems


def main() -> int:
    config.ensure_dirs()
    # Settle tracing before anything runs, so a stale LANGSMITH_TRACING in the
    # environment without a key cannot bury real output in auth failures.
    agent.configure_tracing()
    problems: list[str] = []

    print("Checking tool wrappers")
    problems += check_tools()

    print("\nChecking graph wiring")
    problems += check_graph()

    print("\nChecking the numeric guard")
    problems += check_guard()

    if config.anthropic_api_key():
        print("\nRunning the attribution eval set against the live model")
        results, eval_problems = run_eval_set()
        n_passed = sum(1 for r in results if r.get("passed"))
        payload = {
            "n_cases": len(results),
            "n_passed": n_passed,
            "langsmith_tracing": bool(os.environ.get("LANGSMITH_TRACING") == "true"),
            "results": results,
        }
        print(f"\neval set: {n_passed}/{len(results)} passed")
        print(f"wrote {write_eval_results(payload)}")
        problems += eval_problems
    else:
        print(
            "\nSkipping the attribution eval set: ANTHROPIC_API_KEY is not set. "
            "The structural checks above do not require it."
        )

    print()
    if problems:
        for p in problems:
            print(f"FAILED: {p}", file=sys.stderr)
        return 1
    print("Layer 3 verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
