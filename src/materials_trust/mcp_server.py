"""Layer 2: the deterministic core exposed over the Model Context Protocol.

Brief section 3.3: this layer exposes the core as tools so that any MCP capable
client can call them, and each tool returns the core's structured output
unchanged. That last clause is the whole design constraint. Every function here
is a thin adapter: it parses arguments, calls layer 1, and serialises the result.
Nothing in this module computes, adjusts, rounds, or summarises a physical
quantity, because the moment a wrapper starts massaging numbers the guarantee
that the core is the single source of truth is gone.

Run as a stdio server:

    python -m materials_trust.mcp_server

Claude Desktop configuration:

    {
      "mcpServers": {
        "materials-trust": {
          "command": "python",
          "args": ["-m", "materials_trust.mcp_server"],
          "env": { "MP_API_KEY": "..." }
        }
      }
    }
"""

from __future__ import annotations

import json
from typing import Any

from . import checks, hubbard, matching
from .audit import Auditor, assess_confidence
from .records import Property, Source
from .sources.materials_project import MaterialsProjectSource, MissingAPIKey
from .sources.oqmd import OQMDSource

SERVER_NAME = "materials-trust"

SERVER_INSTRUCTIONS = """\
Tools for checking whether a materials property value can be trusted.

Every number these tools return comes from a deterministic pipeline over
Materials Project and OQMD data, with structure matching by pymatgen's
StructureMatcher and explicit provenance on every value.

Two rules matter when using these tools:

1. Do not compute, estimate, adjust, or invent any numerical value. Report only
   numbers that appear in the tool output, and carry them verbatim.
2. If the output does not contain what is needed to answer, say so rather than
   filling the gap. The flags and the confidence derivation explain themselves,
   so quote them.
"""


def _auditor() -> Auditor:
    return Auditor()


# ---------------------------------------------------------------------------
# Tool implementations. Each returns the core's output unchanged.
# ---------------------------------------------------------------------------

def audit_material(identifier: str) -> dict[str, Any]:
    """Full cross-source audit of one material, by composition or Materials Project ID."""
    try:
        result = _auditor().audit_identifier(identifier)
    except MissingAPIKey as exc:
        return {"error": "configuration", "detail": str(exc)}
    except ValueError as exc:
        return {"error": "not_found", "detail": str(exc)}
    payload = result.to_dict()
    payload["summary"] = result.summary_line()
    return payload


def compare_sources(formula: str, property_name: str) -> dict[str, Any]:
    """Compare one property across sources for one composition."""
    try:
        prop = Property(property_name)
    except ValueError:
        return {
            "error": "bad_argument",
            "detail": (
                f"unknown property {property_name!r}; valid values are "
                f"{[p.value for p in Property]}"
            ),
        }
    try:
        result = _auditor().audit(formula)
    except MissingAPIKey as exc:
        return {"error": "configuration", "detail": str(exc)}

    records = [t for t in result.trust_records if t.property_name is prop]
    return {
        "composition": result.composition,
        "property": prop.value,
        "n_structure_matched_groups": len(records),
        "comparisons": [t.to_dict() for t in records],
        "coverage": result.coverage.to_dict(),
        "failures": [f.to_dict() for f in result.failures],
        "note": (
            "values are grouped by structural identity established with "
            f"{matching.MATCHER_DESCRIPTION}. Entries with the same formula but "
            "different structures are reported as separate groups and must not be "
            "compared with each other."
        ),
    }


def check_physics_consistency(record: dict[str, Any]) -> dict[str, Any]:
    """Run the physics checks over a supplied set of source values.

    ``record`` should contain a ``values`` list in the shape emitted by
    ``audit_material``. This tool exists so a caller can re-run the checks on
    values it already holds, and get the same flags the benchmark would produce.
    """
    values = record.get("values") or record.get("records")
    if not isinstance(values, list) or not values:
        return {
            "error": "bad_argument",
            "detail": "expected a 'values' list as emitted by audit_material",
        }

    # Rebuild only what the checks need. Structures are not reconstructed here,
    # so structural identity is taken as already established by the caller and
    # said so in the response.
    from .records import Functional, MagneticState, PropertyRecord, ValueKind

    rebuilt = []
    for v in values:
        try:
            prop = Property(v["property"])
            rebuilt.append(
                PropertyRecord(
                    source=Source(v["source"]),
                    source_id=str(v["source_id"]),
                    formula=v["formula"],
                    property_name=prop,
                    value=float(v["value"]),
                    units=v["units"],
                    functional=Functional(v["functional"]),
                    correction_scheme=v["correction_scheme"],
                    magnetic_state=MagneticState(v["magnetic_state"]),
                    value_kind=ValueKind(v["value_kind"]),
                    structure_is_icsd_derived=v.get("structure_is_icsd_derived"),
                    structure=None,
                    extras=v.get("extras") or {},
                )
            )
        except Exception as exc:
            return {
                "error": "bad_argument",
                "detail": f"could not rebuild a record: {type(exc).__name__}: {exc}",
            }

    props = {r.property_name for r in rebuilt}
    if len(props) != 1:
        return {
            "error": "bad_argument",
            "detail": "all values must be for the same property",
        }

    spread = checks.compute_spread(rebuilt)
    flags = checks.run_all_checks(records=rebuilt, spread=spread)
    confidence = assess_confidence(spread, flags)
    return {
        "spread": spread.to_dict(),
        "flags": [f.to_dict() for f in flags],
        "confidence": confidence.to_dict(),
        "caveat": (
            "structural identity was assumed rather than verified, because no "
            "structures were supplied. Use audit_material for a structure-matched "
            "comparison."
        ),
    }


