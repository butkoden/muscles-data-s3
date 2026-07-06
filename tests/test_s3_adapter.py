from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from muscles_data.catalog import DataAdapterCatalog
from muscles_data.config import DataConfig
from muscles_data.models import DataCapability
from muscles_data.ports import ObjectStorePort
from muscles_data.runtime import DataRuntime

from muscles_data_s3 import (
    S3ClientMissingError,
    S3ConfigError,
    S3ConnectionError,
    S3ObjectStoreFactory,
)


class FakeBody:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def read(self) -> bytes:
        return self.content


class FakeS3Client:
    def __init__(self, *, fail_head: bool = False) -> None:
        self.fail_head = fail_head
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.head_bucket_calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs):
        self.put_calls.append(dict(kwargs))
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = {
            "Body": bytes(kwargs["Body"]),
            "ContentType": kwargs.get("ContentType"),
            "Metadata": dict(kwargs.get("Metadata") or {}),
        }
        return {"ETag": '"etag"'}

    def get_object(self, **kwargs):
        self.get_calls.append(dict(kwargs))
        item = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        return {
            "Body": FakeBody(item["Body"]),
            "ContentLength": len(item["Body"]),
            "ContentType": item.get("ContentType"),
            "Metadata": dict(item.get("Metadata") or {}),
        }

    def list_objects_v2(self, **kwargs):
        self.list_calls.append(dict(kwargs))
        bucket = kwargs["Bucket"]
        prefix = kwargs.get("Prefix", "")
        max_keys = int(kwargs.get("MaxKeys", 100))
        contents = []
        for (stored_bucket, key), item in sorted(self.objects.items()):
            if stored_bucket != bucket or not key.startswith(prefix):
                continue
            contents.append({"Key": key, "Size": len(item["Body"]), "ETag": '"etag"'})
        return {"Contents": contents[:max_keys], "IsTruncated": len(contents) > max_keys}

    def delete_object(self, **kwargs):
        self.delete_calls.append(dict(kwargs))
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)
        return {"DeleteMarker": True}

    def head_bucket(self, **kwargs):
        self.head_bucket_calls.append(dict(kwargs))
        if self.fail_head:
            raise TimeoutError("https://user:s3-secret@s3.example timed out")
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


def _s3_config(endpoint_url: str = "https://user:s3-secret@s3.example") -> dict[str, Any]:
    return {
        "data": {
            "resources": {
                "objects.docs": {
                    "type": "s3",
                    "endpoint_url": endpoint_url,
                    "bucket": "documents",
                    "region_name": "us-east-1",
                    "aws_access_key_id": "access-secret",
                    "aws_secret_access_key": "secret-key",
                    "prefix": "raw",
                    "max_keys": 2,
                    "native_client": True,
                }
            }
        }
    }


def _catalog(factory: S3ObjectStoreFactory) -> DataAdapterCatalog:
    catalog = DataAdapterCatalog.with_defaults()
    catalog.register(factory)
    return catalog


def _runtime(client: FakeS3Client | None):
    return DataRuntime(
        config=DataConfig.from_raw(_s3_config()),
        catalog=_catalog(S3ObjectStoreFactory(client_factory=lambda _config: client)),
    )


