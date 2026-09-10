"""Import supplied hackathon files once; never change the originals."""

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.config import STORAGE
from backend.ingest import import_dataset


def main():
    parser = argparse.ArgumentParser(description="Импорт файлов Нефтекод")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--id", default="hackathon")
    parser.add_argument("--name", default="Нефтекод 2.0 · История 2023–2026")
    args = parser.parse_args()
    if any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in args.id
    ):
        parser.error("ID должен содержать только латинские буквы, цифры, дефис и подчёркивание")
    files = sorted(
        f
        for f in args.directory.rglob("*")
        if f.suffix.lower() in {".csv", ".xlsx"} and not f.name.startswith("~$")
    )
    if not files:
        parser.error("CSV/Excel не найдены")
    directory = STORAGE / args.id
    if directory.exists():
        parser.error(
            "Этот ID уже существует. Используйте другой --id; исходные наборы не перезаписываются."
        )
    raw = directory / "raw"
    raw.mkdir(parents=True)
    names = [f.name for f in files]
    if len(set(names)) != len(names):
        parser.error("Есть файлы с одинаковыми именами")
    paths = []
    for source in files:
        target = raw / source.name
        shutil.copy2(source, target)
        paths.append(target)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "id": args.id,
                "name": args.name,
                "status": "importing",
                "created_at": datetime.now().isoformat(),
            }
        )
    )
    start = time.perf_counter()
    result = import_dataset(paths, directory, args.name)
    print(
        json.dumps(
            {
                "id": result["id"],
                "status": result["status"],
                "seconds": round(time.perf_counter() - start, 2),
                "metrics": len(result["metrics"]),
                "sources": result["sources"],
                "issues": len(result["issues"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if result["status"] != "ready":
        sys.exit(1)


if __name__ == "__main__":
    main()