def get_provenance(source: str, identifier: str) -> dict[str, Any]:
    """Full provenance for every value one source reports for one identifier."""
    try:
        src = Source(source)
    except ValueError:
        return {
            "error": "bad_argument",
            "detail": (
                f"unknown source {source!r}; valid values are "
                f"{[s.value for s in Source if s is not Source.EXPERIMENT]}"
            ),
        }

    try:
        if src is Source.MATERIALS_PROJECT:
            mp = MaterialsProjectSource()
            if identifier.lower().startswith("mp-"):
                records = mp.records_for(material_ids=[identifier])
            else:
                records = mp.records_for(formula=identifier)
            failures = [f.to_dict() for f in mp.failures]
        elif src is Source.OQMD:
            oqmd = OQMDSource()
            records = oqmd.records_for(identifier)
            failures = [f.to_dict() for f in oqmd.failures]
        else:
            return {
                "error": "bad_argument",
                "detail": (
                    "experimental values carry no per-entry provenance beyond the "
                    "dataset citation; use audit_material instead"
                ),
            }
    except MissingAPIKey as exc:
        return {"error": "configuration", "detail": str(exc)}

    return {
        "source": src.value,
        "identifier": identifier,
        "n_records": len(records),
        "records": [r.to_dict() for r in records],
        "failures": failures,
    }


def explain_hubbard_policy(formula: str) -> dict[str, Any]:
    """Documented +U treatment for a composition in each database.

    Included because it is the most common reason two databases disagree about a
    formation energy, and it can be answered from published methodology without
    any network call.
    """
    try:
        comparison = hubbard.compare_hubbard_treatment(formula)
    except Exception as exc:
        return {"error": "bad_argument", "detail": f"{type(exc).__name__}: {exc}"}
    return comparison.to_dict()


TOOLS: dict[str, Any] = {
    "audit_material": audit_material,
    "compare_sources": compare_sources,
    "check_physics_consistency": check_physics_consistency,
    "get_provenance": get_provenance,
    "explain_hubbard_policy": explain_hubbard_policy,
}


# ---------------------------------------------------------------------------
# MCP wiring
# ---------------------------------------------------------------------------

def _server_class():
    """Locate the decorator-style server class across MCP SDK versions.

    The SDK renamed ``FastMCP`` to ``MCPServer`` in version 2.0 while keeping the
    same decorator and run interface, so both are supported rather than pinning
    the project to one SDK generation.
    """
    try:
        from mcp.server import MCPServer

        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP

        return FastMCP


def build_server():
    """Construct the MCP server, registering each core function as a tool."""
    server = _server_class()(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)

    @server.tool()
    def audit_material_tool(identifier: str) -> str:
        """Audit one material across Materials Project and OQMD.

        Returns every source value with full provenance, the spread between
        sources, all physics-consistency flags, and a confidence band with the
        derivation that produced it. Accepts a composition such as "TiO2" or a
        Materials Project ID such as "mp-149".
        """
        return json.dumps(audit_material(identifier), indent=2)

    @server.tool()
    def compare_sources_tool(formula: str, property_name: str) -> str:
        """Compare one property across sources for one composition.

        property_name must be "formation_energy_per_atom" or "band_gap". Values
        are grouped by structural identity, so entries sharing a formula but not
        a structure are returned as separate groups.
        """
        return json.dumps(compare_sources(formula, property_name), indent=2)

    @server.tool()
    def check_physics_consistency_tool(record_json: str) -> str:
        """Re-run the physics checks over a set of values you already hold.

        Pass the JSON emitted by audit_material, or any object with a "values"
        list in that shape.
        """
        try:
            record = json.loads(record_json)
        except json.JSONDecodeError as exc:
            return json.dumps({"error": "bad_argument", "detail": str(exc)})
        return json.dumps(check_physics_consistency(record), indent=2)

    @server.tool()
    def get_provenance_tool(source: str, identifier: str) -> str:
        """Full provenance for one source's values.

        source must be "materials_project" or "oqmd". Returns functional,
        correction scheme, magnetic state, structure fingerprint, experimental
        observation status, and the retrieval timestamp for every value.
        """
        return json.dumps(get_provenance(source, identifier), indent=2)

    @server.tool()
    def explain_hubbard_policy_tool(formula: str) -> str:
        """Documented Hubbard U treatment for a composition in each database.

        Answers from published methodology with no network call. Useful for
        explaining a formation energy disagreement.
        """
        return json.dumps(explain_hubbard_policy(formula), indent=2)

    return server


def main() -> int:
    build_server().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
