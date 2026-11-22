from pathlib import Path

from app.adapters.footage.base import ProviderError
from app.core.config import get_settings


def get_stock_providers(cache_dir: Path) -> tuple[list, list[dict]]:
    """Instantiate configured stock providers. Returns (providers, init_failures)."""
    out, failures = [], []
    for name in get_settings().footage_provider_list:
        try:
            if name == "pixabay":
                from app.adapters.footage.pixabay import PixabayProvider
                out.append(PixabayProvider(cache_dir))
            elif name == "mock":
                from app.adapters.footage.mock import MockFootageProvider
                out.append(MockFootageProvider(cache_dir / "mock"))
            else:
                failures.append({"provider": name, "reason": "unknown provider"})
        except ProviderError as exc:
            failures.append({"provider": name, "reason": exc.reason})
    return out, failures


def get_generated_provider():
    if get_settings().generated_provider == "disabled":
        return None
    from app.adapters.footage.generated import GeneratedProvider
    return GeneratedProvider()
