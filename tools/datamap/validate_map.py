"""Check coverage, metadata, local links and same-name folder notes using stdlib."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]


def validate(vault):
    vault = vault.resolve()
    errors = []
    types = Counter()
    ids = set()
    links = 0
    notes = list(vault.rglob("*.md"))
    notes = [p for p in notes if ".obsidian" not in p.parts and ".trash" not in p.parts]
    for path in notes:
        text = path.read_text(encoding="utf-8")
        front = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
        if not front:
            errors.append(f"Missing properties: {path}")
            continue
        metadata = {}
        for line in front[1].splitlines():
            key, value = line.split(":", 1)
            metadata[key] = json.loads(value)
        types[metadata["type"]] += 1
        if "id" in metadata:
            if metadata["id"] in ids:
                errors.append(f"Duplicate id: {metadata['id']}")
            ids.add(metadata["id"])
        for label, href in re.findall(r"!?\[([^\]\n]*)\]\(([^)\n]+)\)", text):
            parsed = urlsplit(href)
            if parsed.scheme or href.startswith("#"):
                continue
            target = (path.parent / unquote(parsed.path)).resolve()
            links += 1
            if not target.is_relative_to(vault) or not target.is_file():
                errors.append(f"Unresolved or outside-vault link: {path.relative_to(vault)} -> {href}")
    for directory in [p for p in vault.rglob("*") if p.is_dir() and not any(part.startswith(".") for part in p.relative_to(vault).parts)]:
        if not (directory / f"{directory.name}.md").is_file():
            errors.append(f"Missing folder note: {directory}")
    for kind, count in {"signal": 97, "lab-series": 54, "formula": 17, "analyzer": 2, "source": 7, "sample-point": 6, "diagram": 3}.items():
        if types[kind] != count:
            errors.append(f"Coverage {kind}: expected {count}, got {types[kind]}")
    dictionary = json.loads((Path(__file__).parent / "evidence/tag_dictionary.json").read_text(encoding="utf-8"))
    for tag in dictionary:
        if f"{tag['stage']}:{tag['tag']}" not in ids:
            errors.append(f"Missing signal: {tag}")
    evidence = Path(__file__).parent / "evidence"
    profile = json.loads((evidence / "profile_summary.json").read_text(encoding="utf-8"))
    for series in profile["lims"]:
        number = re.search(r"Точка отбора '([^']+)'", series["point"])[1]
        pid = ("AVT" if "'АВТ'" in series["point"] else "HT") + number
        expected = f"LIMS:{pid}:{series['parameter']}:{series['excel_columns'].replace(':', '-')}"
        if expected not in ids:
            errors.append(f"Missing laboratory series: {expected}")
    formulas = json.loads((evidence / "vak_source.json").read_text(encoding="utf-8"))
    for row in formulas[1:]:
        for offset in (0, 2, 4):
            name, expression = row[offset:offset+2]
            if not name:
                continue
            path = vault / "Источники/Справочник/ВАК" / f"VAK-{name.replace(':', '-')}.md"
            if name not in ids or not path.is_file() or f"```text\n{expression}\n```" not in path.read_text(encoding="utf-8"):
                errors.append(f"Missing or altered source formula: {name}")
    if errors:
        raise SystemExit("\n".join(errors))
    print(json.dumps({"notes": len(notes), "links_checked": links, "types": types, "result": "PASS"}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=ROOT / "DataMap")
    validate(parser.parse_args().vault)
