import ast
import tempfile
import unittest
from pathlib import Path

from maintenance.paths import ROOT
from tools.check_repository import check


class RepositoryLayoutTests(unittest.TestCase):
    def test_production_does_not_import_developer_tools_or_retired_top_level_packages(self):
        forbidden = {"tools", "tests", "agent", "core", "ingest", "voice"}
        for path in (ROOT / "maintenance").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module]
                    if isinstance(node, ast.ImportFrom) and node.level == 0
                    else []
                )
                for name in modules:
                    if name:
                        self.assertNotIn(name.split(".")[0], forbidden, f"{path.name}: {name}")

    def test_private_file_and_nested_repository_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "vendor/.git").mkdir(parents=True)
            issues = check(root, ["config/local.json", "signing/key.p12", "data/notes.md", "vendor"])
            self.assertEqual(len(issues), 4)

    def test_credential_report_never_prints_value(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            key = "sk-" + "X" * 32
            (root / "settings.py").write_text(repr(key))
            issues = check(root, ["settings.py"])
            self.assertEqual(len(issues), 1)
            self.assertNotIn(key, str(issues))
