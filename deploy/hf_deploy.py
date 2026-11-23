"""Deploy (or update) the free Hugging Face Space demo.

    HF_TOKEN=hf_... uv run --with huggingface_hub python deploy/hf_deploy.py \
        --space <hf-username>/incubrix-broll-demo --deploy-key /path/to/github_deploy_key

Reads provider settings and keys from .env (never printed). Secrets go to Space *secrets*,
everything else to Space *variables*. Re-running updates settings and triggers a rebuild.
"""
import argparse
import os
import secrets
import sys
import time
from pathlib import Path

from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parents[1]
SECRET_KEYS = {"GEMINI_API_KEY", "PIXABAY_API_KEY", "CALLBACK_HMAC_SECRET", "S3_SECRET_KEY", "DEMO_ACCESS_TOKEN"}
# Infrastructure is fixed by the single-container image; never forward these from a compose .env
SKIP = {"DATABASE_URL", "REDIS_URL", "JOB_RUNNER", "STORAGE_BACKEND", "LOCAL_STORAGE_DIR", "WORK_DIR",
        "MODEL_CACHE_DIR", "FAULT_INJECTION_FILE", "PUBLIC_BASE_URL", "GIT_SHA", "S3_ENDPOINT",
        "S3_PUBLIC_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET", "S3_REGION", "FFMPEG_BIN", "FFPROBE_BIN"}
OVERRIDES = {"MAX_CONCURRENT_JOBS": "2", "RATE_LIMIT_PER_MINUTE": "10", "APP_ENV": "demo"}


def read_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--space", required=True, help="<hf-username>/<space-name>")
    ap.add_argument("--deploy-key", required=True, type=Path, help="private half of the read-only GitHub deploy key")
    ap.add_argument("--env-file", default=ROOT / ".env", type=Path)
    ap.add_argument("--token-file", default=ROOT / "deploy" / ".demo_token", type=Path,
                    help="where the demo access token is kept (git-ignored); reused on re-deploys")
    args = ap.parse_args()

    api = HfApi(token=os.environ["HF_TOKEN"])
    env = read_env(args.env_file)
    if args.token_file.exists():
        demo_token = args.token_file.read_text().strip()
    else:
        demo_token = secrets.token_hex(16)
        args.token_file.write_text(demo_token + "\n")
        args.token_file.chmod(0o600)

    api.create_repo(args.space, repo_type="space", space_sdk="docker", private=False, exist_ok=True)
    host = f"https://{args.space.replace('/', '-').replace('_', '-').replace('.', '-').lower()}.hf.space"

    sec = {k: v for k, v in env.items() if k in SECRET_KEYS and v and k not in SKIP}
    sec["DEMO_ACCESS_TOKEN"] = demo_token
    sec["GITHUB_DEPLOY_KEY"] = args.deploy_key.read_text()
    for k, v in sec.items():
        api.add_space_secret(args.space, k, v)
    var = {k: v for k, v in env.items() if k not in SECRET_KEYS and k not in SKIP and v}
    var.update(OVERRIDES)
    var["PUBLIC_BASE_URL"] = host
    for k, v in var.items():
        api.add_space_variable(args.space, k, v)
    print(f"set {len(sec)} secrets, {len(var)} variables")

    api.upload_folder(folder_path=str(ROOT / "deploy" / "hf-space"), repo_id=args.space, repo_type="space",
                      commit_message="deploy")
    api.restart_space(args.space, factory_reboot=True)  # rebuild so the image clones the latest main
    print("building... (first build takes ~10-15 min)")
    last = None
    for _ in range(180):
        stage = api.get_space_runtime(args.space).stage
        if stage != last:
            print("  stage:", stage)
            last = stage
        if stage == "RUNNING":
            print(f"\nDemo:  {host}/\nDocs:  {host}/docs\nToken: saved in {args.token_file}")
            return 0
        if stage in {"BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR"}:
            print(f"failed: see https://huggingface.co/spaces/{args.space}?logs=build")
            return 1
        time.sleep(10)
    print("still building; check the Space page")
    return 1


if __name__ == "__main__":
    sys.exit(main())
