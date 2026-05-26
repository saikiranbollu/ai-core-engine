"""F-CD-B01 smoke tests for build_knowledge_graph --clear scope.

Verifies that ``--clear --module ADC`` runs a module-scoped DETACH DELETE
(filtered by toUpper(n.module) = toUpper($mod)) instead of obliterating
the full database. A separate ``--clear-all`` flag is required for the
unscoped wipe and prompts for interactive confirmation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Load build_knowledge_graph directly from its file to bypass the
# src.HybridRAG.code.KG package __init__, which pulls in heavy optional
# deps (kg_query → ontology stack) not needed for the clear-scope tests.
import importlib.util

ROOT = Path(__file__).resolve().parents[2]
_BKG_PATH = ROOT / "src" / "HybridRAG" / "code" / "KG" / "build_knowledge_graph.py"
# The module uses sibling-flat imports (``from incremental_tracker import …``),
# so its directory must be on sys.path before exec_module.
_KG_DIR = str(_BKG_PATH.parent)
if _KG_DIR not in sys.path:
    sys.path.insert(0, _KG_DIR)
_spec = importlib.util.spec_from_file_location("_bkg_under_test", _BKG_PATH)
bkg = importlib.util.module_from_spec(_spec)
sys.modules["_bkg_under_test"] = bkg
_spec.loader.exec_module(bkg)  # type: ignore[union-attr]


def _make_builder(module: str = "ADC", clear_all: bool = False):
    """Construct a KnowledgeGraphBuilder with all I/O mocked out."""
    b = bkg.KnowledgeGraphBuilder.__new__(bkg.KnowledgeGraphBuilder)
    b.module = module
    b.clear_all = clear_all
    b.neo4j_cfg = {"database": "neo4j"}
    # Stub the two I/O sinks used by the clear paths.
    b._run = MagicMock(return_value=[{"c": 42}])
    b._write_tx = MagicMock()
    return b


def test_clear_with_module_is_module_scoped():
    """--clear --module ADC must DETACH DELETE only nodes WHERE module=ADC."""
    b = _make_builder(module="ADC", clear_all=False)

    b._clear_database()

    # _write_tx must have been called exactly once, with the scoped query.
    assert b._write_tx.call_count == 1
    cypher, params = b._write_tx.call_args.args
    assert "WHERE toUpper(n.module) = toUpper($mod)" in cypher
    assert "DETACH DELETE n" in cypher
    assert params == {"mod": "ADC"}
    # And critically: not the unscoped wipe.
    assert "MATCH (n) DETACH DELETE n" not in cypher


def test_clear_all_flag_requires_confirmation(monkeypatch):
    """--clear-all must prompt and only delete when DB name is typed back."""
    b = _make_builder(module=None, clear_all=True)

    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "neo4j")

    b._clear_database()

    # Exactly one delete, and it IS the full wipe.
    assert b._write_tx.call_count == 1
    cypher = b._write_tx.call_args.args[0]
    assert cypher.strip() == "MATCH (n) DETACH DELETE n"


def test_clear_all_aborts_on_wrong_confirmation(monkeypatch):
    """If the user types anything other than the DB name, raise ClearAbortedError."""
    b = _make_builder(module=None, clear_all=True)
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "wrong")

    with pytest.raises(bkg.ClearAbortedError):
        b._clear_database()

    b._write_tx.assert_not_called()


def test_clear_without_module_raises_deprecated_error():
    """F-CD-B01: --clear without --module must raise DeprecatedClearError."""
    b = _make_builder(module=None, clear_all=False)

    with pytest.raises(bkg.DeprecatedClearError, match="--clear used without --module"):
        b._clear_database()

    b._write_tx.assert_not_called()
