"""Regression checks for preserving personal settings during vault bootstrap."""
import json
from pathlib import Path
import tempfile
import unittest

from setup_vault import setup, write_json


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class SetupTests(unittest.TestCase):
    def test_clean_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            setup(root)
            before = snapshot(root)
            setup(root)
            self.assertEqual(before, snapshot(root))
            self.assertEqual(json.loads((root / ".obsidian/community-plugins.json").read_text()), ["folder-notes"])

    def test_personal_settings_and_updated_plugin_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / ".obsidian"
            for name in ("app", "appearance", "workspace", "hotkeys", "core-plugins", "templates"):
                write_json(config / f"{name}.json", {"personal": name})
            write_json(config / "community-plugins.json", ["dataview", "another-plugin"])
            plugin = config / "plugins/folder-notes"
            write_json(plugin / "manifest.json", {"id": "folder-notes", "version": "99.0.0"})
            write_json(plugin / "data.json", {"hideFolderNote": False})
            for name in ("main.js", "styles.css"):
                (plugin / name).write_text("user updated asset", encoding="utf-8")
            before = snapshot(root)
            setup(root)
            after = snapshot(root)
            for name, content in before.items():
                if not name.endswith("community-plugins.json"):
                    self.assertEqual(content, after[name], name)
            self.assertEqual(json.loads((config / "community-plugins.json").read_text()), ["dataview", "another-plugin", "folder-notes"])
            setup(root)
            self.assertEqual(after, snapshot(root))

    def test_bad_config_and_partial_install_leave_vault_unchanged(self):
        for mode in ("config", "partial"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = root / ".obsidian"
                if mode == "config":
                    write_json(config / "community-plugins.json", {"bad": True})
                else:
                    write_json(config / "plugins/folder-notes/manifest.json", {"id": "folder-notes"})
                before = snapshot(root)
                with self.assertRaises(ValueError):
                    setup(root)
                self.assertEqual(before, snapshot(root))


if __name__ == "__main__":
    unittest.main()
