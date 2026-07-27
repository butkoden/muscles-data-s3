from __future__ import annotations

import os
from uuid import uuid4

import pytest
from muscles_data.catalog import DataAdapterCatalog
from muscles_data.config import DataConfig
from muscles_data.models import DataCapability
from muscles_data.ports import ObjectStorePort
from muscles_data.runtime import DataRuntime

from muscles_data_s3 import S3ObjectStoreFactory


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("MUSCLES_DATA_INTEGRATION"), reason="backend integration is disabled"),
]


def test_s3_real_object_lifecycle_against_minio():
    bucket = f"muscles-data-it-{uuid4().hex[:12]}"
    config = DataConfig.from_raw(
        {
            "data": {
                "resources": {
                    "objects.s3": {
                        "type": "s3",
                        "endpoint_url": os.environ["S3_ENDPOINT_URL"],
                        "bucket": bucket,
                        "region_name": "us-east-1",
                        "aws_access_key_id": "minioadmin",
                        "aws_secret_access_key": "minioadmin",
                        "addressing_style": "path",
                        "native_client": True,
                    }
                }
            }
        }
    )
    catalog = DataAdapterCatalog.with_defaults()
    catalog.register(S3ObjectStoreFactory())
    runtime = DataRuntime(config=config, catalog=catalog)

    client = None
    try:
        client = runtime.require_resource("objects.s3", DataCapability.NATIVE_CLIENT).native_client()
        client.create_bucket(Bucket=bucket)
        store = runtime.require_port("objects.s3", ObjectStorePort)
        contracts = pytest.importorskip("muscles_data.contracts")
        contract = getattr(contracts, "assert_object_store_contract", None)
        if contract is not None:
            contract(lambda: store)
        assert runtime.doctor()["status"] == "ok"
    finally:
        try:
            if client is not None:
                objects = client.list_objects_v2(Bucket=bucket).get("Contents", [])
                for item in objects:
                    client.delete_object(Bucket=bucket, Key=item["Key"])
                client.delete_bucket(Bucket=bucket)
        finally:
            runtime.close()
