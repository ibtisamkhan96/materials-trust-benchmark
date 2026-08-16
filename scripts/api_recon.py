"""API reconnaissance.

Brief section 7 says to verify current OQMD endpoint behaviour before relying on
it, and rule 7 says to verify before claiming. This script establishes what the
three data sources actually return today, then writes the findings to
``docs/api-reality.md``.

It answers the questions that determine whether the physics rules are
implementable at all:

1. Does the Materials Project key work, and can the per-entry functional be
   resolved through origins to task_id to run_type?
2. Is OQMD reachable, and does it return atomic sites and a unit cell, without
   which structure matching is impossible and every comparison would degrade to
   polymorph-ambiguous?
3. Does OQMD expose functional or magnetic state metadata anywhere?
4. Does the curated experimental band gap dataset load, and does it carry
   Materials Project IDs?

Run:  python scripts/api_recon.py
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests

from materials_trust import config

TIMEOUT = 90


def _timed(fn, *args, **kwargs) -> tuple[Any, float, str | None]:
    start = time.perf_counter()
    try:
        result = fn(*args, **kwargs)
        return result, time.perf_counter() - start, None
    except Exception as exc:
        return None, time.perf_counter() - start, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Materials Project
# ---------------------------------------------------------------------------

def probe_materials_project() -> dict[str, Any]:
    findings: dict[str, Any] = {"source": "Materials Project", "checks": {}}
    key = config.mp_api_key()
    if not key:
        findings["status"] = "SKIPPED"
        findings["reason"] = (
            "MP_API_KEY is not set. Copy .env.example to .env and add the key."
        )
        return findings

    try:
        from mp_api.client import MPRester
    except Exception as exc:
        findings["status"] = "ERROR"
        findings["reason"] = f"mp-api import failed: {exc}"
        return findings

    try:
        with MPRester(key) as mpr:
            findings["checks"]["database_version"] = mpr.get_database_version()

            fields = [
                "material_id",
                "formula_pretty",
                "structure",
                "band_gap",
                "formation_energy_per_atom",
                "energy_above_hull",
                "theoretical",
                "ordering",
                "total_magnetization",
                "database_IDs",
                "deprecated",
                "symmetry",
                "origins",
            ]
            docs, elapsed, err = _timed(
                mpr.materials.summary.search,
                material_ids=["mp-149", "mp-2657", "mp-390"],
                fields=fields,
            )
            findings["checks"]["summary_search"] = {
                "requested_fields": fields,
                "seconds": round(elapsed, 2),
                "error": err,
                "returned": len(docs) if docs else 0,
            }
            if docs:
                sample = {}
                for d in docs:
                    sample[str(d.material_id)] = {
                        "formula": d.formula_pretty,
                        "band_gap": d.band_gap,
                        "summary_formation_energy_per_atom": d.formation_energy_per_atom,
                        "theoretical": d.theoretical,
                        "ordering": str(d.ordering),
                        "total_magnetization": d.total_magnetization,
                        "icsd_ids_present": bool(
                            (d.database_IDs or {}).get("icsd")
                        ),
                        "deprecated": d.deprecated,
                        "nsites": len(d.structure) if d.structure else None,
                        "spacegroup": (
                            d.symmetry.symbol if d.symmetry is not None else None
                        ),
                        "origins_props": [o.name for o in (d.origins or [])],
                    }
                findings["checks"]["summary_sample"] = sample

            # Formation energy pinned to the GGA/GGA+U mixing scheme, which is
            # the only thermo type comparable with OQMD. The summary endpoint's
            # value comes from the default thermo type, which in current
            # releases is GGA_GGA+U_R2SCAN.
            thermo_docs, elapsed, err = _timed(
                mpr.materials.thermo.search,
                material_ids=["mp-149", "mp-2657", "mp-390"],
                thermo_types=["GGA_GGA+U"],
                fields=["material_id", "thermo_type", "formation_energy_per_atom", "entries"],
            )
            findings["checks"]["thermo_gga_only"] = {
                "seconds": round(elapsed, 2),
                "error": err,
                "returned": len(thermo_docs) if thermo_docs else 0,
            }
            if thermo_docs:
                per_id = {}
                for t in thermo_docs:
                    run_types = []
                    try:
                        for entry in (t.entries or {}).values():
                            rt = (entry.parameters or {}).get("run_type")
                            if rt:
                                run_types.append(str(rt))
                    except Exception as exc:
                        run_types = [f"entry parse failed: {exc}"]
                    per_id[str(t.material_id)] = {
                        "thermo_type": str(t.thermo_type),
                        "formation_energy_per_atom": t.formation_energy_per_atom,
                        "run_types_in_entries": sorted(set(run_types)),
                    }
                findings["checks"]["thermo_sample"] = per_id

            # Does the same material report a different formation energy under
            # the R2SCAN-mixed scheme? If so, using the summary value against
            # OQMD would violate the like-with-like rule.
            mixed_docs, _, err = _timed(
                mpr.materials.thermo.search,
                material_ids=["mp-149", "mp-2657", "mp-390"],
                thermo_types=["GGA_GGA+U_R2SCAN"],
                fields=["material_id", "thermo_type", "formation_energy_per_atom"],
            )
            if mixed_docs:
                findings["checks"]["thermo_r2scan_mixed_sample"] = {
                    str(t.material_id): t.formation_energy_per_atom for t in mixed_docs
                }
            elif err:
                findings["checks"]["thermo_r2scan_mixed_error"] = err

        findings["status"] = "OK"
    except Exception:
        findings["status"] = "ERROR"
        findings["reason"] = traceback.format_exc(limit=4)
    return findings


# ---------------------------------------------------------------------------
# OQMD, native oqmdapi
# ---------------------------------------------------------------------------

OQMD_NATIVE_FIELDS = (
    "name,entry_id,formationenergy_id,spacegroup,ntypes,natoms,volume,"
    "delta_e,band_gap,stability,icsd_id,prototype,calculation_label,fit,"
    "unit_cell,sites,duplicate_entry_id"
)


def probe_oqmd_native(base_url: str) -> dict[str, Any]:
    findings: dict[str, Any] = {"source": f"OQMD native oqmdapi ({base_url})", "checks": {}}
    url = f"{base_url}/oqmdapi/formationenergy"
    # composition is a top level query parameter. Passing it inside filter
    # returns HTTP 400, because filter accepts only element_set, element,
    # generic, prototype, spacegroup, natoms, volume, ntypes, stability,
    # delta_e, and band_gap.
    params = {
        "composition": "NaCl",
        "fields": OQMD_NATIVE_FIELDS,
        "limit": 3,
        "format": "json",
    }
    start = time.perf_counter()
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
        elapsed = time.perf_counter() - start
        findings["checks"]["http_status"] = resp.status_code
        findings["checks"]["seconds"] = round(elapsed, 2)
        findings["checks"]["request_url"] = resp.url
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data") or []
        findings["checks"]["api_version"] = (body.get("meta") or {}).get("api_version")
        findings["checks"]["data_available"] = (body.get("meta") or {}).get("data_available")
        findings["checks"]["returned"] = len(data)
        if data:
            first = data[0]
            findings["checks"]["keys_returned"] = sorted(first.keys())
            findings["checks"]["sites_type"] = type(first.get("sites")).__name__
            findings["checks"]["sites_sample"] = (first.get("sites") or [])[:4]
            findings["checks"]["unit_cell_sample"] = first.get("unit_cell")
            findings["checks"]["structure_reconstructable"] = bool(
                first.get("sites") and first.get("unit_cell")
            )
            findings["checks"]["sample_entry"] = {
                k: first.get(k)
                for k in (
                    "name",
                    "entry_id",
                    "delta_e",
                    "band_gap",
                    "stability",
                    "natoms",
                    "spacegroup",
                    "icsd_id",
                    "calculation_label",
                    "fit",
                )
            }
            findings["checks"]["exposes_functional_field"] = any(
                k in first for k in ("functional", "xc", "exchange_correlation", "potential")
            )
            findings["checks"]["exposes_magnetic_field"] = any(
                "magmom" in k or "magnet" in k or "spin" in k for k in first
            )
        findings["status"] = "OK"
    except Exception as exc:
        findings["status"] = "ERROR"
        findings["checks"]["seconds"] = round(time.perf_counter() - start, 2)
        findings["reason"] = f"{type(exc).__name__}: {exc}"
    return findings


def probe_oqmd_optimade(base_url: str) -> dict[str, Any]:
    findings: dict[str, Any] = {"source": f"OQMD OPTIMADE ({base_url})", "checks": {}}
    # Recorded for completeness. This endpoint is not used by the project: it
    # returned HTTP 400 for the v0.9.5 syntax the OQMD docs show and timed out
    # after 90 seconds for v1.2 syntax. The native endpoint supplies structures
    # via unit_cell and sites, so OPTIMADE is not needed.
    url = f"{base_url}/optimade/structures"
    params = {"filter": 'elements HAS ALL "Na","Cl"', "page_limit": 2}
    start = time.perf_counter()
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
        elapsed = time.perf_counter() - start
        findings["checks"]["http_status"] = resp.status_code
        findings["checks"]["seconds"] = round(elapsed, 2)
        findings["checks"]["request_url"] = resp.url
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data") or []
        findings["checks"]["returned"] = len(data)
        if data:
            attrs = data[0].get("attributes", data[0])
            findings["checks"]["attribute_keys"] = sorted(attrs.keys())
            findings["checks"]["has_lattice_vectors"] = "lattice_vectors" in attrs
            findings["checks"]["has_site_positions"] = any(
                k in attrs for k in ("cartesian_site_positions", "fractional_site_positions")
            )
            findings["checks"]["oqmd_prefixed_keys"] = sorted(
                k for k in attrs if k.startswith("_oqmd")
            )
        findings["status"] = "OK"
    except Exception as exc:
        findings["status"] = "ERROR"
        findings["checks"]["seconds"] = round(time.perf_counter() - start, 2)
        findings["reason"] = f"{type(exc).__name__}: {exc}"
    return findings


# ---------------------------------------------------------------------------
# Experimental dataset
# ---------------------------------------------------------------------------

def probe_matminer() -> dict[str, Any]:
    findings: dict[str, Any] = {"source": "matminer experimental datasets", "checks": {}}
    try:
        from matminer.datasets import load_dataset
    except Exception as exc:
        findings["status"] = "ERROR"
        findings["reason"] = f"matminer import failed: {exc}"
        return findings

    for name in ("expt_gap_kingsbury", "expt_gap"):
        df, elapsed, err = _timed(load_dataset, name)
        entry: dict[str, Any] = {"seconds": round(elapsed, 1), "error": err}
        if df is not None:
            entry["rows"] = int(len(df))
            entry["columns"] = list(df.columns)
            entry["head"] = df.head(3).to_dict(orient="records")
            if "likely_mpid" in df.columns:
                entry["rows_with_mpid"] = int(df["likely_mpid"].notna().sum())
            gap_col = "expt_gap" if "expt_gap" in df.columns else "gap expt"
            if gap_col in df.columns:
                entry["gap_min"] = float(df[gap_col].min())
                entry["gap_max"] = float(df[gap_col].max())
                entry["gap_zero_count"] = int((df[gap_col] == 0).sum())
        findings["checks"][name] = entry

    findings["status"] = "OK" if any(
        c.get("rows") for c in findings["checks"].values()
    ) else "ERROR"
    return findings


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def render_markdown(results: list[dict[str, Any]]) -> str:
    lines = [
        "# API reality check",
        "",
        "Generated by `scripts/api_recon.py`. This file records what the data",
        "sources actually returned, not what their documentation claims. Rerun it",
        "if a source changes behaviour.",
        "",
        f"Run at: {time.strftime('%Y-%m-%d %H:%M:%S')} local time.",
        "",
    ]
    for r in results:
        lines.append(f"## {r['source']}")
        lines.append("")
        lines.append(f"Status: **{r['status']}**")
        if r.get("reason"):
            lines.append("")
            lines.append("```")
            lines.append(str(r["reason"]).strip())
            lines.append("```")
        if r.get("checks"):
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(r["checks"], indent=2, default=str))
            lines.append("```")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    config.ensure_dirs()
    results = [
        probe_materials_project(),
        probe_oqmd_native(config.OQMD_BASE_URL),
        probe_oqmd_optimade(config.OQMD_BASE_URL),
        probe_matminer(),
    ]

    # If https failed for OQMD, try http, which is what the docs show.
    if results[1]["status"] == "ERROR" and config.OQMD_BASE_URL.startswith("https"):
        alt = config.OQMD_BASE_URL.replace("https://", "http://")
        results.append(probe_oqmd_native(alt))

    for r in results:
        print(f"[{r['status']:8s}] {r['source']}")
        if r.get("reason"):
            print(f"           {str(r['reason']).strip().splitlines()[-1]}")
        for k, v in (r.get("checks") or {}).items():
            rendered = json.dumps(v, default=str)
            if len(rendered) > 400:
                rendered = rendered[:400] + " ...(truncated, see docs/api-reality.md)"
            print(f"           {k}: {rendered}")
        print()

    out = config.DOCS_DIR / "api-reality.md"
    out.write_text(render_markdown(results), encoding="utf-8")
    print(f"Wrote {out}")
    return 0 if all(r["status"] != "ERROR" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
