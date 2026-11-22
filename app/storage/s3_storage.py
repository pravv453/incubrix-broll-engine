"""Object storage (Integration Standard #7). S3/MinIO in compose; a local
directory backend exists only so the no-dependency test command can run."""
import hashlib
import mimetypes
import shutil
from pathlib import Path
from typing import Protocol

from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode


class Storage(Protocol):
    def put_file(self, key: str, path: str | Path, content_type: str | None = None) -> str: ...
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str: ...
    def get_file(self, key: str, dest: str | Path) -> Path: ...
    def get_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def url(self, key: str) -> str: ...


def _ctype(key: str, given: str | None) -> str:
    return given or mimetypes.guess_type(key)[0] or "application/octet-stream"


class S3Storage:
    def __init__(self, s: Settings):
        import boto3
        from botocore.config import Config

        cfg = Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}, s3={"addressing_style": "path"})
        kw = dict(aws_access_key_id=s.s3_access_key, aws_secret_access_key=s.s3_secret_key, region_name=s.s3_region, config=cfg)
        self.bucket = s.s3_bucket
        self.expiry = s.s3_presign_expiry_s
        self.client = boto3.client("s3", endpoint_url=s.s3_endpoint, **kw)
        # presigned URLs must be signed for the host the *client* will call
        self.public = boto3.client("s3", endpoint_url=s.s3_public_endpoint or s.s3_endpoint, **kw)
        self._bucket_ready = False

    def ensure_bucket(self) -> None:
        """Create the bucket if missing (replaces a separate init container)."""
        if self._bucket_ready:
            return
        from botocore.exceptions import ClientError

        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            try:
                self.client.create_bucket(Bucket=self.bucket)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                    raise
        self._bucket_ready = True

    def _wrap(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as exc:  # noqa: BLE001
            from botocore.exceptions import ClientError

            if isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                raise FileNotFoundError(str(exc)) from exc
            raise AppError(ErrorCode.STORAGE_UNAVAILABLE, details={"reason": type(exc).__name__}) from exc

    def put_file(self, key, path, content_type=None):
        self._wrap(self.client.upload_file, str(path), self.bucket, key, ExtraArgs={"ContentType": _ctype(key, content_type)})
        return key

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self._wrap(self.client.put_object, Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return key

    def get_file(self, key, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        self._wrap(self.client.download_file, self.bucket, key, str(dest))
        return Path(dest)

    def get_bytes(self, key):
        return self._wrap(self.client.get_object, Bucket=self.bucket, Key=key)["Body"].read()

    def exists(self, key):
        try:
            self._wrap(self.client.head_object, Bucket=self.bucket, Key=key)
            return True
        except FileNotFoundError:
            return False

    def url(self, key):
        return self.public.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=self.expiry)


class LocalStorage:
    """Filesystem backend; links are served by the API at /api/v1/files/{key}."""

    def __init__(self, s: Settings):
        self.root = Path(s.local_storage_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise AppError(ErrorCode.INVALID_REQUEST, message="invalid storage key")
        return p

    def put_file(self, key, path, content_type=None):
        dst = self._p(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".part")
        shutil.copyfile(path, tmp)
        tmp.replace(dst)  # atomic publish
        return key

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        dst = self._p(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dst)
        return key

    def get_file(self, key, dest):
        src = self._p(key)
        if not src.exists():
            raise FileNotFoundError(key)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return Path(dest)

    def get_bytes(self, key):
        src = self._p(key)
        if not src.exists():
            raise FileNotFoundError(key)
        return src.read_bytes()

    def exists(self, key):
        return self._p(key).exists()

    def url(self, key):
        return f"{get_settings().public_base_url.rstrip('/')}/api/v1/files/{key}"

    def path(self, key: str) -> Path:
        return self._p(key)


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        s = get_settings()
        _storage = S3Storage(s) if s.storage_backend == "s3" else LocalStorage(s)
        if isinstance(_storage, S3Storage):
            _storage._wrap(_storage.ensure_bucket)
    return _storage


def reset_storage() -> None:
    global _storage
    _storage = None


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
