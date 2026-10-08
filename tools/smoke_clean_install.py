"""Portable fresh-wheel smoke; no editable installs or ambient PYTHONPATH."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    env = {
        key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "UV_NO_SYNC"}
    }
    with tempfile.TemporaryDirectory(prefix="nat20-wheel-smoke-") as directory:
        work = Path(directory)

        def run(*command: str) -> None:
            subprocess.run(command, cwd=work, env=env, check=True)

        for package in ("dnd5e-srd-data", "dnd5e-engine", "nat20-bridge"):
            run(
                "uv",
                "build",
                "--wheel",
                "--package",
                package,
                "--project",
                str(ROOT),
                "--out-dir",
                str(work / "dist"),
            )
        requirements = work / "requirements.txt"
        run(
            "uv",
            "export",
            "--locked",
            "--project",
            str(ROOT),
            "--package",
            "nat20-bridge",
            "--no-dev",
            "--extra",
            "dev",
            "--no-emit-workspace",
            "--format",
            "requirements-txt",
            "--output-file",
            str(requirements),
        )
        run("uv", "venv", str(work / "venv"))
        python = work / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        wheels = sorted((work / "dist").glob("*.whl"))
        if len(wheels) != 3:
            raise RuntimeError(f"Expected exactly three wheels, got {wheels}")
        run(
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "-r",
            str(requirements),
            *(str(path) for path in wheels),
        )
        run(
            str(python),
            "-I",
            "-c",
            "import dnd5e_engine, dnd5e_srd_data, nat20_bridge; from pathlib import Path; "
            "assert all('site-packages' in Path(m.__file__).parts "
            "for m in (dnd5e_engine, dnd5e_srd_data, nat20_bridge)); "
            "print('All three packages imported from installed wheels')",
        )
        run(str(python), "-I", str(ROOT / "packages/dnd5e-engine/scripts/_smoke_grid_combat.py"))
        run(str(python), "-I", str(ROOT / "tools/smoke_bridge_http.py"))
        print(
            "SMOKE PASSED: isolated wheels, locked dependencies, public grid + Bridge HTTP",
            flush=True,
        )


if __name__ == "__main__":
    main()
