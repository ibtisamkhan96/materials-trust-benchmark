"""Command line interface.

Brief section 3.2.1: an installable package with a small CLI that can audit a
single material or run a full benchmark.

    mtb audit TiO2
    mtb audit mp-149 --json
    mtb benchmark --limit 20
    mtb select-set
    mtb unit-check
    mtb recon
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from . import config
from .audit import AuditResult, Auditor
from .checks import Severity
from .records import Property
from .sources.materials_project import MissingAPIKey


def _run_script(name: str, argv: list[str]) -> int:
    """Execute one of the pipeline scripts as if from the command line."""
    path = config.PROJECT_ROOT / "scripts" / f"{name}.py"
    if not path.exists():
        print(f"script not found: {path}", file=sys.stderr)
        return 2
    spec = importlib.util.spec_from_file_location(f"_mtb_script_{name}", path)
    if spec is None or spec.loader is None:
        print(f"could not load {path}", file=sys.stderr)
        return 2
    module = importlib.util.module_from_spec(spec)
    saved = sys.argv
    sys.argv = [str(path), *argv]
    try:
        spec.loader.exec_module(module)
        return int(module.main())
    finally:
        sys.argv = saved


def _render_audit(result: AuditResult) -> str:
    lines: list[str] = []
    lines.append(f"Composition: {result.composition}")
    lines.append("")
    cov = result.coverage
    lines.append(
        "Coverage: "
        + ", ".join(
            f"{src} {n} record(s)" for src, n in sorted(cov.n_records_by_source.items())
        )
        or "Coverage: none"
    )
    lines.append(
        f"Distinct structures across sources: {cov.n_distinct_structures}. "
        f"Structure-matched multi-source comparisons: "
        f"{cov.n_structure_matched_multi_source}."
    )
    if result.failures:
        lines.append("")
        lines.append("Failures:")
        for f in result.failures:
            lines.append(f"  {f.source.value}: {f.reason}")

    by_property: dict[str, list] = {}
    for tr in result.trust_records:
        by_property.setdefault(tr.property_name.value, []).append(tr)

    for prop in sorted(by_property):
        records = sorted(
            by_property[prop],
            key=lambda t: (-len(t.spread.per_source), t.structure_fingerprint or ""),
        )
        units = "eV/atom" if prop == Property.FORMATION_ENERGY_PER_ATOM.value else "eV"
        lines.append("")
        lines.append("=" * 78)
        lines.append(f"{prop} ({units})")
        lines.append("=" * 78)
        for tr in records:
            lines.append("")
            lines.append(f"  Structure: {tr.structure_fingerprint or 'unavailable'}")
            for source, sv in sorted(
                tr.spread.per_source.items(), key=lambda kv: kv[0].value
            ):
                extra = (
                    f" (median of {sv.n} entries, intra-source spread "
                    f"{sv.intra_spread:.4f})"
                    if sv.n > 1
                    else ""
                )
                lines.append(
                    f"    {source.value:18s} {sv.representative:+.4f} {units}{extra}"
                )
            spread = tr.spread.cross_source_spread
            if spread is not None:
                lines.append(f"    cross-source spread: {spread:.4f} {units}")
            lines.append(f"    confidence: {tr.confidence.band.value}")
            for step in tr.confidence.steps:
                lines.append(f"      {step}")
            if tr.flags:
                lines.append("    flags:")
                for flag in tr.flags:
                    marker = "!" if flag.severity is Severity.WARNING else "-"
                    lines.append(f"      {marker} {flag.code.value}: {flag.message}")
    return "\n".join(lines)


def cmd_audit(args: argparse.Namespace) -> int:
    properties = (
        [Property(args.property)]
        if args.property
        else [Property.FORMATION_ENERGY_PER_ATOM, Property.BAND_GAP]
    )
    auditor = Auditor(properties=properties)
    try:
        result = auditor.audit_identifier(args.identifier)
    except MissingAPIKey as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload: dict[str, Any] = result.to_dict()
        print(json.dumps(payload, indent=2))
    else:
        print(_render_audit(result))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mtb",
        description=(
            "Materials Data Trust Benchmark. Audits materials property values "
            "across Materials Project, OQMD, and experiment."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="audit one material by formula or mp-id")
    audit.add_argument("identifier", help="a composition such as TiO2, or an mp-id")
    audit.add_argument(
        "--property",
        choices=[p.value for p in Property],
        help="restrict to one property",
    )
    audit.add_argument("--json", action="store_true", help="emit the structured record")
    audit.set_defaults(func=cmd_audit)

    bench = sub.add_parser("benchmark", help="run the full benchmark")
    bench.add_argument("--limit", type=int, default=None)
    bench.add_argument("--no-cache", action="store_true")
    bench.set_defaults(
        func=lambda a: _run_script(
            "run_benchmark",
            [*(["--limit", str(a.limit)] if a.limit else []), *(["--no-cache"] if a.no_cache else [])],
        )
    )

    sel = sub.add_parser("select-set", help="regenerate the benchmark material set")
    sel.set_defaults(func=lambda a: _run_script("select_material_set", []))

    unit = sub.add_parser("unit-check", help="verify formation energy units")
    unit.set_defaults(func=lambda a: _run_script("unit_harness", []))

    recon = sub.add_parser("recon", help="check live API behaviour")
    recon.set_defaults(func=lambda a: _run_script("api_recon", []))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