def test_s3_object_store_is_registered_lazy_and_maps_operations():
    client = FakeS3Client()
    runtime = _runtime(client)

    listed = runtime.list_resources()
    resource = next(item for item in listed if item["name"] == "objects.docs")
    inspected_before = runtime.inspect_resource("objects.docs")

    assert resource["type"] == "s3"
    assert "object_store" in resource["capabilities"]
    assert "native_client" not in resource["capabilities"]
    assert resource["initialized"] is False
    assert inspected_before["initialized"] is False
    assert inspected_before["options"]["endpoint_url"] == "***"
    assert inspected_before["options"]["aws_secret_access_key"] == "***"

    store = runtime.require_port("objects.docs", ObjectStorePort)
    write = store.put_object(
        "docs/readme.txt",
        b"hello",
        content_type="text/plain",
        metadata={"owner": "denis", "version": 1},
    )
    blob = store.get_object("docs/readme.txt")
    store.put_object("docs/guide.txt", b"guide")
    store.put_object("images/logo.png", b"png")
    listed_objects = store.list_objects(prefix="docs", limit=10)
    deleted = store.delete_object("docs/guide.txt")

    assert write.written == 1
    assert client.put_calls[0]["Bucket"] == "documents"
    assert client.put_calls[0]["Key"] == "raw/docs/readme.txt"
    assert client.put_calls[0]["Body"] == b"hello"
    assert client.put_calls[0]["ContentType"] == "text/plain"
    assert client.put_calls[0]["Metadata"] == {"owner": "denis", "version": "1"}
    assert blob.key == "docs/readme.txt"
    assert blob.content == b"hello"
    assert blob.content_type == "text/plain"
    assert blob.metadata == {"owner": "denis", "version": "1"}
    assert [item.key for item in listed_objects] == ["docs/guide.txt", "docs/readme.txt"]
    assert client.list_calls[-1]["Prefix"] == "raw/docs/"
    assert client.list_calls[-1]["MaxKeys"] == 2
    assert deleted.deleted == 1
    assert client.delete_calls[-1] == {"Bucket": "documents", "Key": "raw/docs/guide.txt"}

    native = runtime.require_resource("objects.docs", DataCapability.NATIVE_CLIENT).native_client()
    assert native is client
    assert client.head_bucket_calls == []


def test_s3_inspect_doctor_and_safe_failures():
    client = FakeS3Client()
    runtime = _runtime(client)

    doctor = runtime.doctor()
    checks = [check for check in doctor["checks"] if check["resource"] == "objects.docs"]

    assert checks[0]["status"] == "ok"
    assert client.head_bucket_calls == [{"Bucket": "documents"}]
    assert "s3-secret" not in repr(doctor)
    assert "secret-key" not in repr(runtime.inspect_resource("objects.docs"))

    missing_client_runtime = _runtime(None)
    with pytest.raises(S3ClientMissingError):
        missing_client_runtime.require_port("objects.docs", ObjectStorePort).get_object("docs/readme.txt")

    failing_runtime = DataRuntime(
        config=DataConfig.from_raw(_s3_config()),
        catalog=_catalog(S3ObjectStoreFactory(client_factory=lambda _config: FakeS3Client(fail_head=True))),
    )
    failing_doctor = failing_runtime.doctor()
    assert failing_doctor["status"] == "failed"
    assert [check for check in failing_doctor["checks"] if check["resource"] == "objects.docs"][0]["status"] == "failed"
    assert "s3-secret" not in repr(failing_doctor)

    bad_client = FakeS3Client()
    bad_client.get_object = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("secret-key leaked"))  # type: ignore[method-assign]
    bad_runtime = DataRuntime(
        config=DataConfig.from_raw(_s3_config()),
        catalog=_catalog(S3ObjectStoreFactory(client_factory=lambda _config: bad_client)),
    )
    with pytest.raises(S3ConnectionError) as exc_info:
        bad_runtime.require_port("objects.docs", ObjectStorePort).get_object("docs/readme.txt")
    assert "secret-key" not in str(exc_info.value)


def test_s3_resource_requires_bucket_and_rejects_unsafe_keys():
    assert DataConfig.from_raw(_s3_config()).resources["objects.docs"].type == "s3"

    adapter = S3ObjectStoreFactory(client_factory=lambda _config: FakeS3Client()).create(
        DataConfig.from_raw(
            {
                "data": {
                    "resources": {
                        "objects.docs": {
                            "type": "s3",
                            "endpoint_url": "https://s3.example",
                        }
                    }
                }
            }
        ).resources["objects.docs"]
    )
    with pytest.raises(S3ConfigError, match="requires bucket"):
        adapter.put_object("docs/readme.txt", b"hello")

    unsafe = _runtime(FakeS3Client()).require_port("objects.docs", ObjectStorePort)
    with pytest.raises(S3ConfigError, match="unsafe object key"):
        unsafe.put_object("../secret.txt", b"nope")
