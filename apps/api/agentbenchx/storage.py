"""Private object storage accessed only through authenticated API routes."""

import base64
import hashlib
import os
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol


class StorageError(Exception):
    """Safe public error; never contains SDK request details or credentials."""


def logical_key(key: str) -> str:
    parts = key.split("/")
    if not key or any(p in {"", ".", ".."} for p in parts) or "\\" in key or "\x00" in key:
        raise ValueError("Invalid logical storage key")
    return str(PurePosixPath(key))


class StorageBackend(Protocol):
    def put(self, key: str, content: bytes) -> None: ...
    def open(self, key: str) -> BinaryIO: ...
    def head(self, key: str) -> dict: ...
    def delete(self, key: str) -> None: ...


class LocalStorageBackend:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def path(self, key):
        path = (self.root / logical_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Storage path escapes root")
        return path

    def put(self, key, content):
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(content)

    def open(self, key):
        return self.path(key).open("rb")

    def head(self, key):
        with self.open(key) as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            return {"ContentLength": os.fstat(stream.fileno()).st_size, "Metadata": {"sha256": digest}}

    def delete(self, key):
        # Internal maintenance operation only; no delete API or automatic cleanup.
        self.path(key).unlink(missing_ok=True)


class S3CompatibleStorageBackend:
    def __init__(self, settings, client=None):
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint_url,
                region_name=settings.s3_region,
                aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
                aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
                config=Config(
                    signature_version="s3v4",
                    connect_timeout=3,
                    read_timeout=10,
                    retries={"total_max_attempts": 2, "mode": "standard"},
                    max_pool_connections=5,
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
            )
        self.client, self.bucket = client, settings.s3_bucket

    def put(self, key, content):
        from botocore.exceptions import BotoCoreError, ClientError

        key = logical_key(key)
        digest = hashlib.sha256(content).hexdigest()
        try:
            # No public ACL or URL. Conditional write preserves immutable agent objects.
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=content,
                IfNoneMatch="*",
                Metadata={"sha256": digest},
                ContentType="application/json" if key.endswith(".json") else "text/plain; charset=utf-8",
                # R2 supports Content-MD5 for transport integrity. ETag is never a SHA-256.
                ContentMD5=base64.b64encode(hashlib.md5(content, usedforsecurity=False).digest()).decode(),
            )
            stored = self.head(key)
            if (
                stored.get("ContentLength") != len(content)
                or stored.get("Metadata", {}).get("sha256") != digest
            ):
                raise StorageError("Object storage integrity check failed")
        except FileNotFoundError:
            raise StorageError("Uploaded object unavailable") from None
        except (BotoCoreError, ClientError):
            raise StorageError("Object storage write failed") from None

    def head(self, key):
        from botocore.exceptions import BotoCoreError, ClientError

        key = logical_key(key)
        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
                raise FileNotFoundError("Object unavailable") from None
            raise StorageError("Object storage metadata read failed") from None
        except BotoCoreError:
            raise StorageError("Object storage metadata read failed") from None

    def delete(self, key):
        from botocore.exceptions import BotoCoreError, ClientError

        key = logical_key(key)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except (BotoCoreError, ClientError):
            raise StorageError("Object storage delete failed") from None

    def open(self, key):
        from botocore.exceptions import BotoCoreError, ClientError

        key = logical_key(key)
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                raise FileNotFoundError("Object unavailable") from None
            raise StorageError("Object storage read failed") from None
        except BotoCoreError:
            raise StorageError("Object storage read failed") from None


def storage_backend(settings) -> StorageBackend:
    if settings.storage_backend == "s3":
        return S3CompatibleStorageBackend(settings)
    return LocalStorageBackend(settings.storage_root)
