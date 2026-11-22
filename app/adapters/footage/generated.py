"""Second footage source: AI-generated still -> slow pan/zoom motion clip.

Always flagged is_ai_generated=true. Never used for scenes naming a real person
or brand (no likenesses, no invented logos). Prompts forbid faces, text and logos,
and the face detector rejects any image that still contains a face."""
import hashlib
from pathlib import Path

from app.adapters.footage.base import ProviderError, injected_fault
from app.core.config import get_settings
from app.schemas.footage import Attribution, FootageCandidate, Licence
from app.utils.ffmpeg import run_ffmpeg

SAFETY_SUFFIX = ("Photorealistic stock-footage style still, natural light. No text, no captions, no logos, "
                 "no brand names, no watermarks, no recognisable or close-up human faces.")


def build_prompt(concept: str, style: str, avoid: list[str]) -> str:
    av = f" Avoid: {', '.join(avoid)}." if avoid else ""
    return f"{concept}. Style: {style}.{av} {SAFETY_SUFFIX}"


class MockImageBackend:
    name, model = "mock", "mock-image-1"

    def generate(self, prompt: str, aspect: str, dest: Path) -> Path:
        h = int(hashlib.md5(prompt.encode()).hexdigest()[:8], 16)
        w, ht = (1920, 1080) if aspect == "16:9" else (1080, 1920) if aspect == "9:16" else (1440, 1440)
        sx, sy = -0.75 + (h % 100) / 400, 0.1 + (h % 37) / 300
        run_ffmpeg(["-f", "lavfi", "-i", f"mandelbrot=s={w}x{ht}:start_x={sx}:start_y={sy}:start_scale=0.3:maxiter=250",
                    "-frames:v", "1", "-vf", f"hue=h={h % 360}", str(dest)])
        return dest


class GeminiImageBackend:
    name = "gemini"

    def __init__(self) -> None:
        from app.adapters.gemini_client import require_model

        self.model = require_model(get_settings().gemini_image_model, "GEMINI_IMAGE_MODEL")

    def generate(self, prompt: str, aspect: str, dest: Path) -> Path:
        from google.genai import types

        from app.adapters.gemini_client import gemini_client

        cfg = {"response_modalities": ["IMAGE"]}
        try:
            cfg["image_config"] = types.ImageConfig(aspect_ratio=aspect)
        except Exception:  # noqa: BLE001 - older SDKs lack ImageConfig; prompt carries the ratio
            prompt = f"{prompt} Aspect ratio {aspect}."
        resp = gemini_client().models.generate_content(model=self.model, contents=prompt,
                                                       config=types.GenerateContentConfig(**cfg))
        for part in resp.candidates[0].content.parts:
            if getattr(part, "inline_data", None) and part.inline_data.data:
                dest.write_bytes(part.inline_data.data)
                return dest
        raise RuntimeError("no image in response")


def motion_clip(image: Path, out: Path, w: int, h: int, duration_s: float, seed: int, fps: int = 30) -> Path:
    """Ken Burns: slow zoom with a gentle drift. Upscaled first so motion is sub-pixel smooth."""
    frames = max(1, int(round(duration_s * fps)))
    direction = seed % 4
    zoom = "min(1.0+0.0009*on,1.25)" if direction % 2 == 0 else "max(1.25-0.0009*on,1.0)"
    x = "iw/2-(iw/zoom/2)" if direction < 2 else "(iw-iw/zoom)*on/{f}".format(f=frames)
    vf = (f"scale={w * 3}:{h * 3}:force_original_aspect_ratio=increase,crop={w * 3}:{h * 3},"
          f"zoompan=z='{zoom}':x='{x}':y='ih/2-(ih/zoom/2)':d={frames}:s={w}x{h}:fps={fps},format=yuv420p")
    run_ffmpeg(["-loop", "1", "-i", str(image), "-vf", vf, "-frames:v", str(frames), "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "18", "-an", str(out)])
    return out


class GeneratedProvider:
    name = "generated"

    def __init__(self) -> None:
        p = get_settings().generated_provider
        self.backend = GeminiImageBackend() if p == "gemini" else MockImageBackend()

    def generate(self, scene_id: str, concept: str, style: str, avoid: list[str], aspect: str,
                 duration_s: float, work: Path, seed: int) -> tuple[FootageCandidate, Path, Path]:
        fault = injected_fault(self.name)
        if fault in ("down", "slow", "empty"):
            raise ProviderError(self.name, f"injected fault: {fault}")
        prompt = build_prompt(concept, style, avoid)
        img = work / f"{scene_id}_gen.png"
        try:
            self.backend.generate(prompt, aspect, img)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(self.name, f"image generation failed: {type(exc).__name__}") from exc
        w, h = (1920, 1080) if aspect == "16:9" else (1080, 1920) if aspect == "9:16" else (1080, 1080)
        clip = motion_clip(img, work / f"{scene_id}_gen.mp4", w, h, duration_s + 0.5, seed)
        aid = hashlib.sha256(img.read_bytes()).hexdigest()[:16]
        is_mock = self.backend.name == "mock"
        cand = FootageCandidate(
            provider="generated", kind="generated", asset_id=f"gen-{aid}", title=concept[:120], tags=[],
            description=prompt, source_url=f"generated://{self.backend.name}/{aid}", download_url=None,
            width=w, height=h, duration_s=duration_s + 0.5, fps=30,
            licence=Licence(
                name="AI-generated by us (mock backend, owned)" if is_mock else "AI-generated via Gemini API (output owned by user per Gemini API terms)",
                url="https://example.invalid/mock-licence" if is_mock else "https://ai.google.dev/gemini-api/terms",
                commercial_use_allowed=True, attribution_required=False, evidence_status="ai_generated_provider_terms",
                restrictions=[] if is_mock else ["Contains Google SynthID invisible provenance mark (not a visible watermark)"],
            ),
            attribution=Attribution(required=False, text=f"AI-generated image ({self.backend.name}/{getattr(self.backend, 'model', '')}), animated by IncuBrix B-roll Engine"),
            is_ai_generated=True, watermarked=False,
            extra={"prompt": prompt, "image_model": getattr(self.backend, "model", ""), "backend": self.backend.name},
        )
        return cand, clip, img
