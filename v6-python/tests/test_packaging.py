"""Dependency-declaration tests.

pyproject.toml and requirements.txt drifted apart unnoticed because nothing
ever read both: the venv was built from requirements.txt, so pyproject's
copy was never exercised and was free to rot. It did -- it pinned
databento>=1.0.0 (which has never been published, so `pip install .` could
only fail) while omitting pytz, which normalize/session.py imports at module
scope.

These tests make that class of drift a build failure instead of a surprise on
whichever machine next runs a fresh install.
"""
import ast
import pathlib
import sys
import tomllib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
PACKAGES = ("premarketv6", "strategiesv6")


def _normalize(name):
    """PEP 503 name normalization: exchange_calendars -> exchange-calendars."""
    return "".join("-" if c in "-_." else c for c in name.strip().lower())


def _requirement_name(line):
    """'psycopg[binary]>=3.1.0' -> 'psycopg'. None for comments and blanks."""
    line = line.split("#", 1)[0].strip()
    if not line:
        return None
    for i, char in enumerate(line):
        if char in "[<>=!~; ":
            return _normalize(line[:i])
    return _normalize(line)


def _pyproject_names():
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    project = data["project"]
    declared = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        declared.extend(extra)
    return {n for n in map(_requirement_name, declared) if n}


def _requirements_names(filename):
    lines = (REPO / filename).read_text().splitlines()
    return {n for n in map(_requirement_name, lines) if n}


def _imported_top_level_modules():
    """Every non-stdlib module the shipped packages import, via AST.

    Regex over source picks up prose inside docstrings; ast does not.
    """
    modules = set()
    for package in PACKAGES:
        root = REPO / package
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules.add(node.module.split(".")[0])
    return {
        m for m in modules
        if m not in sys.stdlib_module_names and m not in PACKAGES
    }


def test_requirements_txt_matches_pyproject():
    """The two dependency lists must name the same distributions."""
    assert _requirements_names("requirements.txt") == _pyproject_names()


def test_every_imported_package_is_declared():
    """An import with no declaration is a fresh install that dies on startup."""
    from importlib.metadata import packages_distributions

    declared = _pyproject_names()
    mapping = packages_distributions()
    undeclared = {}
    for module in sorted(_imported_top_level_modules()):
        # Fall back to the module name when metadata has no mapping, so an
        # uninstalled-and-undeclared import still fails rather than vanishing.
        dists = {_normalize(d) for d in mapping.get(module, [module])}
        if not dists & declared:
            undeclared[module] = sorted(dists)
    assert not undeclared, f"imported but not declared in pyproject.toml: {undeclared}"


def test_lock_pins_every_declared_dependency():
    """requirements.lock is the machine-move artifact; a gap in it is a drift."""
    lock = REPO / "requirements.lock"
    if not lock.exists():
        pytest.skip("requirements.lock not generated")
    pinned = {
        _requirement_name(line)
        for line in lock.read_text().splitlines()
        if "==" in line and not line.lstrip().startswith("#")
    }
    missing = sorted(_pyproject_names() - pinned)
    assert not missing, f"declared but absent from requirements.lock: {missing}"
