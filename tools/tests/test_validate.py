"""Check gate composition without installing dependencies or running runtime tests."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import validate  # noqa: E402


class RunnerTests(unittest.TestCase):
    def commands(self, *arguments: str) -> list[list[str]]:
        with (
            patch.object(sys, "argv", ["validate.py", *arguments]),
            patch.object(validate, "run") as run,
            patch.dict(os.environ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            validate.main()
        return [call.args[0] for call in run.call_args_list]

    def test_plan_never_runs_sync_or_checks(self) -> None:
        self.assertEqual(self.commands("fast", "--files", "README.md", "--plan"), [])

    def test_docs_only_builds_docs_without_runtime_tests(self) -> None:
        commands = self.commands("fast", "--files", "docs/index.md")
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][:3], ["uv", "sync", "--locked"])
        self.assertIn("--group", commands[0])
        self.assertEqual(commands[1][-3:], ["mkdocs", "build", "--strict"])

    def test_fast_syncs_once_and_omits_expensive_full_checks(self) -> None:
        commands = self.commands("fast", "--files", "tools/validate.py")
        self.assertEqual(sum(command[:2] == ["uv", "sync"] for command in commands), 1)
        pytest = [command for command in commands if "pytest" in command]
        self.assertEqual(len(pytest), 4)
        self.assertTrue(all("tests" not in command for command in pytest))
        self.assertTrue(all("--cov" not in " ".join(command) for command in commands))
        self.assertFalse(any("smoke_clean_install.py" in " ".join(command) for command in commands))
        self.assertFalse(any(command[0] == "make" for command in commands))
        self.assertTrue(
            all("--no-sync" in command for command in commands if command[:2] == ["uv", "run"])
        )

    def test_auto_escalates_and_never_duplicates_fast_tests(self) -> None:
        commands = self.commands("fast", "--auto", "--files", "tools/validate.py")
        pytest = [command for command in commands if "pytest" in command]
        self.assertEqual(len(pytest), 4)
        self.assertTrue(all(command[-1] == "tests" for command in pytest))
        self.assertFalse(any(command[0] == "make" for command in commands))

    def test_bridge_does_not_install_unrelated_dev_packages(self) -> None:
        commands = self.commands("fast", "--files", "packages/nat20-bridge/src/server.py")
        self.assertEqual(
            commands[0], ["uv", "sync", "--locked", "--package", "nat20-bridge", "--extra", "dev"]
        )
        self.assertEqual(sum("pytest" in command for command in commands), 1)

    def test_full_delegates_original_package_gates_once(self) -> None:
        commands = self.commands("full", "--files", "README.md")
        self.assertEqual(sum(command == ["make", "check"] for command in commands), 4)
        self.assertFalse(any("pytest" in command for command in commands))
        self.assertEqual(
            sum(command[-1].endswith("smoke_clean_install.py") for command in commands), 1
        )
        self.assertEqual(sum("mkdocs" in command for command in commands), 1)

    def test_command_failure_is_not_reported_as_success(self) -> None:
        with (
            patch.object(sys, "argv", ["validate.py", "fast", "--files", "README.md"]),
            patch.object(validate, "run", side_effect=RuntimeError("sync failed")),
            contextlib.redirect_stdout(io.StringIO()) as output,
            self.assertRaisesRegex(RuntimeError, "sync failed"),
        ):
            validate.main()
        self.assertNotIn("PASSED", output.getvalue())


if __name__ == "__main__":
    unittest.main()
