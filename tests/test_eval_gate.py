import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "eval_gate", Path(__file__).resolve().parents[1] / "scripts" / "eval_gate.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def test_gate_min_max_and_missing_metrics():
    rows = gate.check({"a": 0.9, "b": 0, "c": 0.5}, {"min": {"a": 0.8, "c": 0.7, "missing": 1}, "max": {"b": 0}})
    result = {r[0]: r[4] for r in rows}
    assert result == {"a": True, "c": False, "missing": False, "b": True}
