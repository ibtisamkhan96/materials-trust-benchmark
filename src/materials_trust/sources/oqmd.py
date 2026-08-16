"""OQMD adapter, native ``oqmdapi`` REST endpoint.

Endpoint behaviour verified against the live service, recorded in
``docs/api-reality.md``. Three things matter and are easy to get wrong:

1. ``composition`` is a top level query parameter, not a ``filter`` keyword.
   Passing ``filter=composition=NaCl`` returns HTTP 400. The documentation lists
   ``composition`` under primary query fields, and ``filter`` accepts only
   ``element_set``, ``element``, ``generic``, ``prototype``, ``spacegroup``,
   ``natoms``, ``volume``, ``ntypes``, ``stability``, ``delta_e``, ``band_gap``.

2. ``delta_e`` is formation energy in **eV/atom**. Verified numerically: NaCl
   returns -2.050 eV/atom against an experimental -2.13 eV/atom, and rutile
   TiO2 returns -3.216 eV/atom against an experimental -3.26 eV/atom. Both are
   consistent with per atom and inconsistent with per formula unit by factors of
   2 and 3 respectively. ``scripts/unit_harness.py`` re-checks this on every
   benchmark run rather than trusting this comment.

3. The API exposes no exchange-correlation functional field and no magnetic
   ordering field. Confirmed by requesting a record with no ``fields`` filter and
   inspecting every key returned. Both are therefore derived from OQMD's
   published methodology in ``hubbard.py`` and labelled as derived, never
   presented as per-entry metadata.

The OPTIMADE endpoint at ``/optimade/structures`` was also tested. It returned
HTTP 400 for the documented v0.9.5 filter syntax and timed out after 90 seconds
for v1.2 syntax, so it is not used. The native endpoint returns ``unit_cell`` and
``sites``, which is everything needed to rebuild a structure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

import requests
from pymatgen.core import Composition, Lattice, Structure

from .. import config, hubbard
from ..cache import DiskCache, make_key
from ..records import (
    Functional,
    MagneticState,
    Property,
    PropertyRecord,
    Source,
    SourceFailure,
    ValueKind,
)

#: Every field the endpoint offers that this project has any use for. Requested
#: explicitly so that a silent schema change shows up as a missing key rather
#: than as a quietly absent property.
FIELDS = (
    "name,entry_id,formationenergy_id,calculation_id,duplicate_entry_id,"
    "composition,composition_generic,prototype,spacegroup,volume,ntypes,natoms,"
    "unit_cell,sites,band_gap,delta_e,stability,fit,calculation_label,icsd_id"
)

PAGE_LIMIT = 100

FORMATION_ENERGY_CORRECTION = (
    "OQMD fitted elemental chemical potential corrections "
    "(Kirklin et al. 2015). Separate fitted corrections are applied to "
    "elements treated with GGA+U."
)


class OQMDUnavailable(RuntimeError):
    """OQMD could not be reached or returned an unusable response."""


@dataclass
class OQMDSource:
    """Retrieval of OQMD entries for a composition.

    Failures are returned rather than raised into the caller's face, because
    brief rule 5.3 requires partial results and coverage to be recorded
    explicitly rather than aborting a benchmark run.
    """

    cache_enabled: bool = True
    timeout: float = config.OQMD_TIMEOUT_SECONDS
    max_retries: int = config.OQMD_MAX_RETRIES

    def __post_init__(self) -> None:
        self.cache = DiskCache(namespace="oqmd", enabled=self.cache_enabled)
        self.session = requests.Session()
        self.failures: list[SourceFailure] = []

    # -- HTTP ---------------------------------------------------------------

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{config.OQMD_BASE_URL}/oqmdapi/formationenergy"
        last: str = "no attempt made"
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code >= 500:
                    last = f"HTTP {resp.status_code}"
                    raise OQMDUnavailable(last)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retries:
                    time.sleep(config.OQMD_BACKOFF_SECONDS * attempt)
        raise OQMDUnavailable(last)

    def fetch_composition(self, formula: str) -> list[dict[str, Any]]:
        """All OQMD entries for a composition, paginated, cached verbatim.

        Raises ``OQMDUnavailable`` if the service cannot be reached. Callers that
        want the failure recorded instead should use :meth:`records_for`.
        """
        reduced = Composition(formula).reduced_formula
        key = make_key(endpoint="formationenergy", composition=reduced, fields=FIELDS)
        cached = self.cache.get("composition", key)
        if cached is not None:
            return cached

        collected: list[dict[str, Any]] = []
        offset = 0
        while True:
            params = {
                "composition": reduced,
                "fields": FIELDS,
                "limit": PAGE_LIMIT,
                "offset": offset,
                "noduplicate": "True",
                "format": "json",
            }
            body = self._get(params)
            page = body.get("data") or []
            collected.extend(page)
            meta = body.get("meta") or {}
            if not meta.get("more_data_available") or not page:
                break
            offset += PAGE_LIMIT
            if offset > 2000:  # guard against a pagination loop
                break

        self.cache.put(
            "composition", key, collected, query={"composition": reduced, "fields": FIELDS}
        )
        return collected

    # -- parsing ------------------------------------------------------------

    @staticmethod
    def build_structure(entry: dict[str, Any]) -> Structure | None:
        """Rebuild a pymatgen Structure from ``unit_cell`` and ``sites``.

        OQMD returns the lattice as a 3x3 matrix in Angstrom and sites as
        strings of the form ``"Cl @ 0.5 0 0.5"``, where the coordinates are
        fractional. Returning None is the correct outcome for anything that does
        not parse cleanly: a guessed structure would be far worse than no
        structure, because it would silently drive structure matching.
        """
        cell = entry.get("unit_cell")
        sites = entry.get("sites")
        if not cell or not sites:
            return None
        try:
            lattice = Lattice(cell)
            species: list[str] = []
            coords: list[list[float]] = []
            for raw in sites:
                if "@" not in raw:
                    return None
                left, right = raw.split("@", 1)
                symbol = left.strip()
                parts = right.split()
                if len(parts) != 3:
                    return None
                coords.append([float(p) for p in parts])
                species.append(symbol)
            structure = Structure(lattice, species, coords, coords_are_cartesian=False)
        except Exception:
            return None
        if len(structure) == 0:
            return None
        return structure

    def _records_from_entry(
        self, entry: dict[str, Any], properties: Iterable[Property]
    ) -> list[PropertyRecord]:
        entry_id = entry.get("entry_id")
        name = entry.get("name")
        if entry_id is None or not name:
            return []

        composition = Composition(name)
        structure = self.build_structure(entry)

        expected_u = hubbard.oqmd_expected_u(composition)
        functional = Functional.PBE_PLUS_U if expected_u else Functional.PBE
        spin_polarised = hubbard.oqmd_expected_spin_polarised(composition)
        magnetic_state = (
            MagneticState.FERROMAGNETIC if spin_polarised else MagneticState.NON_MAGNETIC
        )

        icsd_id = entry.get("icsd_id")
        is_icsd = bool(icsd_id) and str(icsd_id) not in {"0", "None"}

        common_extras: dict[str, Any] = {
            "stability_ev_per_atom": entry.get("stability"),
            "prototype": entry.get("prototype"),
            "spacegroup_reported": entry.get("spacegroup"),
            "natoms": entry.get("natoms"),
            "ntypes": entry.get("ntypes"),
            "volume": entry.get("volume"),
            "fit": entry.get("fit"),
            "calculation_label": entry.get("calculation_label"),
            "icsd_id": icsd_id,
            "duplicate_entry_id": entry.get("duplicate_entry_id"),
            # Provenance of the provenance. These two fields are inferred from
            # published methodology because the API exposes neither, and the
            # distinction is reported rather than hidden.
            "functional_determined_by": "documented methodology, not per-entry metadata",
            "functional_reference": hubbard.OQMD_U_REFERENCE,
            "hubbard_u_minus_j_applied": expected_u or None,
            "magnetic_state_determined_by": (
                "documented methodology, not per-entry metadata"
            ),
            "magnetic_reference": hubbard.OQMD_MAGNETIC_REFERENCE,
            "structure_reconstructed": structure is not None,
        }

        url = f"{config.OQMD_BASE_URL}/materials/entry/{entry_id}"
        out: list[PropertyRecord] = []

        for prop in properties:
            if prop is Property.FORMATION_ENERGY_PER_ATOM:
                raw = entry.get("delta_e")
                if raw is None:
                    continue
                out.append(
                    PropertyRecord(
                        source=Source.OQMD,
                        source_id=str(entry_id),
                        formula=name,
                        property_name=Property.FORMATION_ENERGY_PER_ATOM,
                        value=float(raw),
                        units="eV/atom",
                        functional=functional,
                        correction_scheme=FORMATION_ENERGY_CORRECTION,
                        magnetic_state=magnetic_state,
                        value_kind=ValueKind.COMPUTED,
                        structure_is_icsd_derived=is_icsd,
                        structure=structure,
                        source_url=url,
                        extras=dict(common_extras),
                    )
                )
            elif prop is Property.BAND_GAP:
                raw = entry.get("band_gap")
                if raw is None:
                    continue
                out.append(
                    PropertyRecord(
                        source=Source.OQMD,
                        source_id=str(entry_id),
                        formula=name,
                        property_name=Property.BAND_GAP,
                        value=float(raw),
                        units="eV",
                        functional=functional,
                        # Band gaps are not corrected by either database. Saying
                        # "none" explicitly is required by the record type.
                        correction_scheme="none",
                        magnetic_state=magnetic_state,
                        value_kind=ValueKind.COMPUTED,
                        structure_is_icsd_derived=is_icsd,
                        structure=structure,
                        source_url=url,
                        extras=dict(common_extras),
                    )
                )
        return out

    # -- public -------------------------------------------------------------

    def records_for(
        self,
        formula: str,
        properties: Iterable[Property] = (
            Property.FORMATION_ENERGY_PER_ATOM,
            Property.BAND_GAP,
        ),
    ) -> list[PropertyRecord]:
        """Every OQMD record for a composition. Failures are recorded, not raised."""
        properties = list(properties)
        try:
            entries = self.fetch_composition(formula)
        except OQMDUnavailable as exc:
            self.failures.append(
                SourceFailure(
                    source=Source.OQMD,
                    query=f"composition={formula}",
                    reason=str(exc),
                )
            )
            return []

        records: list[PropertyRecord] = []
        for entry in entries:
            try:
                records.extend(self._records_from_entry(entry, properties))
            except Exception as exc:
                # A single malformed entry must not take down a benchmark run,
                # but it must be visible in the coverage statistics.
                self.failures.append(
                    SourceFailure(
                        source=Source.OQMD,
                        query=f"composition={formula} entry_id={entry.get('entry_id')}",
                        reason=f"record construction failed: {type(exc).__name__}: {exc}",
                    )
                )
        return records
