from __future__ import annotations

import importlib
import threading
from collections.abc import Callable
from dataclasses import asdict
from pathlib import PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit

from muscles_data.config import DataResourceConfig
from muscles_data.errors import DataError
from muscles_data.models import DataCapability, HealthResult, InspectResult, ObjectBlob, ObjectInfo, WriteResult


_CLIENT_UNSET = object()
_ALLOWED_OPTIONS = {
    "endpoint_url",
    "bucket",
    "region_name",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "profile_name",
    "prefix",
    "max_keys",
    "native_client",
    "timeout",
    "connect_timeout",
    "read_timeout",
    "addressing_style",
    "verify",
    "use_ssl",
}
_SECRET_MARKERS = ("password", "passwd", "secret", "token", "api_key", "apikey", "credential", "access_key")


class S3AdapterError(DataError):
    """Base error for S3 object-store adapter failures."""


class S3ConfigError(ValueError, S3AdapterError):
    """Raised when an S3 resource config cannot be mapped safely."""


class S3ClientMissingError(S3AdapterError):
    """Raised when S3 adapter is used without an available client."""


class S3ConnectionError(S3AdapterError):
    """Raised when an S3 operation cannot reach or use the backend."""


class S3ObjectStoreAdapter:
    resource_type = "s3"

    def __init__(
        self,
        config: DataResourceConfig,
        *,
        client_factory: Callable[[DataResourceConfig], Any] | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory
        self._client: Any = _CLIENT_UNSET
        self._lock = threading.RLock()
        self.closed = False

    def put_object(
        self,
        key: str,
        content: bytes,
        content_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> WriteResult:
        options = dict(options or {})
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket_name(),
            "Key": self._storage_key(key),
            "Body": bytes(content),
        }
        if content_type:
            kwargs["ContentType"] = content_type
        normalized_metadata = _normalize_metadata(metadata)
        if normalized_metadata:
            kwargs["Metadata"] = normalized_metadata
        for option_key, target_key in {
            "cache_control": "CacheControl",
            "content_disposition": "ContentDisposition",
            "content_encoding": "ContentEncoding",
        }.items():
            if option_key in options:
                kwargs[target_key] = options[option_key]

        try:
            self._client_instance().put_object(**kwargs)
        except (S3ClientMissingError, S3ConfigError):
            raise
        except Exception as exc:
            raise S3ConnectionError(self._safe_error(exc)) from exc
        return WriteResult(written=1, matched=1)

    def get_object(self, key: str, options: Mapping[str, Any] | None = None) -> ObjectBlob:
        del options
        normalized = _normalize_object_key(key)
        try:
            response = self._client_instance().get_object(Bucket=self.bucket_name(), Key=self._storage_key(normalized))
            body = response.get("Body")
            content = body.read() if hasattr(body, "read") else body
        except (S3ClientMissingError, S3ConfigError):
            raise
        except Exception as exc:
            raise S3ConnectionError(self._safe_error(exc)) from exc
        return ObjectBlob(
            key=normalized,
            content=bytes(content or b""),
            content_type=response.get("ContentType"),
            metadata=dict(response.get("Metadata") or {}),
        )

    def list_objects(
        self,
        prefix: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> list[ObjectInfo]:
        del options
        bounded_limit = self._bounded_limit(limit)
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket_name(),
            "MaxKeys": bounded_limit,
        }
        storage_prefix = self._storage_prefix(prefix)
        if storage_prefix:
            kwargs["Prefix"] = storage_prefix
        if cursor:
            kwargs["ContinuationToken"] = str(cursor)
        try:
            response = self._client_instance().list_objects_v2(**kwargs)
        except (S3ClientMissingError, S3ConfigError):
            raise
        except Exception as exc:
            raise S3ConnectionError(self._safe_error(exc)) from exc

        output: list[ObjectInfo] = []
        for item in response.get("Contents", []) or []:
            key = self._public_key(str(item.get("Key", "")))
            output.append(
                ObjectInfo(
                    key=key,
                    size=int(item.get("Size", 0) or 0),
                    metadata=_object_info_metadata(item),
                )
            )
        return output

    def delete_object(self, key: str, options: Mapping[str, Any] | None = None) -> WriteResult:
        del options
        try:
            self._client_instance().delete_object(Bucket=self.bucket_name(), Key=self._storage_key(key))
        except (S3ClientMissingError, S3ConfigError):
            raise
        except Exception as exc:
            raise S3ConnectionError(self._safe_error(exc)) from exc
        return WriteResult(deleted=1, matched=1)

    def inspect(self) -> dict[str, Any]:
        return asdict(
            InspectResult(
                name=self.config.name,
                type=self.config.type,
                capabilities=[],
                initialized=self._client is not _CLIENT_UNSET,
                status="ok",
                options=self.config.safe_options(),
                details={
                    "backend": "s3",
                    "bucket": self.bucket_name(),
                    "prefix": self.prefix(),
                    "max_keys": self.max_keys(),
                },
            )
        )

    def health(self) -> HealthResult:
        try:
            bucket = self.bucket_name()
            self._client_instance().head_bucket(Bucket=bucket)
        except Exception as exc:
            return HealthResult(status="failed", message=self._safe_error(exc), details=self._safe_details())
        return HealthResult(status="ok", message="S3 bucket is available", details=self._safe_details())

    def close(self) -> None:
        if self._client is _CLIENT_UNSET:
            self.closed = True
            return
        close = getattr(self._client, "close", None)
        if callable(close):
            close()
        self.closed = True

    def native_client(self):
        return self._client_instance()

    def bucket_name(self) -> str:
        bucket = str(self.config.options.get("bucket", "")).strip()
        if not bucket:
            raise S3ConfigError("S3 resource requires bucket")
        return bucket

    def prefix(self) -> str | None:
        return _normalize_object_prefix(self.config.options.get("prefix"))

    def max_keys(self) -> int:
        value = int(self.config.options.get("max_keys", 1000))
        if value <= 0:
            raise S3ConfigError("S3 max_keys must be positive")
        return value

    def _bounded_limit(self, limit: int) -> int:
        return min(max(0, int(limit)), self.max_keys())

    def _storage_key(self, key: str) -> str:
        normalized = _normalize_object_key(key)
        prefix = self.prefix()
        return f"{prefix}{normalized}" if prefix else normalized

    def _storage_prefix(self, prefix: str | None) -> str | None:
        configured = self.prefix() or ""
        requested = _normalize_object_prefix(prefix)
        if requested is None:
            return configured or None
        return f"{configured}{requested}"

    def _public_key(self, storage_key: str) -> str:
        prefix = self.prefix()
        if prefix and storage_key.startswith(prefix):
            return storage_key[len(prefix) :]
        return storage_key

    def _client_instance(self):
        if self._client is _CLIENT_UNSET:
            with self._lock:
                if self._client is _CLIENT_UNSET:
                    self._validate_options()
                    client = self._client_factory(self.config) if self._client_factory else _default_s3_client(self.config)
                    if client is None:
                        raise S3ClientMissingError("S3 client is not available")
                    self._client = client
        return self._client

    def _validate_options(self) -> None:
        unknown = sorted(set(self.config.options) - _ALLOWED_OPTIONS)
        if unknown:
            names = ", ".join(unknown)
            raise S3ConfigError(f"Unsupported S3 resource options: {names}")
        self.bucket_name()
        self.prefix()
        self.max_keys()

    def _safe_details(self) -> dict[str, Any]:
        details: dict[str, Any] = {"backend": "s3"}
        try:
            details["bucket"] = self.bucket_name()
        except S3ConfigError:
            pass
        try:
            details["prefix"] = self.prefix()
        except S3ConfigError:
            pass
        return details

    def _safe_error(self, exc: Exception) -> str:
        message = str(exc)
        sensitive_values = set()
        for key, value in self.config.options.items():
            if any(marker in str(key).lower() for marker in _SECRET_MARKERS) and value:
                sensitive_values.add(str(value))
        endpoint_url = str(self.config.options.get("endpoint_url", ""))
        if endpoint_url:
            sensitive_values.add(endpoint_url)
        try:
            parsed = urlsplit(endpoint_url)
            if parsed.username:
                sensitive_values.add(parsed.username)
            if parsed.password:
                sensitive_values.add(parsed.password)
        except Exception:  # pragma: no cover
            pass
        for value in sorted((item for item in sensitive_values if item), key=len, reverse=True):
            message = message.replace(value, "***")
        return message


class S3ObjectStoreFactory:
    resource_type = "s3"

    def __init__(self, *, client_factory: Callable[[DataResourceConfig], Any] | None = None) -> None:
        self._client_factory = client_factory

    def capabilities(self, config: DataResourceConfig) -> set[DataCapability]:
        native = {DataCapability.NATIVE_CLIENT} if bool(config.options.get("native_client")) else set()
        return {DataCapability.OBJECT_STORE, DataCapability.HEALTHCHECK} | native

    def create(self, config: DataResourceConfig) -> S3ObjectStoreAdapter:
        return S3ObjectStoreAdapter(config, client_factory=self._client_factory)


def _default_s3_client(config: DataResourceConfig):
    try:
        boto3 = importlib.import_module("boto3")
    except ImportError as exc:
        raise S3ClientMissingError("boto3 package is not installed") from exc

    kwargs: dict[str, Any] = {}
    for option_key in (
        "endpoint_url",
        "region_name",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "verify",
        "use_ssl",
    ):
        if option_key in config.options:
            kwargs[option_key] = config.options[option_key]

    client_config = _botocore_config(config)
    if client_config is not None:
        kwargs["config"] = client_config

    profile_name = config.options.get("profile_name")
    if profile_name:
        session_kwargs: dict[str, Any] = {"profile_name": profile_name}
        if "region_name" in kwargs:
            session_kwargs["region_name"] = kwargs.pop("region_name")
        session = boto3.session.Session(**session_kwargs)
        return session.client("s3", **kwargs)
    return boto3.client("s3", **kwargs)


def _botocore_config(config: DataResourceConfig):
    config_kwargs: dict[str, Any] = {}
    if "timeout" in config.options:
        timeout = float(config.options["timeout"])
        config_kwargs["connect_timeout"] = timeout
        config_kwargs["read_timeout"] = timeout
    if "connect_timeout" in config.options:
        config_kwargs["connect_timeout"] = float(config.options["connect_timeout"])
    if "read_timeout" in config.options:
        config_kwargs["read_timeout"] = float(config.options["read_timeout"])
    if "addressing_style" in config.options:
        config_kwargs["s3"] = {"addressing_style": str(config.options["addressing_style"])}
    if not config_kwargs:
        return None
    try:
        config_module = importlib.import_module("botocore.config")
    except ImportError as exc:
        raise S3ClientMissingError("botocore package is not installed") from exc
    return config_module.Config(**config_kwargs)


def _normalize_object_key(key: str) -> str:
    path = PurePosixPath(str(key))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise S3ConfigError(f"unsafe object key: {key}")
    return path.as_posix()


def _normalize_object_prefix(prefix: Any) -> str | None:
    if prefix is None:
        return None
    cleaned = str(prefix).strip("/")
    if not cleaned:
        return None
    return _normalize_object_key(cleaned) + "/"


def _normalize_metadata(metadata: Mapping[str, Any] | None) -> dict[str, str]:
    return {str(key): str(value) for key, value in dict(metadata or {}).items()}


def _object_info_metadata(item: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for source_key, target_key in {
        "ETag": "etag",
        "LastModified": "last_modified",
        "StorageClass": "storage_class",
    }.items():
        if source_key in item:
            metadata[target_key] = item[source_key]
    return metadata
