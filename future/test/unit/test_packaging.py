"""Tree boundaries: entry points, the removed dashboard, engine dependencies."""
from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

TREE = Path(__file__).resolve().parents[2]
REPO = TREE.parent


class EntryPointTests(unittest.TestCase):
    """These spawn a real interpreter, so they can fail for reasons that have
    nothing to do with the code: a machine under load refuses the spawn with
    OSError. That is skipped rather than reported as a defect -- a flaky red
    test teaches people to ignore red tests."""

    def _run(self, args, cwd):
        try:
            return subprocess.run([sys.executable, *args], cwd=cwd,
                                  capture_output=True, text=True)
        except OSError as error:
            self.skipTest(f"could not spawn a subprocess: {error}")

    def test_root_level_module_execution_explains_itself(self):
        # Regression: `python -m future.entrypoints.live` died with a bare ImportError
        # from deep inside the module, because each tree is its own import root
        # and `bot` bound to the wrong package.
        result = self._run(["-m", "future.entrypoints.live", "--help"], REPO)
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("cd future", combined)
        self.assertNotIn("ImportError", combined)

    def test_tree_local_execution_still_works(self):
        result = self._run(["-m", "entrypoints.live"], TREE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FUTURES", result.stdout)

    def test_future_entrypoint_from_root_explains_tree_boundary(self):
        result = self._run(
            ["-m", "future.entrypoints.main", "--status", "--offline"], REPO)
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("cd future", combined)
        self.assertNotIn("ImportError", combined)


class RemovedDashboardTests(unittest.TestCase):
    def test_the_copied_webapp_refuses_rather_than_serving_a_missing_template(self):
        from strategy import webapp

        with self.assertRaises(NotImplementedError):
            webapp.create_app()

    def test_no_template_directory_is_referenced(self):
        source = (TREE / "strategy/webapp.py").read_text(encoding="utf-8")
        self.assertNotIn("template", source.lower())


class EngineDependencyTests(unittest.TestCase):
    """`engine` must not import `bot`, not even under TYPE_CHECKING."""

    def test_no_engine_module_imports_bot(self):
        offenders = []
        for path in (TREE / "engine").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("bot"):
                    offenders.append(f"{path.name}:{node.lineno}")
                if isinstance(node, ast.Import):
                    offenders += [f"{path.name}:{node.lineno}" for alias in node.names
                                  if alias.name.startswith("bot")]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
