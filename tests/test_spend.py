import pytest
from langchain_core.messages import AIMessage

from sre_assistant.llm.spend import FALLBACK, SpendGuard, SpendLedger, SpendLimitExceeded, price
from tests.fakes import ScriptedChatModel


def test_price_uses_cached_rate_and_longest_prefix():
    # gpt-4.1-mini: 0.40 in, 0.10 cached, 1.60 out per 1M
    assert price("gpt-4.1-mini", 1_000_000, 0) == pytest.approx(0.40)
    assert price("gpt-4.1-mini", 1_000_000, 0, cached_tokens=1_000_000) == pytest.approx(0.10)
    assert price("gpt-4.1-mini-2025-04-14", 0, 1_000_000) == pytest.approx(1.60), "prefix match"
    assert price("gpt-4.1", 1_000_000, 0) == pytest.approx(2.00), "longest prefix wins over gpt-4.1-mini? no: exact"
    assert price("some-new-model", 1_000_000, 0) == pytest.approx(FALLBACK[0]), "unknown models priced pessimistically"


def test_ledger_accumulates_and_blocks_at_cap(tmp_path):
    ledger = SpendLedger(tmp_path / "spend.jsonl", limit_usd=0.001)
    ledger.check()
    ledger.record("gpt-4.1-mini", 2_000, 500)  # 0.0008 + 0.0008 = 0.0016
    assert ledger.total() == pytest.approx(0.0016)
    with pytest.raises(SpendLimitExceeded, match="cap reached"):
        ledger.check()
    assert ledger.summary()["by_model"] == {"gpt-4.1-mini": pytest.approx(0.0016)}


def test_guard_records_usage_and_refuses_calls_over_the_cap(tmp_path):
    ledger = SpendLedger(tmp_path / "spend.jsonl", limit_usd=0.0005)
    reply = AIMessage("hi", usage_metadata={"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100,
                                            "input_token_details": {"cache_read": 800}})  # fmt: skip
    model = ScriptedChatModel([reply, AIMessage("never sent")], callbacks=[SpendGuard(ledger, "gpt-4.1-mini")])
    model.invoke("first call")
    # 200 fresh*0.40 + 800 cached*0.10 + 100 out*1.60 = 0.00008+0.00008+0.00016 = 0.00032
    assert ledger.total() == pytest.approx(0.00032)
    ledger.record("gpt-4.1-mini", 1000, 0)  # push over the cap
    with pytest.raises(SpendLimitExceeded):
        model.invoke("second call")
    assert len(model.prompts) == 1, "the blocked call never reached the model"
