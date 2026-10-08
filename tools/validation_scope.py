"""Conservative, deterministic validation planning; no third-party imports or execution."""

from __future__ import annotations

import ast
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "data": "packages/dnd5e-srd-data",
    "engine": "packages/dnd5e-engine",
    "bridge": "packages/nat20-bridge",
    "demo": "apps/demo",
}
DOWNSTREAM = {
    "data": {"data", "engine", "bridge", "demo"},
    "engine": {"engine", "bridge", "demo"},
    "bridge": {"bridge"},
    "demo": {"demo"},
}
CORE = [
    "tests/test_smoke.py",
    "tests/test_public_api_surface.py",
    "tests/test_delivery_hardening.py",
    "tests/test_monster_target_selection.py",
    "tests/test_delivery_lifecycle_provenance.py",
    "tests/test_reaction_runtime.py",
    "tests/test_typed_effect_lifecycle.py",
    "tests/test_typed_check_pipeline.py",
    "tests/test_physical_movement_runtime.py",
    "tests/activities/test_save_primitive.py",
    "tests/activities/test_delivery_activity_semantics.py",
]
CONTRACTS = {
    "data": ["tests/test_schema_common.py", "tests/test_schema_monster.py"],
    "engine": CORE,
    "bridge": ["tests/test_combat_e2e.py"],
    "demo": ["tests/test_engine_surface_usage.py", "tests/test_replay.py"],
}
PUBLIC_ENGINE = {
    "__init__.py",
    "orchestrator.py",
    "events.py",
    "specs.py",
    "results.py",
    "outcome.py",
    "testing.py",
    "lib_loader.py",
    "build_spec.py",
    "build_party.py",
    "spell_delivery.py",
    "live_spell_delivery.py",
    "live_monster_delivery.py",
    "reactions.py",
    "live_reactions.py",
    "effect_lifecycle.py",
    "turn_lifecycle.py",
}


@dataclass
class Plan:
    level: str
    changed: list[str]
    tests: dict[str, list[str]] = field(default_factory=dict)
    packages: list[str] = field(default_factory=list)
    docs: bool = False
    tooling: bool = False
    examples: bool = False
    integration_required: bool = False
    reasons: list[str] = field(default_factory=list)

    def json(self) -> dict[str, object]:
        return asdict(self)


def git(*args: str, root: Path = ROOT) -> str:
    return subprocess.check_output(["git", *args], cwd=root).decode("utf-8")


def changed_files(base: str | None = None, root: Path = ROOT) -> list[str]:
    """An explicit base is exact (CI push.before / PR merge-base), never silently ignored."""
    # Keep index and worktree deltas separate: opposite edits can cancel in diff HEAD.
    dirty = set(git("diff", "--name-only", "--no-renames", "-z", root=root).split("\0"))
    dirty.update(
        git("diff", "--cached", "--name-only", "--no-renames", "-z", root=root).split("\0")
    )
    dirty.update(git("ls-files", "--others", "--exclude-standard", "-z", root=root).split("\0"))
    dirty.discard("")
    if base:
        # Validate the ref separately; an invalid CI base must fail rather than under-test.
        git("rev-parse", "--verify", f"{base}^{{commit}}", root=root)
        dirty.update(
            git("diff", "--name-only", "--no-renames", "-z", base, "HEAD", root=root).split("\0")
        )
    elif not dirty:
        # Clean checkout: validate the latest batch, not every earlier branch commit.
        dirty.update(
            git(
                "diff-tree",
                "--root",
                "--first-parent",
                "-m",
                "--no-commit-id",
                "--name-only",
                "--no-renames",
                "-r",
                "-z",
                "HEAD",
                root=root,
            ).split("\0")
        )
    return sorted(dirty - {""})


