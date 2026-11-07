"""The canon-authority token is created in exactly the places the board ruled (v2.3.16).

"Private" is a naming convention in Python, not enforcement — so this test pins every place the
``CanonAuthority`` token is constructed in production code and FAILS if a new one appears:

* ``MemoryStore.approve``   (mori_advisor/memory_store.py) — the SQLite approval path
* ``PostgresStore.approve`` (mori_advisor/store/postgres_store.py) — the Postgres approval path
* ``import_standards``      (mori_advisor/main.py) — the standards import (dreamer-gated)

It also pins which modules may import the class at all, so an aliased import (``import ... as X``)
or a module-attribute call cannot create a site this scan misses. Tests may build the token to
seed canon (tests/_canon.py); they are outside the scanned trees.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("mori_advisor", "mori_intake", "scripts")

EXPECTED_SITES = {
    ("mori_advisor/memory_store.py", "approve"),
    ("mori_advisor/store/postgres_store.py", "approve"),
    ("mori_advisor/main.py", "import_standards"),
}
# Modules allowed to IMPORT the class: the three creators, plus the module that defines it and
# the SQLite/Postgres chokepoints' type checks (isinstance) which live in the same two stores.
EXPECTED_IMPORTERS = {
    "mori_advisor/memory_store.py",
    "mori_advisor/store/postgres_store.py",
    "mori_advisor/main.py",
}


def _sites(tree: ast.AST, rel: str) -> tuple[set, set, bool]:
    """(creation sites, importer, references-by-other-means) for one module."""
    sites: set = set()
    imports = False
    other = False
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def enclosing(node) -> str:
        best = None
        for f in funcs:
            if f.lineno <= node.lineno <= (f.end_lineno or f.lineno):
                if best is None or f.lineno > best.lineno:
                    best = f
        return best.name if best else "<module>"

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "canon_authority" in node.module:
            for a in node.names:
                if a.name == "CanonAuthority":
                    imports = True
                    if a.asname and a.asname != "CanonAuthority":
                        other = True  # an alias would hide a creation site from this scan
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id == "CanonAuthority") or (
                isinstance(f, ast.Attribute) and f.attr == "CanonAuthority"
            ):
                sites.add((rel, enclosing(node)))
        if isinstance(node, ast.Constant) and node.value == "CanonAuthority":
            other = True  # getattr(module, "CanonAuthority") and friends
    return sites, ({rel} if imports else set()), other


def scan(roots=SCANNED, base: Path = ROOT) -> tuple[set, set, set]:
    sites: set = set()
    importers: set = set()
    others: set = set()
    for top in roots:
        for path in sorted((base / top).rglob("*.py")):
            rel = path.relative_to(base).as_posix()
            if rel == "mori_advisor/canon_authority.py":
                continue  # the definition itself
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            s, i, o = _sites(tree, rel)
            sites |= s
            importers |= i
            if o:
                others.add(rel)
    return sites, importers, others


def test_the_token_is_created_only_where_the_board_ruled():
    sites, _importers, _others = scan()
    assert sites == EXPECTED_SITES, (
        "CanonAuthority is created somewhere new (or a ruled site is gone). Every creation site "
        "is a way to change canon without review — it needs a board ruling, then this list.\n"
        f"found: {sorted(sites)}"
    )


def test_only_the_ruled_modules_import_the_token():
    _sites_, importers, others = scan()
    assert importers == EXPECTED_IMPORTERS, sorted(importers)
    assert others == set(), f"aliased or by-name references to CanonAuthority: {sorted(others)}"


def test_the_scan_catches_a_new_site(tmp_path):
    """The pin is unable not to fire: a creation anywhere else is reported."""
    pkg = tmp_path / "mori_advisor"
    pkg.mkdir()
    (pkg / "rogue.py").write_text(
        "from mori_advisor.canon_authority import CanonAuthority\n\n"
        "def _write(**kw):\n    return CanonAuthority('sneaky')\n"
    )
    (pkg / "aliased.py").write_text(
        "from mori_advisor.canon_authority import CanonAuthority as CA\n\nTOKEN = CA('x')\n"
    )
    sites, importers, others = scan(roots=("mori_advisor",), base=tmp_path)
    assert ("mori_advisor/rogue.py", "_write") in sites
    assert "mori_advisor/aliased.py" in others
