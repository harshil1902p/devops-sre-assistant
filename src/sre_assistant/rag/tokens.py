import tiktoken

# o200k_base is the tokenizer of gpt-4o / gpt-4o-mini (our Azure models in Phase 7). Local models
# tokenize differently, but one consistent counter is what matters for budgets and comparisons.
_ENC = tiktoken.get_encoding("o200k_base")


def count_tokens(text: str) -> int:
    return len(_ENC.encode(text, disallowed_special=()))


def split_tokens(text: str, size: int, overlap: int) -> list[str]:
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")
    ids = _ENC.encode(text, disallowed_special=())
    step = size - overlap
    return [_ENC.decode(ids[i : i + size]) for i in range(0, max(len(ids) - overlap, 1), step)]