def imports(path: Path, module: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    dependencies: set[str] = set()
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            dependencies.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = node.module or ""
            if node.level:
                parents = package.split(".")
                prefix = ".".join(
                    parents[: len(parents) - node.level + 1] + ([prefix] if prefix else [])
                )
            dependencies.add(prefix)
            dependencies.update(f"{prefix}.{alias.name}" for alias in node.names)
    return dependencies


def related_engine_tests(paths: list[str], root: Path) -> list[str]:
    """Reverse import closure including relative imports, helpers and package initializers.

    Unknown/deleted source and syntax errors fall back to the entire engine suite.
    This is an optimization, not the integration gate for public/dynamic contracts.
    """
    engine = root / PACKAGES["engine"]
    modules: dict[str, Path] = {}
    for directory in (engine / "src", engine):
        search = directory if directory.name == "src" else directory / "tests"
        for path in search.rglob("*.py"):
            module = ".".join(path.relative_to(directory).with_suffix("").parts)
            modules[module.removesuffix(".__init__")] = path
    changed_modules = {
        name for name, path in modules.items() if path.relative_to(root).as_posix() in paths
    }
    if not changed_modules:
        return ["tests"]
    try:
        edges = {name: imports(path, name) for name, path in modules.items()}
    except (SyntaxError, UnicodeError):
        return ["tests"]
    # Python initializes each parent package when importing a child module.
    for name, dependencies in edges.items():
        dependencies.update(".".join(name.split(".")[:i]) for i in range(1, len(name.split("."))))
    affected = set(changed_modules)
    while True:
        expanded = affected | {
            name for name, dependencies in edges.items() if dependencies & affected
        }
        if expanded == affected:
            break
        affected = expanded
    selected = sorted(
        {
            path.relative_to(engine).as_posix()
            for name, path in modules.items()
            if name in affected and path.name.startswith("test_")
        }
    )
    return selected or ["tests"]


def build_plan(paths: list[str], level: str = "fast", root: Path = ROOT) -> Plan:
    plan = Plan(level=level, changed=sorted(set(paths)))
    selected: dict[str, set[str]] = {}
    owners: set[str] = set()
    expanded: set[str] = set()
    engine_sources: list[str] = []

    def add(package: str, tests: list[str]) -> None:
        selected.setdefault(package, set()).update(tests)

    for path in plan.changed:
        owner = next((key for key, value in PACKAGES.items() if path.startswith(value + "/")), None)
        if path.startswith("docs/") or path.endswith(".md") or path == "mkdocs.yml":
            plan.docs = True
            if path == "AGENTS.md":
                plan.tooling = True
            continue
        if path in {"pyproject.toml", "uv.lock", ".python-version"}:
            expanded.update(PACKAGES)
            plan.reasons.append(f"workspace dependency/configuration: {path}")
        elif path.startswith(("tools/", ".github/")) or path == "Makefile":
            plan.tooling = True
            expanded.update(PACKAGES)
            plan.reasons.append(f"validation infrastructure: {path}")
        elif path.startswith("examples/"):
            owners.add("engine")
            plan.examples = True
            add("engine", CORE)
        elif owner:
            owners.add(owner)
            relative = path[len(PACKAGES[owner]) + 1 :]
            if relative == "Makefile" or relative.startswith("scripts/smoke_clean_install"):
                plan.tooling = True
                expanded.update(DOWNSTREAM[owner])
                plan.reasons.append(f"package validation infrastructure: {path}")
            elif (
                relative.startswith("tests/")
                and relative.endswith(".py")
                and (root / path).is_file()
                and Path(path).name.startswith("test_")
            ):
                add(owner, [relative])
            elif (
                owner == "engine"
                and relative.startswith("src/")
                and relative.endswith(".py")
                and (root / path).is_file()
            ):
                engine_sources.append(path)
                if Path(path).name in PUBLIC_ENGINE or "/activities/" in path or "/types/" in path:
                    expanded.update(DOWNSTREAM[owner])
                    plan.reasons.append(f"public engine/resolver contract: {path}")
            elif owner in {"bridge", "demo"} and relative.startswith(("src/", "tests/")):
                add(owner, ["tests"])
            elif owner == "data":
                expanded.update(DOWNSTREAM[owner])
                add(owner, ["tests"])
                plan.reasons.append(f"schema/canonical/data tooling contract: {path}")
            else:
                expanded.update(DOWNSTREAM[owner])
                add(owner, ["tests"])
                plan.reasons.append(f"package configuration/helper/deleted path: {path}")
        elif path in {"LICENSE", ".gitignore", ".gitattributes"}:
            plan.tooling = True
        else:
            # Unknown files may be a new package/build/runtime input. Never silently omit them.
            expanded.update(PACKAGES)
            for key in PACKAGES:
                add(key, ["tests"])
            plan.tooling = True
            plan.reasons.append(f"unknown path; full workspace runtime fallback: {path}")

    if engine_sources:
        add("engine", related_engine_tests(engine_sources, root))
    if len(engine_sources) > 1 or len(owners) > 1:
        for owner in owners:
            expanded.update(DOWNSTREAM[owner])
        plan.reasons.append("multiple underlying modules/packages changed")
    if "engine" in owners:
        add("engine", CORE)
    plan.integration_required = bool(expanded)
    for package in expanded:
        add(package, ["tests"] if level in {"integration", "full"} else CONTRACTS[package])
    if level == "integration":
        for package in owners:
            add(package, ["tests"])
        plan.examples |= bool(expanded & {"data", "engine"} or "engine" in owners)
        plan.docs |= bool(expanded & {"data", "engine"})
    if level == "full":
        selected = {key: {"tests"} for key in PACKAGES}
        plan.docs = plan.tooling = plan.examples = True
    plan.packages = [key for key in PACKAGES if key in selected]
    plan.tests = {
        key: ["tests"] if "tests" in selected[key] else sorted(selected[key])
        for key in plan.packages
    }
    return plan
