"""Materials Project adapter.

The important design decision in this module concerns which formation energy to
use, and it is a physics decision, not a plumbing one.

The ``summary`` endpoint exposes ``formation_energy_per_atom``, and it is
tempting to use it. It should not be used for cross-source comparison. In
current Materials Project releases the summary value derives from the default
thermo type, which is the ``GGA_GGA+U_R2SCAN`` mixing scheme. OQMD contains no
r2SCAN calculations at all. Comparing a partly r2SCAN-referenced Materials
Project energy against a purely GGA(+U) OQMD energy would break brief rule 2.3,
compare like with unlike, and manufacture a disagreement that is an artefact of
the mixing scheme rather than a property of either calculation.

So formation energy is taken from the ``thermo`` endpoint pinned to
``thermo_types=["GGA_GGA+U"]``, which is the only thermo type comparable with
OQMD. The summary value is still retrieved and stored in ``extras`` under
``summary_formation_energy_per_atom`` so the report can quantify how much the
choice matters.

The per-entry functional is resolved from the thermo entries' ``run_type``,
which is the authoritative answer for Materials Project. It is cross-checked
against the documented +U policy in ``hubbard.py``, and any disagreement between
the two is recorded rather than silently resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from pymatgen.core import Composition, Structure

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

SUMMARY_FIELDS = [
    "material_id",
    "formula_pretty",
    "structure",
    "band_gap",
    "is_gap_direct",
    "formation_energy_per_atom",
    "energy_above_hull",
    "theoretical",
    "ordering",
    "total_magnetization",
    "database_IDs",
    "deprecated",
    "symmetry",
    "origins",
    "nsites",
]

THERMO_FIELDS = [
    "material_id",
    "thermo_type",
    "formation_energy_per_atom",
    "energy_above_hull",
    "entries",
]

#: The only thermo type comparable with OQMD, which has no r2SCAN data.
THERMO_TYPE = "GGA_GGA+U"

FORMATION_ENERGY_CORRECTION = (
    "MP2020Compatibility: anion corrections plus GGA/GGA+U mixing corrections, "
    "as applied within the GGA_GGA+U thermo type"
)

_ORDERING_MAP = {
    "FM": MagneticState.FERROMAGNETIC,
    "AFM": MagneticState.ANTIFERROMAGNETIC,
    "FiM": MagneticState.FERRIMAGNETIC,
    "NM": MagneticState.NON_MAGNETIC,
    "Ferromagnetic": MagneticState.FERROMAGNETIC,
    "Antiferromagnetic": MagneticState.ANTIFERROMAGNETIC,
    "Ferrimagnetic": MagneticState.FERRIMAGNETIC,
    "Non-magnetic": MagneticState.NON_MAGNETIC,
    "NonMagnetic": MagneticState.NON_MAGNETIC,
}

_RUN_TYPE_MAP = {
    "GGA": Functional.PBE,
    "GGA_U": Functional.PBE_PLUS_U,
    "GGA+U": Functional.PBE_PLUS_U,
    "R2SCAN": Functional.R2SCAN,
    "r2SCAN": Functional.R2SCAN,
}


class MissingAPIKey(RuntimeError):
    pass


@dataclass
class MaterialsProjectSource:
    api_key: str | None = None
    cache_enabled: bool = True

    def __post_init__(self) -> None:
        self.api_key = self.api_key or config.mp_api_key()
        self.cache = DiskCache(namespace="materials_project", enabled=self.cache_enabled)
        self.failures: list[SourceFailure] = []

    def _require_key(self) -> str:
        if not self.api_key:
            raise MissingAPIKey(
                "MP_API_KEY is not set. Copy .env.example to .env and add a free "
                "key from https://next-gen.materialsproject.org/api"
            )
        return self.api_key

    def _rester(self):
        from mp_api.client import MPRester

        # use_document_model=False returns plain dictionaries, which cache to
        # disk verbatim without a pydantic round trip.
        return MPRester(self._require_key(), use_document_model=False)

    # -- retrieval ----------------------------------------------------------

    def summary(
        self,
        formula: str | None = None,
        material_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not formula and not material_ids:
            raise ValueError("provide either formula or material_ids")
        query: dict[str, Any] = {"fields": SUMMARY_FIELDS}
        if formula:
            query["formula"] = Composition(formula).reduced_formula
        if material_ids:
            query["material_ids"] = sorted(material_ids)

        key = make_key(endpoint="summary", **query)
        cached = self.cache.get("summary", key)
        if cached is not None:
            return cached

        with self._rester() as mpr:
            docs = mpr.materials.summary.search(**query)
        docs = [dict(d) for d in docs]
        self.cache.put("summary", key, docs, query=query)
        return docs

    def thermo(self, material_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Thermo documents pinned to the GGA/GGA+U mixing scheme."""
        ids = sorted(set(material_ids))
        if not ids:
            return {}
        key = make_key(endpoint="thermo", thermo_type=THERMO_TYPE, material_ids=ids)
        cached = self.cache.get("thermo", key)
        if cached is None:
            with self._rester() as mpr:
                docs = mpr.materials.thermo.search(
                    material_ids=ids,
                    thermo_types=[THERMO_TYPE],
                    fields=THERMO_FIELDS,
                )
            cached = [self._slim_thermo(dict(d)) for d in docs]
            self.cache.put(
                "thermo", key, cached, query={"material_ids": ids, "thermo_type": THERMO_TYPE}
            )
        return {d["material_id"]: d for d in cached if d.get("material_id")}

    @staticmethod
    def _slim_thermo(doc: dict[str, Any]) -> dict[str, Any]:
        """Keep the thermo fields we use, discarding megabytes of entry payload.

        The run types are extracted here rather than at read time so that the
        cached artefact records exactly what was used to decide the functional.
        """
        run_types: list[str] = []
        entries = doc.get("entries") or {}
        if isinstance(entries, dict):
            for label, entry in entries.items():
                params = (entry or {}).get("parameters") or {} if isinstance(entry, dict) else {}
                rt = params.get("run_type") or label
                if rt:
                    run_types.append(str(rt))
        return {
            "material_id": str(doc.get("material_id")) if doc.get("material_id") else None,
            "thermo_type": str(doc.get("thermo_type")),
            "formation_energy_per_atom": doc.get("formation_energy_per_atom"),
            "energy_above_hull": doc.get("energy_above_hull"),
            "run_types": sorted(set(run_types)),
        }

    # -- interpretation -----------------------------------------------------

    @staticmethod
    def _magnetic_state(doc: dict[str, Any]) -> MagneticState:
        ordering = doc.get("ordering")
        if ordering is None:
            return MagneticState.UNKNOWN
        return _ORDERING_MAP.get(str(ordering), MagneticState.UNKNOWN)

    @staticmethod
    def _functional(
        run_types: Sequence[str], composition: Composition
    ) -> tuple[Functional, str, dict[str, Any]]:
        """Resolve the functional, preferring the API over the documented policy."""
        predicted_u = hubbard.mp_expected_u(composition)
        predicted = Functional.PBE_PLUS_U if predicted_u else Functional.PBE

        resolved: Functional | None = None
        mapped = [_RUN_TYPE_MAP.get(rt) for rt in run_types]
        mapped = [m for m in mapped if m is not None]
        if len(set(mapped)) == 1:
            resolved = mapped[0]
        elif len(set(mapped)) > 1:
            # More than one run type contributed. Under the GGA_GGA+U thermo
            # type this means the mixing scheme combined GGA and GGA+U
            # calculations, and no single functional describes the number.
            resolved = Functional.PBE_OR_PBE_PLUS_U_UNRESOLVED

        extras: dict[str, Any] = {
            "run_types_reported": list(run_types),
            "hubbard_u_predicted": predicted_u or None,
            "hubbard_u_reference": hubbard.MP_U_REFERENCE,
        }
        if resolved is None:
            extras["functional_determined_by"] = (
                "documented methodology, API run_type unavailable"
            )
            return predicted, "methodology", extras

        extras["functional_determined_by"] = "API run_type"
        if resolved in (Functional.PBE, Functional.PBE_PLUS_U) and resolved is not predicted:
            # Worth surfacing: either the policy has changed or this material is
            # an exception to it. Recorded, never silently overridden.
            extras["functional_policy_disagreement"] = {
                "api": resolved.value,
                "documented_policy": predicted.value,
            }
        return resolved, "api", extras

    def _records_from_doc(
        self,
        doc: dict[str, Any],
        thermo: dict[str, Any] | None,
        properties: Iterable[Property],
    ) -> list[PropertyRecord]:
        mpid = str(doc.get("material_id") or "")
        formula = doc.get("formula_pretty")
        if not mpid or not formula:
            return []
        if doc.get("deprecated"):
            return []

        composition = Composition(formula)
        structure = None
        raw_structure = doc.get("structure")
        if raw_structure is not None:
            try:
                structure = (
                    raw_structure
                    if isinstance(raw_structure, Structure)
                    else Structure.from_dict(raw_structure)
                )
            except Exception:
                structure = None

        run_types = (thermo or {}).get("run_types") or []
        functional, _how, functional_extras = self._functional(run_types, composition)

        theoretical = doc.get("theoretical")
        icsd_ids = (doc.get("database_IDs") or {}).get("icsd") or []
        structure_is_icsd = (
            (not theoretical) if theoretical is not None else (bool(icsd_ids) or None)
        )

        symmetry = doc.get("symmetry") or {}
        common_extras: dict[str, Any] = {
            "energy_above_hull": doc.get("energy_above_hull"),
            "spacegroup_reported": symmetry.get("symbol") if isinstance(symmetry, dict) else None,
            "nsites": doc.get("nsites"),
            "theoretical": theoretical,
            "icsd_ids": list(icsd_ids)[:10] or None,
            "total_magnetization": doc.get("total_magnetization"),
            "ordering_reported": str(doc.get("ordering")) if doc.get("ordering") else None,
            "magnetic_state_determined_by": "API ordering field",
            "thermo_type_used": THERMO_TYPE,
            "summary_formation_energy_per_atom": doc.get("formation_energy_per_atom"),
            **functional_extras,
        }

        url = f"https://next-gen.materialsproject.org/materials/{mpid}"
        magnetic_state = self._magnetic_state(doc)
        out: list[PropertyRecord] = []

        for prop in properties:
            if prop is Property.FORMATION_ENERGY_PER_ATOM:
                if thermo is None:
                    self.failures.append(
                        SourceFailure(
                            source=Source.MATERIALS_PROJECT,
                            query=f"thermo {mpid} thermo_type={THERMO_TYPE}",
                            reason=(
                                "no GGA_GGA+U thermo document, so no formation energy "
                                "comparable with OQMD exists for this material"
                            ),
                        )
                    )
                    continue
                value = thermo.get("formation_energy_per_atom")
                if value is None:
                    continue
                extras = dict(common_extras)
                extras["thermo_energy_above_hull"] = thermo.get("energy_above_hull")
                out.append(
                    PropertyRecord(
                        source=Source.MATERIALS_PROJECT,
                        source_id=mpid,
                        formula=formula,
                        property_name=Property.FORMATION_ENERGY_PER_ATOM,
                        value=float(value),
                        units="eV/atom",
                        functional=functional,
                        correction_scheme=FORMATION_ENERGY_CORRECTION,
                        magnetic_state=magnetic_state,
                        value_kind=ValueKind.COMPUTED,
                        structure_is_icsd_derived=structure_is_icsd,
                        structure=structure,
                        source_url=url,
                        extras=extras,
                    )
                )
            elif prop is Property.BAND_GAP:
                value = doc.get("band_gap")
                if value is None:
                    continue
                extras = dict(common_extras)
                extras["is_gap_direct"] = doc.get("is_gap_direct")
                out.append(
                    PropertyRecord(
                        source=Source.MATERIALS_PROJECT,
                        source_id=mpid,
                        formula=formula,
                        property_name=Property.BAND_GAP,
                        value=float(value),
                        units="eV",
                        functional=functional,
                        correction_scheme="none",
                        magnetic_state=magnetic_state,
                        value_kind=ValueKind.COMPUTED,
                        structure_is_icsd_derived=structure_is_icsd,
                        structure=structure,
                        source_url=url,
                        extras=extras,
                    )
                )
        return out

    # -- public -------------------------------------------------------------

    def records_for(
        self,
        formula: str | None = None,
        material_ids: Sequence[str] | None = None,
        properties: Iterable[Property] = (
            Property.FORMATION_ENERGY_PER_ATOM,
            Property.BAND_GAP,
        ),
    ) -> list[PropertyRecord]:
        properties = list(properties)
        try:
            docs = self.summary(formula=formula, material_ids=material_ids)
        except MissingAPIKey:
            raise
        except Exception as exc:
            self.failures.append(
                SourceFailure(
                    source=Source.MATERIALS_PROJECT,
                    query=f"summary formula={formula} ids={material_ids}",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
            return []

        ids = [str(d.get("material_id")) for d in docs if d.get("material_id")]
        thermo_by_id: dict[str, dict[str, Any]] = {}
        if Property.FORMATION_ENERGY_PER_ATOM in properties and ids:
            try:
                thermo_by_id = self.thermo(ids)
            except Exception as exc:
                self.failures.append(
                    SourceFailure(
                        source=Source.MATERIALS_PROJECT,
                        query=f"thermo ids={len(ids)} thermo_type={THERMO_TYPE}",
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                )

        records: list[PropertyRecord] = []
        for doc in docs:
            try:
                records.extend(
                    self._records_from_doc(
                        doc, thermo_by_id.get(str(doc.get("material_id"))), properties
                    )
                )
            except Exception as exc:
                self.failures.append(
                    SourceFailure(
                        source=Source.MATERIALS_PROJECT,
                        query=f"record for {doc.get('material_id')}",
                        reason=f"record construction failed: {type(exc).__name__}: {exc}",
                    )
                )
        return records
