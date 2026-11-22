"""Write openapi.yaml from the live FastAPI app (tests assert they stay identical)."""
import os
import sys
from pathlib import Path

os.environ.setdefault("STORAGE_BACKEND", "local")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from app.main import create_app  # noqa: E402

spec = create_app(readiness_checks={}).openapi()
out = Path(__file__).resolve().parents[1] / "openapi.yaml"
out.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=120))
print(f"wrote {out} (OpenAPI {spec['openapi']}, {len(spec['paths'])} paths)")
