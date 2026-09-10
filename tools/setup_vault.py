"""Install the bundled Folder Notes without replacing personal vault settings."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "tools" / "obsidian" / "folder-notes"
ASSETS = ("main.js", "manifest.json", "styles.css")
DEFAULTS = {
    "folderNoteName": "{{folder_name}}",
    "newFolderNoteName": "{{folder_name}}",
    "storageLocation": "insideFolder",
    "hideFolderNote": True,
    "autoCreate": False,
    "syncDelete": False,
}


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def setup(vault):
    vault = Path(vault).resolve()
    if not vault.is_dir():
        raise ValueError(f"Vault directory does not exist: {vault}")
    config = vault / ".obsidian"
    plugin = config / "plugins" / "folder-notes"
    enabled_file = config / "community-plugins.json"
    enabled = read_json(enabled_file, [])
    if not isinstance(enabled, list) or not all(isinstance(x, str) for x in enabled):
        raise ValueError("community-plugins.json must contain a list of plugin IDs")
    # Validate before changing the vault. Never replace a user's plugin release.
    hashes = read_json(BUNDLE.parent / "provenance.json", {})["sha256"]
    for name in ASSETS:
        if hashlib.sha256((BUNDLE / name).read_bytes()).hexdigest() != hashes[name]:
            raise ValueError(f"Bundled asset hash mismatch: {name}")
    present = [name for name in ASSETS if (plugin / name).exists()]
    if present and len(present) != len(ASSETS):
        raise ValueError("Existing Folder Notes installation is incomplete; repair it in Obsidian first")
    if present:
        manifest = read_json(plugin / "manifest.json", {})
        if manifest.get("id") != "folder-notes":
            raise ValueError("Existing plugin manifest has an unexpected ID")
    else:
        plugin.mkdir(parents=True, exist_ok=True)
        for name in (*ASSETS, "LICENSE"):
            shutil.copyfile(BUNDLE / name, plugin / name)
    settings = plugin / "data.json"
    if not settings.exists():
        write_json(settings, DEFAULTS)
    if "folder-notes" not in enabled:
        write_json(enabled_file, enabled + ["folder-notes"])
    templates = config / "templates.json"
    if not templates.exists():
        write_json(templates, {"folder": "Шаблоны"})
    print(f"Folder Notes installed/enabled in {vault}. Personal settings preserved.")
    print("Open the vault in Obsidian; allow community plugins there if Restricted mode is on.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=ROOT / "DataMap")
    args = parser.parse_args()
    setup(args.vault)
