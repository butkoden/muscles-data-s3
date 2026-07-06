# muscles-data-s3

S3-compatible adapter package for `muscles-data`.

This package is intentionally separate from `muscles-data`: the core package
owns typed ports and runtime, while this package owns the boto3-backed
`ObjectStorePort` adapter.

## Install

```bash
python -m pip install muscles-data-s3
```

For local framework development:

```bash
PYTHONPATH=../muscles-data/src:src python3 -m pytest -q
```

## Configuration

```yaml
data:
  resources:
    objects.docs:
      type: s3
      endpoint_url: ${S3_ENDPOINT}
      bucket: documents
      region_name: us-east-1
      prefix: raw
      max_keys: 100
```

The adapter also supports standard boto3 credential options:

- `aws_access_key_id`;
- `aws_secret_access_key`;
- `aws_session_token`;
- `profile_name`;
- the default boto3 environment/provider chain.

## Usage

Register the external factory in the project composition root:

```python
from muscles_data.catalog import DataAdapterCatalog
from muscles_data.config import DataConfig
from muscles_data.ports import ObjectStorePort
from muscles_data.runtime import DataRuntime
from muscles_data_s3 import S3ObjectStoreFactory

catalog = DataAdapterCatalog.with_defaults()
catalog.register(S3ObjectStoreFactory())

runtime = DataRuntime(config=DataConfig.from_raw(config), catalog=catalog)
objects = runtime.require_port("objects.docs", ObjectStorePort)
```

Then use the narrow port:

```python
objects.put_object("docs/readme.txt", b"hello", content_type="text/plain")
blob = objects.get_object("docs/readme.txt")
items = objects.list_objects(prefix="docs", limit=20)
objects.delete_object("docs/readme.txt")
```

If `prefix: raw` is configured, the public key `docs/readme.txt` is stored as
`raw/docs/readme.txt` in S3 and returned to port consumers without the configured
prefix. This keeps application code independent from bucket layout details.

## Capabilities

`S3ObjectStoreFactory` provides:

- `object_store`;
- `healthcheck`;
- `native_client` only when the resource declares `native_client: true`.

Native access is an advanced project escape hatch:

```python
from muscles_data import DataCapability

client = runtime.require_resource("objects.docs", DataCapability.NATIVE_CLIENT).native_client()
```

Use the native client for backend-specific operations such as presigned URLs,
multipart upload configuration, bucket policies or lifecycle rules. Keep normal
blob reads and writes on `ObjectStorePort`.

## Boundaries

This package owns:

- lazy boto3 client creation;
- S3-compatible endpoint and bucket binding;
- object put/get/list/delete;
- key normalization and optional prefix mapping;
- safe `inspect()` and `doctor()`.

It does not own:

- document parsing;
- application storage schemas;
- lifecycle policies;
- multipart/streaming abstractions in the MVP;
- cross-resource transactions.

`data.doctor` performs `head_bucket`. `data.resources.list` and package
initialization do not open an S3 connection.
