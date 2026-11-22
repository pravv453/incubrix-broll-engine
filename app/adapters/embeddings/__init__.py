"""Text similarity in [0, 1]. mock = hashed bag-of-words (deterministic, lexical);
fastembed = ONNX sentence embeddings (BAAI/bge-small-en-v1.5 by default), no torch."""
import hashlib
import math
import re

from app.core.config import get_settings


def _stem(w: str) -> str:
    for suf in ("ing", "ers", "es", "ed", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


class MockEmbedder:
    name, model = "mock", "hashed-bow-512"

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * 512
        for w in re.findall(r"\w+", text.lower()):
            if len(w) < 3:
                continue
            h = int(hashlib.md5(_stem(w).encode()).hexdigest(), 16)
            v[h % 512] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def similarities(self, query: str, docs: list[str]) -> list[float]:
        q = self._vec(query)
        return [max(0.0, sum(a * b for a, b in zip(q, self._vec(d)))) for d in docs]


class FastEmbedder:
    name = "fastembed"

    def __init__(self) -> None:
        from fastembed import TextEmbedding

        s = get_settings()
        self.model = s.embedding_model
        self._m = TextEmbedding(model_name=s.embedding_model, cache_dir=s.model_cache_dir)

    def similarities(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        vecs = list(self._m.embed([query] + docs))
        q = vecs[0]
        out = []
        for d in vecs[1:]:
            cos = float((q @ d) / ((q @ q) ** 0.5 * (d @ d) ** 0.5))
            out.append(min(1.0, max(0.0, (cos - 0.55) / 0.35)))  # bge cosines cluster in 0.55-0.9
        return out


_inst = None


def get_embedder():
    global _inst
    if _inst is None:
        _inst = FastEmbedder() if get_settings().embedding_provider == "fastembed" else MockEmbedder()
    return _inst
