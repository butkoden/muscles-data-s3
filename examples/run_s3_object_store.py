from __future__ import annotations

from dataclasses import asdict
from typing import Any

from muscles_data import DataCapability
from muscles_data.catalog import DataAdapterCatalog
from muscles_data.config import DataConfig
from muscles_data.ports import ObjectStorePort
from muscles_data.runtime import DataRuntime
from muscles_data_s3 import S3ObjectStoreFactory


class FakeBody:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def read(self) -> bytes:
        return self.content


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}

    def put_object(self, **kwargs):
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = {
            "Body": bytes(kwargs["Body"]),
            "ContentType": kwargs.get("ContentType"),
            "Metadata": dict(kwargs.get("Metadata") or {}),
        }
        return {"ETag": '"demo"'}

    def get_object(self, **kwargs):
        item = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        return {
            "Body": FakeBody(item["Body"]),
            "ContentType": item.get("ContentType"),
            "Metadata": dict(item.get("Metadata") or {}),
        }

    def list_objects_v2(self, **kwargs):
        bucket = kwargs["Bucket"]
        prefix = kwargs.get("Prefix", "")
        max_keys = int(kwargs.get("MaxKeys", 100))
        contents = [
            {"Key": key, "Size": len(item["Body"])}
            for (stored_bucket, key), item in sorted(self.objects.items())
            if stored_bucket == bucket and key.startswith(prefix)
        ]
        return {"Contents": contents[:max_keys]}

    def delete_object(self, **kwargs):
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)
        return {}

    def head_bucket(self, **_kwargs):
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


def main() -> None:
    client = FakeS3Client()
    catalog = DataAdapterCatalog.with_defaults()
    catalog.register(S3ObjectStoreFactory(client_factory=lambda _config: client))
    runtime = DataRuntime(
        config=DataConfig.from_raw(
            {
                "data": {
                    "resources": {
                        "objects.docs": {
                            "type": "s3",
                            "endpoint_url": "https://s3.example",
                            "bucket": "documents",
                            "prefix": "raw",
                            "max_keys": 10,
                            "native_client": True,
                        }
                    }
                }
            }
        ),
        catalog=catalog,
    )

    objects = runtime.require_port("objects.docs", ObjectStorePort)
    put = objects.put_object("docs/readme.txt", b"hello", content_type="text/plain")
    blob = objects.get_object("docs/readme.txt")
    listed = objects.list_objects(prefix="docs")
    deleted = objects.delete_object("docs/readme.txt")
    native = runtime.require_resource("objects.docs", DataCapability.NATIVE_CLIENT).native_client()

    print("put ->", asdict(put))
    print("blob ->", {"key": blob.key, "content": blob.content.decode("utf-8"), "content_type": blob.content_type})
    print("list ->", [item.key for item in listed])
    print("delete ->", asdict(deleted))
    print("native ->", native.__class__.__name__)
    print("doctor ->", runtime.doctor())


if __name__ == "__main__":
    main()
