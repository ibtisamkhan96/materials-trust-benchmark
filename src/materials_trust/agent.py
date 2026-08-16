"""Layer 3: a LangGraph agent that explains, and is prevented from calculating.

Brief section 3.3 states the hard boundary: a language model may never compute,
estimate, adjust, or invent a numerical value. It may only decide which
deterministic tools to call and translate the core's structured output into
readable explanation, and every quantitative claim it makes must correspond to a
value the core actually emitted.

A prompt asking the model to behave is not enforcement. So the boundary is
enforced mechanically, as a node in the graph. After the model produces its final
answer, the guard extracts every number from the prose and checks each one
against the numbers the tools actually returned. Anything it cannot trace is
reported as unverified, and the answer is annotated with that fact rather than
being presented as though it were sound.

The guard is deliberately strict. If the model computes a percentage, an average,
or a difference that the core did not emit, that number will not be traceable and
the guard will say so. That is the intended behaviour, not a false positive: the
brief forbids the model from computing, so a computed number is exactly what
should be caught.

Running the eval set against a live model showed three things the first version
of the guard got wrong, all fixed here. It read a Unicode minus sign as absent,
so a negative value quoted correctly from a tool was reported as invented. It
read a markdown list enumerator as a quantitative claim. And because it looked
only for digits, it let through a ratio the model had computed and then written
out in words, which is the one class of error that mattered, since it was the
boundary being crossed without the guard noticing.

Graph shape:

    agent  --(tool calls)-->  tools  -->  agent
      |
      +--(no tool calls)-->  guard  -->  END
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Sequence, TypedDict

from . import config, mcp_server

DEFAULT_MODEL = "claude-sonnet-4-5-20250929"

SYSTEM_PROMPT = """\
You explain materials data quality. You have tools that run a deterministic,
physics-checked pipeline over the Materials Project and OQMD databases.

Your job is attribution: not "these values differ by 0.2 eV" but "these values
differ by 0.2 eV because Materials Project applied a GGA+U correction to this
transition metal oxide and OQMD did not".

Absolute rules:

1. Never compute, estimate, adjust, average, or invent a number. Do not derive
   percentages, differences, or means. If you want a quantity, it must appear
   verbatim in a tool result. Every number you write is checked against the tool
   output, and untraceable numbers are reported as unverified.
2. Quote numbers exactly as the tools give them, or rounded, never rescaled.
   Never convert a value into different units. eV/atom stays eV/atom.
3. Always state which source each value came from, and name the functional and
   correction scheme when discussing formation energies.
4. If the tool output does not contain what is needed to answer, say so plainly
   and stop. Do not fill the gap from your own memory. This holds even when you
   hedge: do not offer a remembered value as an aside, as a "commonly cited"
   figure, or as something the reader might check elsewhere. A recalled number
   is exactly the kind of number this system exists to keep out. Name the
   quantity in words if you must, and give no figure.
5. Do not express a comparison as a ratio in words either. "More than five
   times the threshold" and "twice the spread" are arithmetic you performed.
   State the two values the tools gave and let the reader compare them.
6. Never present a DFT band gap as a prediction of an experimental one. Standard
   PBE underestimates gaps substantially, and a computed gap of 0.0 eV does not
   establish that a material is a metal.
7. Prefer the explanations the tools already provide in their "explanations" and
   flag "message" fields. They are generated deterministically and are safe to
   quote.

