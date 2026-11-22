"""Visual relevance of an image to a text brief, in [0, 1].
clip = OpenAI CLIP ViT-B/32 (MIT weights) via fastembed ONNX; mock = deterministic."""
import hashlib
from pathlib import Path

from app.core.config import get_settings


class MockVision:
    name, model = "mock", "mock-vision-1"

    def score(self, text: str, image_paths: list[Path]) -> float | None:
        if not image_paths:
            return None
        h = int(hashlib.md5((text + image_paths[0].name).encode()).hexdigest()[:6], 16)
        return 0.4 + (h % 400) / 1000  # 0.4-0.8


class ClipVision:
    name = "clip"

    def __init__(self) -> None:
        from fastembed import ImageEmbedding, TextEmbedding

        s = get_settings()
        self.model = f"{s.vision_text_model} + {s.vision_image_model}"
        self._t = TextEmbedding(model_name=s.vision_text_model, cache_dir=s.model_cache_dir)
        self._i = ImageEmbedding(model_name=s.vision_image_model, cache_dir=s.model_cache_dir)

    def score(self, text: str, image_paths: list[Path]) -> float | None:
        if not image_paths:
            return None
        t = next(iter(self._t.embed([f"a photo of {text}"])))
        best = []
        for img in self._i.embed([str(p) for p in image_paths]):
            cos = float((t @ img) / ((t @ t) ** 0.5 * (img @ img) ** 0.5))
            best.append(cos)
        mean = sum(best) / len(best)
        return min(1.0, max(0.0, (mean - 0.15) / 0.17))  # CLIP B/32 cosines: ~0.15 unrelated .. ~0.32 strong


_inst = None


def get_vision():
    global _inst
    p = get_settings().vision_provider
    if p == "disabled":
        return None
    if _inst is None:
        _inst = ClipVision() if p == "clip" else MockVision()
    return _inst
