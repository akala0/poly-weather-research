"""Containment checks; not positive exchange group-completeness qualification."""

import ast
from pathlib import Path

from test_paper_seal_blockers import fill_trade, processor, submit_first


def test_no_production_callsite_can_reach_private_ordered_model_kernel():
    root = Path(__file__).resolve().parents[1] / "src"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                assert name != "_process_ordered_model_trades", str(path)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                # Disallow reflective production lookup of the private method too.
                assert node.value != "_process_ordered_model_trades", str(path)


def test_unsupported_group_contract_is_explicit_and_zero_consumption(tmp_path):
    paper = processor(tmp_path)
    submit_first(paper)
    before = next(iter(paper.ledger.orders.values())).as_dict()
    assert paper.process_trades([fill_trade()]) == ()
    assert next(iter(paper.ledger.orders.values())).as_dict() == before
    assert "UNSUPPORTED_GROUP_COMPLETENESS" in (tmp_path / "paper.jsonl").read_text()
    assert paper.status()["paper_score_eligible"] is False
