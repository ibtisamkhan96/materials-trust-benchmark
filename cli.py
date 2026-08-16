"""Entry point shim so the CLI can be run without installing the package.

    python cli.py audit TiO2

The implementation lives in ``src/materials_trust/cli.py``. Installing the
package also provides an ``mtb`` command.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from materials_trust.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
