"""Approximate model prices for cost tracking (USD per 1M tokens). Update from the Azure pricing page;
these are estimates for reporting, not billing. Unknown models cost 0 (e.g. local Ollama)."""

PRICES_PER_1M: dict[str, tuple[float, float]] = {
    # deployment/model name prefix: (input, output)
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4o": (2.50, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price = next((p for name, p in sorted(PRICES_PER_1M.items(), key=lambda kv: -len(kv[0])) if model.startswith(name)),
                 (0.0, 0.0))  # fmt: skip
    return round(input_tokens / 1e6 * price[0] + output_tokens / 1e6 * price[1], 6)
