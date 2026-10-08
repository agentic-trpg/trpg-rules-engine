"""Shared local/CI validation entrypoint. Planning has no environment mutations."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True

from validation_scope import PACKAGES, ROOT, build_plan, changed_files  # noqa: E402


def run(command: list[str], cwd: Path = ROOT) -> None:
    started = time.perf_counter()
    print(f"\n[{cwd.relative_to(ROOT) or '.'}] {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, check=True)
    print(f"Completed in {time.perf_counter() - started:.2f}s", flush=True)


def main() -> None:
    started = time.perf_counter()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("level", choices=["fast", "integration", "full", "docs", "smoke"])
    parser.add_argument("--base", help="Exact Git base; includes staged/unstaged/untracked changes")
    parser.add_argument(
        "--files", nargs="+", help="Explicit impact paths, for scope inspection/benchmarks"
    )
    parser.add_argument(
        "--plan", action="store_true", help="JSON only; no sync/tests/state changes"
    )
    parser.add_argument(
        "--auto", action="store_true", help="Raise fast to integration for public/high-risk changes"
    )
    args = parser.parse_args()
    paths = args.files if args.files is not None else changed_files(args.base)
    plan = build_plan(paths, args.level)
    if args.auto and args.level == "fast" and plan.integration_required:
        plan = build_plan(paths, "integration")
    if args.level == "docs":
        plan.packages, plan.tests, plan.docs = [], {}, True
        plan.tooling = plan.examples = False
    if args.level == "smoke":
        plan.packages, plan.tests, plan.docs = [], {}, False
        plan.tooling = plan.examples = False
    if args.plan:
        print(json.dumps(plan.json(), indent=2))
        return
    print(json.dumps(plan.json(), indent=2), flush=True)
    if plan.integration_required and plan.level == "fast":
        print(
            "PUBLIC/HIGH-RISK CHANGE: run check-integration instead of repeating check-fast.",
            flush=True,
        )
    # Bootstrap once; all later uv runs must neither resolve nor synchronize.
    sync = ["uv", "sync", "--locked"]
    if plan.packages or plan.tooling or plan.docs:
        if plan.tooling or len(plan.packages) > 1:
            sync += ["--all-packages", "--extra", "dev"]
        elif plan.packages:
            sync += [
                "--package",
                {
                    "data": "dnd5e-srd-data",
                    "engine": "dnd5e-engine",
                    "bridge": "nat20-bridge",
                    "demo": "nat20-demo",
                }[plan.packages[0]],
                "--extra",
                "dev",
            ]
        else:
            # mkdocstrings imports both runtime packages.
            sync += ["--package", "dnd5e-engine"]
        if plan.docs:
            sync += ["--group", "docs"]
        run(sync)
    os.environ["UV_NO_SYNC"] = "1"
    os.environ["UV_LOCKED"] = "1"
    uv = ["uv", "run", "--no-sync"]
    if plan.tooling:
        run(uv + ["ruff", "check", "tools"])
        run(uv + ["ruff", "format", "--check", "tools"])
        run(uv + ["mypy", "--strict", "tools"])
        run(uv + ["python", "-m", "unittest", "discover", "-s", "tools/tests", "-v"])
    for package in plan.packages:
        cwd = ROOT / PACKAGES[package]
        if plan.level == "full":
            run(["make", "check"], cwd)
        else:
            # Retain configured package lint/type rules; runtime tests carry no coverage overhead.
            targets = ["src", "tests"] + (["tools"] if package == "data" else [])
            run(uv + ["ruff", "check", *targets], cwd)
            run(uv + ["ruff", "format", "--check", *targets], cwd)
            run(uv + ["mypy", "src"], cwd)
            run(uv + ["pytest", "-q", *plan.tests[package]], cwd)
    if plan.examples:
        for name in ("grid_combat", "skill_check", "build_party_member"):
            run(uv + ["python", f"examples/{name}.py"])
    if plan.docs:
        run(uv + ["mkdocs", "build", "--strict"])
    if plan.level in {"full", "smoke"}:
        run([sys.executable, "tools/smoke_clean_install.py"])
    print(f"VALIDATION {plan.level}: PASSED in {time.perf_counter() - started:.2f}s", flush=True)


if __name__ == "__main__":
    main()
