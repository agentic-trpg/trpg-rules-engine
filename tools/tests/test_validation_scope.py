"""Small planner tests, including real Git worktree semantics and import closure."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validation_scope import (  # noqa: E402
    CONTRACTS,
    CORE,
    PACKAGES,
    ROOT,
    Plan,
    build_plan,
    changed_files,
    related_engine_tests,
)


class ScopeTests(unittest.TestCase):
    def plan(self, *paths: str, level: str = "fast") -> Plan:
        return build_plan(list(paths), level)

    def test_engine_test_only_is_scoped(self) -> None:
        plan = self.plan(PACKAGES["engine"] + "/tests/test_delivery_hardening.py")
        self.assertEqual(plan.packages, ["engine"])
        self.assertIn("tests/test_delivery_hardening.py", plan.tests["engine"])
        self.assertNotIn("tests", plan.tests["engine"])
        self.assertFalse(plan.integration_required)

    def test_engine_source_uses_reverse_dependency_closure(self) -> None:
        plan = self.plan(PACKAGES["engine"] + "/src/dnd5e_engine/size.py")
        self.assertEqual(plan.packages, ["engine"])
        self.assertIn("tests/test_smoke.py", plan.tests["engine"])

    def test_schema_and_canonical_expand_downstream(self) -> None:
        for path in ("src/dnd5e_srd_data/models/common.py", "canonical/spells/fireball.json"):
            with self.subTest(path=path):
                plan = self.plan(PACKAGES["data"] + "/" + path)
                self.assertEqual(set(plan.packages), set(PACKAGES))
                self.assertTrue(plan.integration_required)
                self.assertEqual(plan.tests["data"], ["tests"])

    def test_public_resolver_and_api_expand_downstream(self) -> None:
        for path in (
            "orchestrator.py",
            "activities/save.py",
            "types/combat.py",
            "spell_delivery.py",
        ):
            plan = self.plan(PACKAGES["engine"] + "/src/dnd5e_engine/" + path)
            self.assertEqual(set(plan.packages), {"engine", "bridge", "demo"})
            self.assertTrue(plan.integration_required)

    def test_bridge_and_demo_only(self) -> None:
        for owner in ("bridge", "demo"):
            plan = self.plan(PACKAGES[owner] + "/src/changed.py")
            self.assertEqual(plan.packages, [owner])
            self.assertEqual(plan.tests, {owner: ["tests"]})

    def test_docs_only_has_no_python_tests_or_examples(self) -> None:
        plan = self.plan("docs/dev/spell-area-delivery.md", "README.md", "mkdocs.yml")
        self.assertTrue(plan.docs)
        self.assertFalse(plan.tooling)
        self.assertFalse(plan.examples)
        self.assertEqual(plan.tests, {})

    def test_dependencies_and_member_configuration(self) -> None:
        for path in ("uv.lock", "pyproject.toml", PACKAGES["data"] + "/pyproject.toml"):
            plan = self.plan(path, level="integration")
            self.assertEqual(plan.tests, {key: ["tests"] for key in PACKAGES})
            self.assertTrue(plan.integration_required)

    def test_multiple_modules_require_integration(self) -> None:
        plan = self.plan(
            *(PACKAGES["engine"] + "/src/dnd5e_engine/" + name for name in ("size.py", "check.py"))
        )
        self.assertTrue(plan.integration_required)
        self.assertEqual(set(plan.packages), {"engine", "bridge", "demo"})

    def test_pipeline_fast_is_bounded_and_focused_is_complete(self) -> None:
        fast = self.plan("tools/validate.py", ".github/workflows/ci.yml")
        self.assertTrue(fast.tooling)
        self.assertEqual(fast.tests["engine"], sorted(CORE))
        self.assertNotIn("tests", fast.tests["engine"])
        focused = self.plan("tools/validate.py", level="integration")
        self.assertEqual(focused.tests, {key: ["tests"] for key in PACKAGES})
        self.assertTrue(focused.docs)

    def test_unknown_and_deleted_files_fall_back(self) -> None:
        plan = self.plan("unknown/runtime.config")
        self.assertEqual(plan.tests, {key: ["tests"] for key in PACKAGES})
        self.assertIn("unknown path", " ".join(plan.reasons))
        plan = self.plan(PACKAGES["engine"] + "/src/dnd5e_engine/deleted.py")
        self.assertEqual(plan.tests["engine"], ["tests"])
        self.assertTrue(plan.integration_required)

    def test_shared_test_helpers_fall_back(self) -> None:
        for path in ("tests/conftest.py", "tests/e2e/harness.py"):
            plan = self.plan(PACKAGES["engine"] + "/" + path)
            self.assertEqual(plan.tests["engine"], ["tests"])

    def test_full_retains_all_capabilities(self) -> None:
        plan = self.plan("README.md", level="full")
        self.assertEqual(plan.tests, {key: ["tests"] for key in PACKAGES})
        self.assertTrue(plan.docs and plan.tooling and plan.examples)

    def test_no_duplicate_tests_or_recursive_full_suite_overlap(self) -> None:
        plan = self.plan(
            "tools/validate.py",
            PACKAGES["engine"] + "/tests/test_delivery_hardening.py",
            "tools/validate.py",
        )
        self.assertEqual(len(plan.changed), 2)
        for tests in plan.tests.values():
            self.assertEqual(tests, sorted(set(tests)))
            if "tests" in tests:
                self.assertEqual(tests, ["tests"])

    def test_all_curated_tests_exist(self) -> None:
        for owner, tests in CONTRACTS.items():
            for test in tests:
                self.assertTrue((ROOT / PACKAGES[owner] / test).is_file(), test)

    def test_plan_cli_is_pure(self) -> None:
        before = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)
        result = subprocess.check_output(
            [
                sys.executable,
                str(ROOT / "tools/validate.py"),
                "fast",
                "--files",
                "docs/index.md",
                "--plan",
            ],
            cwd=ROOT,
        )
        self.assertEqual(json.loads(result)["tests"], {})
        self.assertEqual(
            before, subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)
        )

    def test_relative_imports_helpers_and_parent_initializers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                "src/dnd5e_engine/__init__.py": "",
                "src/dnd5e_engine/leaf.py": "VALUE = 1",
                "src/dnd5e_engine/middle.py": "from .leaf import VALUE",
                "tests/helper.py": "from dnd5e_engine.middle import VALUE",
                "tests/test_related.py": "from tests.helper import VALUE",
                "tests/test_other.py": "import math",
            }
            for name, content in files.items():
                path = root / PACKAGES["engine"] / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            changed = [PACKAGES["engine"] + "/src/dnd5e_engine/leaf.py"]
            self.assertEqual(related_engine_tests(changed, root), ["tests/test_related.py"])
            changed = [PACKAGES["engine"] + "/src/dnd5e_engine/__init__.py"]
            self.assertEqual(related_engine_tests(changed, root), ["tests/test_related.py"])


class GitDiffTests(unittest.TestCase):
    def test_base_and_staged_unstaged_untracked_rename_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args: str) -> str:
                return (
                    subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)
                    .decode()
                    .strip()
                )

            git("init")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Planner Test")
            (root / "initial.py").write_text("a", encoding="utf-8")
            (root / "deleted.py").write_text("b", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "initial")
            base = git("rev-parse", "HEAD")
            self.assertEqual(changed_files(root=root), ["deleted.py", "initial.py"])
            git("mv", "initial.py", "renamed.py")
            git("commit", "-m", "rename")
            (root / "deleted.py").unlink()
            (root / "staged.py").write_text("c", encoding="utf-8")
            git("add", "staged.py")
            (root / "untracked space.py").write_text("d", encoding="utf-8")
            # Opposite staged/worktree edits disappear from `diff HEAD`.
            (root / "renamed.py").write_text("changed", encoding="utf-8")
            git("add", "renamed.py")
            (root / "renamed.py").write_text("a", encoding="utf-8")
            self.assertEqual(
                changed_files(base, root),
                ["deleted.py", "initial.py", "renamed.py", "staged.py", "untracked space.py"],
            )
            self.assertEqual(
                changed_files(root=root),
                ["deleted.py", "renamed.py", "staged.py", "untracked space.py"],
            )
            with self.assertRaises(subprocess.CalledProcessError):
                changed_files("nonexistent-base", root)


if __name__ == "__main__":
    unittest.main()
