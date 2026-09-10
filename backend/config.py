import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORAGE = Path(os.environ.get("OILCODE_STORAGE", ROOT / "storage"))
REGISTRY = ROOT / "backend" / "resources" / "registry.json"
FRESHNESS = {"kip": 10, "pak": 30, "lims": 48 * 60}