End every answer with a short list headed "Values referenced", giving each number
you used with its source and identifier, so a reader can check it.
"""

#: The lookbehind matters. Without it the hyphen in "mp-24972" reads as a minus
#: sign and the identifier is extracted as the number -24972, and the subscripts
#: in a formula like Fe2O3 are extracted as the numbers 2 and 3. Requiring that a
#: sign or leading digit not follow a word character or a decimal point means
#: identifiers, chemical formulae, and space group symbols such as P4_2/mnm are
#: not mistaken for quantitative claims.
NUMBER_PATTERN = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

#: A live model writes negative values with U+2212 MINUS SIGN about as often as
#: with ASCII hyphen-minus, and "-0.943" is the same claim either way. Left
#: alone, the sign is not recognised, the bare magnitude 0.943 is compared
#: against a source value of -0.943, and a number the tools really did emit is
#: reported as invented. Normalising also closes the opposite hole: a claim of
#: U+2212 0.5 can no longer be satisfied by a source value of +0.5, which the
#: old behaviour would have allowed.
UNICODE_MINUS = "\u2212"

#: A markdown ordered-list marker is a structural token, not a quantitative
#: claim. The "4." opening a line enumerates a point; it asserts nothing about a
#: material. Left in, it is extracted as the number 4 and named as an invented
#: value, and false positives of that kind teach a reader to ignore the guard.
#: The pattern is deliberately narrow. Only a short integer followed by a period
#: and then whitespace, at the very start of a line, is removed, so a real
#: quantitative claim cannot be smuggled through by dressing it as a list item.
LIST_MARKER_PATTERN = re.compile(r"^[ \t]{0,3}\d{1,3}\.(?=[ \t])", re.MULTILINE)

#: Spelled-out ratios evade a digit-based guard completely. A live model wrote
#: "more than five times the documented threshold", which is a ratio it computed
#: from two tool values and then rendered in words, so no digit appeared for the
#: guard to check. The boundary forbids computing, not merely forbidding digits,
#: so these forms are caught as claims in their own right. The vocabulary is
#: kept small and requires the multiplicative word, because "two databases" is a
#: count the tools support while "twice the spread" is arithmetic the model did.
_MULTIPLIER_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|"
    "thirty|forty|fifty|hundred|thousand|several|many"
)
WORD_RATIO_PATTERN = re.compile(
    rf"\b(?:(?:{_MULTIPLIER_WORDS})[\s-]+times|twice|thrice|"
    r"(?:two|three|four|five|ten|hundred)[\s-]?fold|"
    r"an?\s+order\s+of\s+magnitude)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# The numeric guard
# ---------------------------------------------------------------------------

def extract_numbers(text: str) -> list[str]:
    return NUMBER_PATTERN.findall((text or "").replace(UNICODE_MINUS, "-"))


def extract_word_ratios(text: str) -> list[str]:
    """Find ratios the model wrote out in words rather than digits."""
    return [m.group(0) for m in WORD_RATIO_PATTERN.finditer(text or "")]


def _as_float(token: str) -> float | None:
    try:
        return float(token)
    except (TypeError, ValueError):
        return None


#: Precisions at which a rounding of a source value is accepted. Zero is
#: excluded deliberately. Rounding 3.2426 to 3 would let a bare integer match
#: almost any value in the tool output, which would gut the guard, and the system
#: prompt tells the model to quote values as given.
_ACCEPTED_ROUNDING_DIGITS = range(1, 7)


def _traceable(claim: str, permitted: Sequence[float]) -> bool:
    """Is a claimed number a value the core emitted, or an honest rounding of one?

    Matching is numeric rather than textual. A substring test against the tool
    output would be far too permissive: the claim "3" appears inside almost any
    JSON payload, so a fabricated number would pass. Numeric matching also
    handles identifiers correctly, because the digits in "mp-149" are extracted
    from the tool output as the number 149 and so remain quotable.
    """
    value = _as_float(claim)
    if value is None:
        return False
    for source_value in permitted:
        if source_value == value:
            return True
        for digits in _ACCEPTED_ROUNDING_DIGITS:
            if round(source_value, digits) == value:
                return True
    return False


@dataclass
class GuardResult:
    passed: bool
    n_claims: int
    unverified: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "n_numeric_claims": self.n_claims,
            "unverified_numbers": list(self.unverified),
            "note": self.note,
        }


def verify_numeric_claims(
    answer: str, tool_outputs: Sequence[str], question: str = ""
) -> GuardResult:
    """Check every number in the answer against the numbers the tools returned."""
    # Numbers the user themselves supplied are legitimate to echo back.
    permitted_text = "\n".join([*tool_outputs, question])
    permitted = [
        v for v in (_as_float(t) for t in extract_numbers(permitted_text)) if v is not None
    ]

    # Stripping list markers applies to the answer only. The tool outputs are
    # JSON, where a line never opens with an enumerator, so doing it there could
    # only ever shrink the permitted set for no benefit.
    claims = extract_numbers(LIST_MARKER_PATTERN.sub("", answer or ""))
    unverified = sorted(
        {c for c in claims if not _traceable(c, permitted)},
        key=lambda c: (_as_float(c) is None, _as_float(c) or 0.0),
    )

    # A ratio in words is a computed quantity with no digit to check, so it can
    # never be traced. It is reported unless the user's own question used it.
    word_ratios = [
        phrase
        for phrase in extract_word_ratios(answer)
        if phrase.lower() not in question.lower()
    ]
    if word_ratios:
        claims = [*claims, *word_ratios]
        unverified = [*unverified, *sorted(set(word_ratios))]

    if not claims:
        return GuardResult(
            passed=True, n_claims=0, note="the answer makes no numeric claims"
        )
    if unverified:
        return GuardResult(
            passed=False,
            n_claims=len(claims),
            unverified=unverified,
            note=(
                "these quantities do not appear in any tool result and could not "
                "be traced to a value the deterministic core emitted. They may "
                "have been computed or invented by the language model, which the "
                "project's hard boundary forbids. A ratio written in words is "
                "listed here for the same reason: it is arithmetic the model did "
                "rather than a value a tool returned. Treat them as unsupported."
            ),
        )
    return GuardResult(
        passed=True,
        n_claims=len(claims),
        note="every numeric claim traces to a value the deterministic core emitted",
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

#: A well studied composition has more polymorphs than a model context can hold.
#: The live eval set proved it: the Si case built a 219,610 token prompt against
#: a 200,000 token limit and died with no answer at all, because the audit for Si
#: spans dozens of structure groups across the two databases.
#:
#: Silently dropping entries would be the worse failure. The model would see a
#: short list and conclude that the missing structures are not in the databases,
#: which is the false-confidence this whole project exists to prevent. So the cap
#: keeps every scalar and every count verbatim, keeps whole entries rather than
#: partial ones, and states inside the payload how many were omitted and how to
#: retrieve them. No value is ever altered, rescaled, or summarised. An entry is
#: present in full or it is declared absent.
TOOL_RESULT_CHAR_BUDGET = 60_000


def bound_tool_result(
    payload: Any, budget: int = TOOL_RESULT_CHAR_BUDGET
) -> str:
    """Serialise a core result, capping its size without distorting any value."""
    text = json.dumps(payload)
    if len(text) <= budget or not isinstance(payload, dict):
        return text

    scalars = {
        k: v for k, v in payload.items() if not (isinstance(v, list) and len(v) > 1)
    }
    lists = {k: v for k, v in payload.items() if isinstance(v, list) and len(v) > 1}

    # Room is shared equally between the list fields rather than packed
    # cleverly. Predictable, explicable behaviour is worth more here than
    # squeezing in a few more entries.
    overhead = len(json.dumps(scalars)) + 800
    share = max(budget - overhead, 0) // max(len(lists), 1)

    trimmed: dict[str, Any] = dict(scalars)
    omitted: dict[str, int] = {}
    for key, items in lists.items():
        kept: list[Any] = []
        used = 0
        for item in items:
            size = len(json.dumps(item)) + 1
            # Always keep one, so a single oversized entry does not empty the
            # field and make the composition look absent from the databases.
            if kept and used + size > share:
                break
            kept.append(item)
            used += size
        trimmed[key] = kept
        if len(kept) < len(items):
            omitted[key] = len(items) - len(kept)

    if omitted:
        trimmed["truncated"] = {
            "reason": (
                "the complete result is larger than a model context window, so "
                "whole entries were omitted to make it fit"
            ),
            "entries_omitted": omitted,
            "warning": (
                "every entry shown above is complete and unaltered, but this is "
                "not the whole result. Do not describe the omitted entries and "
                "do not conclude that they are absent from the databases. Narrow "
                "the request, for example to a specific Materials Project ID, to "
                "see the rest."
            ),
        }
    return json.dumps(trimmed)


def build_tools() -> list[Any]:
    """Wrap the core functions as LangChain tools.

    Each tool returns the core's JSON with no value interpreted, adjusted, or
    recomputed. The only departure from returning it byte for byte is the size
    cap in ``bound_tool_result``, which drops whole entries and says so when a
    result cannot fit in a model context.
    """
    from langchain_core.tools import tool

    @tool
    def audit_material(identifier: str) -> str:
        """Audit one material across Materials Project and OQMD.

        Returns every source value with provenance, the cross-source spread, all
        physics-consistency flags, and a confidence band with its derivation.
        Accepts a composition such as "TiO2" or a Materials Project ID such as
        "mp-149".
        """
        return bound_tool_result(mcp_server.audit_material(identifier))

    @tool
    def compare_sources(formula: str, property_name: str) -> str:
        """Compare one property across sources for a composition.

        property_name must be "formation_energy_per_atom" or "band_gap". Results
        are grouped by structural identity, so same-formula different-structure
        entries appear as separate groups and must not be compared with each other.
        """
        return bound_tool_result(mcp_server.compare_sources(formula, property_name))

    @tool
    def get_provenance(source: str, identifier: str) -> str:
        """Full provenance for one source's values.

        source must be "materials_project" or "oqmd".
        """
        return bound_tool_result(mcp_server.get_provenance(source, identifier))

    @tool
    def explain_hubbard_policy(formula: str) -> str:
        """Documented Hubbard U treatment for a composition in each database.

        Use this to explain a formation energy disagreement. Needs no network call.
        """
        return json.dumps(mcp_server.explain_hubbard_policy(formula))

    @tool
    def check_physics_consistency(record_json: str) -> str:
        """Re-run the physics checks over values you already hold.

        Pass the JSON emitted by audit_material.
        """
        try:
            record = json.loads(record_json)
        except json.JSONDecodeError as exc:
            return json.dumps({"error": "bad_argument", "detail": str(exc)})
        return json.dumps(mcp_server.check_physics_consistency(record))

    return [
        audit_material,
        compare_sources,
        get_provenance,
        explain_hubbard_policy,
        check_physics_consistency,
    ]


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def configure_tracing() -> bool:
    """Enable LangSmith tracing when a key is present, and disable it when not.

    The second half matters. If ``LANGSMITH_TRACING`` is already true in the
    environment but no key is available, every model call emits an authentication
    failure for a trace nobody can read, which buries real errors in noise. So
    tracing is turned off explicitly rather than left to fail repeatedly.
    """
    if os.environ.get("LANGSMITH_API_KEY"):
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        os.environ.setdefault("LANGSMITH_PROJECT", "materials-trust-benchmark")
        return os.environ.get("LANGSMITH_TRACING", "").lower() == "true"
    if os.environ.get("LANGSMITH_TRACING", "").lower() == "true":
        os.environ["LANGSMITH_TRACING"] = "false"
    return False


class MissingModelKey(RuntimeError):
    pass


def build_graph(model_name: str = DEFAULT_MODEL, temperature: float = 0.0):
    """Build the explanation agent.

    ``temperature=0`` because this layer's job is faithful translation of
    structured output, and there is nothing to be gained from sampling variety
    when the requirement is fidelity.
    """
    if not config.anthropic_api_key():
        raise MissingModelKey(
            "ANTHROPIC_API_KEY is not set. Layer 3 needs it; layers 1 and 2 do not."
        )
    configure_tracing()

    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import add_messages
    from langgraph.prebuilt import ToolNode

    # Functional TypedDict syntax, deliberately. This module uses
    # ``from __future__ import annotations``, so a class-statement TypedDict would
    # store its annotations as strings to be resolved later against the module
    # namespace, where the locally imported ``add_messages`` reducer does not
    # exist. The functional form receives the reducer as a real object.
    _State = TypedDict(
        "_State",
        {
            "messages": Annotated[list, add_messages],
            "question": str,
            "guard": "dict[str, Any] | None",
        },
    )

    tools = build_tools()
    model = ChatAnthropic(
        model=model_name, temperature=temperature, max_tokens=4096
    ).bind_tools(tools)

    # Node parameters are annotated as plain dicts on purpose. Annotating them
    # with the locally defined _State would leave a string annotation that
    # LangGraph cannot resolve against the module namespace. The state schema is
    # supplied to StateGraph directly, so nothing is lost.
    def call_model(state: dict) -> dict[str, Any]:
        messages = [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]]
        return {"messages": [model.invoke(messages)]}

    def route(state: dict) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            return "tools"
        return "guard"

    def guard(state: dict) -> dict[str, Any]:
        tool_outputs = [
            str(m.content) for m in state["messages"] if isinstance(m, ToolMessage)
        ]
        final = state["messages"][-1]
        answer = final.content if isinstance(final.content, str) else str(final.content)
        result = verify_numeric_claims(answer, tool_outputs, state.get("question", ""))

        if result.passed:
            return {"guard": result.to_dict()}

        # The brief requires the failure to be visible in the output, not logged
        # and forgotten. The unsupported numbers are named so a reader can check.
        warning = (
            "\n\n---\n"
            "**Numeric guard: FAILED.** The following numbers in the answer above "
            "could not be traced to any value returned by the deterministic core: "
            f"{', '.join(result.unverified)}. "
            "This project forbids the language model from computing or inventing "
            "numerical values, so these should be treated as unsupported and "
            "verified directly against the tool output before being used."
        )
        return {
            "guard": result.to_dict(),
            "messages": [AIMessage(content=answer + warning)],
        }

    graph = StateGraph(_State)
    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode(tools))
    graph.add_node("guard", guard)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, {"tools": "tools", "guard": "guard"})
    graph.add_edge("tools", "agent")
    graph.add_edge("guard", END)
    return graph.compile()


@dataclass
class Explanation:
    question: str
    answer: str
    guard: GuardResult
    tool_calls: list[dict[str, Any]]
    langsmith_enabled: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "guard": self.guard.to_dict(),
            "tool_calls": self.tool_calls,
            "langsmith_tracing": self.langsmith_enabled,
        }


def explain(question: str, model_name: str = DEFAULT_MODEL) -> Explanation:
    """Answer a question about materials data trust, with the guard applied."""
    from langchain_core.messages import AIMessage, HumanMessage

    tracing = configure_tracing()
    app = build_graph(model_name)
    final_state = app.invoke(
        {"messages": [HumanMessage(content=question)], "question": question, "guard": None},
        config={"recursion_limit": 25},
    )

    messages = final_state["messages"]
    answer = ""
    for message in reversed(messages):
        if isinstance(message, AIMessage) and isinstance(message.content, str):
            answer = message.content
            break

    calls: list[dict[str, Any]] = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            calls.append({"name": call.get("name"), "args": call.get("args")})

    guard_payload = final_state.get("guard") or {}
    guard = GuardResult(
        passed=bool(guard_payload.get("passed")),
        n_claims=int(guard_payload.get("n_numeric_claims", 0)),
        unverified=list(guard_payload.get("unverified_numbers") or []),
        note=str(guard_payload.get("note", "")),
    )
    return Explanation(
        question=question,
        answer=answer,
        guard=guard,
        tool_calls=calls,
        langsmith_enabled=tracing,
    )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Ask the explanation agent a question.")
    parser.add_argument("question", help="for example: why do the databases disagree about FeF3")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        result = explain(args.question, args.model)
    except MissingModelKey as exc:
        print(f"error: {exc}")
        return 2

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(result.answer)
        print()
        print(f"tools called: {[c['name'] for c in result.tool_calls]}")
        print(f"numeric guard: {'passed' if result.guard.passed else 'FAILED'}")
        print(f"  {result.guard.note}")
        if result.guard.unverified:
            print(f"  unverified: {result.guard.unverified}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
