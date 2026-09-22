"""Start the ready-to-run repository demo with its committed dataset."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    dataset = root / "storage" / "hackathon" / "observations.parquet"
    manifest = root / "storage" / "hackathon" / "manifest.json"
    if not dataset.is_file() or not manifest.is_file():
        print(
            "Данные демо не найдены. Выполните `git lfs pull` в ветке demo "
            "или загрузите набор через интерфейс.",
            file=sys.stderr,
        )
        return 2
    env = {**os.environ, "OILCODE_STORAGE": str(root / "storage")}
    return subprocess.call([sys.executable, str(root / "scripts" / "dev.py")], cwd=root, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
