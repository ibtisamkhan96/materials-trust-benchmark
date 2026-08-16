"""Configuration, paths, and the documented numerical thresholds.

Every threshold used anywhere in the physics checks is defined here with a
written justification, so that the README and the report can quote it and a
reader can audit it.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
CACHE_DIR = DATA_DIR / "cache"
RESULTS_DIR = PROJECT_ROOT / "results"
DOCS_DIR = PROJECT_ROOT / "docs"
FIGURES_DIR = RESULTS_DIR / "figures"


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, CACHE_DIR, RESULTS_DIR, DOCS_DIR, FIGURES_DIR):
        d.mkdir(parents=True, exist_ok=True)


def mp_api_key() -> str | None:
    return os.environ.get("MP_API_KEY") or None


def anthropic_api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY") or None


# ---------------------------------------------------------------------------
# OQMD access
# ---------------------------------------------------------------------------

OQMD_BASE_URL = os.environ.get("OQMD_BASE_URL", "https://oqmd.org").rstrip("/")
OQMD_TIMEOUT_SECONDS = float(os.environ.get("OQMD_TIMEOUT_SECONDS", "60"))
OQMD_MAX_RETRIES = 3
OQMD_BACKOFF_SECONDS = 4.0

# ---------------------------------------------------------------------------
# Structure matching tolerances
# ---------------------------------------------------------------------------
# These are the pymatgen StructureMatcher defaults. They are deliberately not
# loosened: a loose matcher would merge distinct polymorphs and manufacture
# false agreement, which is the single worst failure mode available to this
# project. They are recorded here so the report can state them.

STRUCTURE_MATCH_LTOL = 0.2  # fractional length tolerance
STRUCTURE_MATCH_STOL = 0.3  # site tolerance, fraction of average free length
STRUCTURE_MATCH_ANGLE_TOL = 5.0  # degrees

# ---------------------------------------------------------------------------
# Disagreement thresholds
# ---------------------------------------------------------------------------
# Formation energy: 50 meV/atom. Chosen because it is the conventional scale at
# which DFT formation energies are considered to agree, and it is comfortably
# larger than numerical noise between well converged calculations of the same
# structure with the same functional. Disagreements above it require a physical
# explanation rather than being attributable to convergence settings.
LARGE_DISAGREEMENT_FORMATION_ENERGY_EV_PER_ATOM = 0.050

# Band gap: 0.5 eV. Chosen because it is large relative to the numerical spread
# expected from k-point sampling of the same structure and functional, while
# being small relative to the systematic PBE underestimation this project
# quantifies (roughly 1 eV on average for semiconductors).
LARGE_DISAGREEMENT_BAND_GAP_EV = 0.5

# A formation energy outside this window is treated as a unit or parsing error
# rather than a physical value. The most negative formation energies in either
# database are around -4 eV/atom.
PHYSICAL_FORMATION_ENERGY_BOUND_EV_PER_ATOM = 20.0

# A DFT or measured band gap above this is treated as a parsing error.
PHYSICAL_BAND_GAP_BOUND_EV = 25.0

# ---------------------------------------------------------------------------
# Polymorph spread tolerance for the DFT versus experiment comparison
# ---------------------------------------------------------------------------
# An experimental band gap is reported against a composition, not against a
# structure. Where a composition has several computed polymorphs whose gaps
# differ by more than this, the comparison is not clean and is reported
# separately rather than folded into the headline statistics.
POLYMORPH_GAP_SPREAD_TOLERANCE_EV = 0.5
