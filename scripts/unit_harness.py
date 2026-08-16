"""Run the unit and sign sanity checks against live source data.

Fails with a non-zero exit code if either source's formation energies are not
demonstrably in eV/atom. The benchmark run calls the same logic, so a unit
regression stops the pipeline rather than propagating into the report.

Run:  python scripts/unit_harness.py
"""

from __future__ import annotations

import json
import sys

from materials_trust import config, unit_checks
from materials_trust.records import Property, Source
from materials_trust.sources.materials_project import MaterialsProjectSource, MissingAPIKey
from materials_trust.sources.oqmd import OQMDSource

TEST_COMPOUNDS = list(unit_checks.LITERATURE_FORMATION_ENTHALPY_KJ_PER_MOL)


def _lowest_energy_value(records) -> float | None:
    """The most stable polymorph's formation energy for this composition.

    The literature enthalpy refers to the thermodynamically stable phase, so the
    comparison uses the lowest computed formation energy rather than an arbitrary
    polymorph.
    """
    vals = [
        r.value for r in records if r.property_name is Property.FORMATION_ENERGY_PER_ATOM
    ]
    return min(vals) if vals else None


def check_oqmd() -> unit_checks.UnitCheckReport:
    src = OQMDSource()
    verdicts = []
    signs: list[tuple[str, float]] = []
    for formula in TEST_COMPOUNDS:
        records = src.records_for(
            formula, properties=[Property.FORMATION_ENERGY_PER_ATOM]
        )
        value = _lowest_energy_value(records)
        if value is None:
            print(f"  OQMD returned no formation energy for {formula}, skipping")
            continue
        verdicts.append(unit_checks.classify_units(formula, value))
        signs.append((formula, value))
    for problem in unit_checks.check_sign_convention(signs):
        print(f"  SIGN PROBLEM: {problem}")
    return unit_checks.UnitCheckReport(verdicts=verdicts, source=Source.OQMD.value)


def check_materials_project() -> unit_checks.UnitCheckReport | None:
    try:
        src = MaterialsProjectSource()
        src._require_key()
    except MissingAPIKey as exc:
        print(f"  skipping Materials Project: {exc}")
        return None

    verdicts = []
    signs: list[tuple[str, float]] = []
    for formula in TEST_COMPOUNDS:
        records = src.records_for(
            formula=formula, properties=[Property.FORMATION_ENERGY_PER_ATOM]
        )
        value = _lowest_energy_value(records)
        if value is None:
            print(f"  Materials Project returned no formation energy for {formula}")
            continue
        verdicts.append(unit_checks.classify_units(formula, value))
        signs.append((formula, value))
    for problem in unit_checks.check_sign_convention(signs):
        print(f"  SIGN PROBLEM: {problem}")
    return unit_checks.UnitCheckReport(
        verdicts=verdicts, source=Source.MATERIALS_PROJECT.value
    )


def main() -> int:
    config.ensure_dirs()
    reports = []

    print("Checking OQMD formation energy units")
    oqmd_report = check_oqmd()
    reports.append(oqmd_report)
    for v in oqmd_report.verdicts:
        mark = "ok" if v.verdict == "eV/atom" else v.verdict
        print(
            f"  {v.formula:8s} reported {v.reported_value:+8.4f}  "
            f"experiment {v.literature_ev_per_atom:+8.4f} eV/atom  -> {mark}"
        )
    print(f"  OQMD unit check passed: {oqmd_report.passed}")

    print("\nChecking Materials Project formation energy units")
    mp_report = check_materials_project()
    if mp_report is not None:
        reports.append(mp_report)
        for v in mp_report.verdicts:
            mark = "ok" if v.verdict == "eV/atom" else v.verdict
            print(
                f"  {v.formula:8s} reported {v.reported_value:+8.4f}  "
                f"experiment {v.literature_ev_per_atom:+8.4f} eV/atom  -> {mark}"
            )
        print(f"  Materials Project unit check passed: {mp_report.passed}")

    out = config.RESULTS_DIR / "unit_check.json"
    out.write_text(
        json.dumps([r.to_dict() for r in reports], indent=2), encoding="utf-8"
    )
    print(f"\nWrote {out}")

    failed = [r for r in reports if not r.passed]
    if failed:
        for r in failed:
            try:
                r.raise_if_failed()
            except unit_checks.UnitCheckFailure as exc:
                print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
