# muscles-data-s3

S3-compatible adapter package for `muscles-data`.

This package is intentionally separate from `muscles-data`: the core package
owns typed ports and runtime, while this package owns the boto3-backed
`ObjectStorePort` adapter.

## Related packages

- Core runtime and port contracts:
  [`muscles-data`](https://github.com/butkoden/muscles-data)
- Elasticsearch search adapter:
  [`muscles-data-elasticsearch`](https://github.com/butkoden/muscles-data-elasticsearch)
- OpenSearch search adapter:
  [`muscles-data-opensearch`](https://github.com/butkoden/muscles-data-opensearch)
- Redis key-value/lock/stream adapter:
  [`muscles-data-redis`](https://github.com/butkoden/muscles-data-redis)
- Qdrant vector adapter:
  [`muscles-data-qdrant`](https://github.com/butkoden/muscles-data-qdrant)
- MongoDB document-store adapter:
  [`muscles-data-mongodb`](https://github.com/butkoden/muscles-data-mongodb)
- SQLAlchemy direct SQL resource adapter:
  [`muscles-data-sqlalchemy`](https://github.com/butkoden/muscles-data-sqlalchemy)
- Executable example:
  [`example_data_s3_1`](https://github.com/butkoden/muscular-example/tree/master/example_data_s3_1)

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
      endpoint_url: !environ S3_ENDPOINT
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

Register this adapter once in the application composition root when building
the shared `DataAdapterCatalog`, alongside other configured adapters:

```python
catalog.register(S3ObjectStoreFactory())
```

The `objects.docs` resource itself is declared in the application's central
`data.resources` configuration shown below. A package resolves the port from
the existing runtime; it does not build another catalog or `DataRuntime`:

```python
from muscles.data.ports import ObjectStorePort

objects = app.resource.connect("objects.docs", ObjectStorePort)
```

Then use the narrow port:

```python
objects.put_object("docs/readme.txt", b"hello", content_type="text/plain")
blob = objects.get_object("docs/readme.txt")
items = objects.list_objects(prefix="docs", limit=20)
objects.delete_object("docs/readme.txt")
```

Backend-specific request options are available without exposing the boto3
client: `get_object()` accepts `range`, conditional headers and
version/checksum options; `list_objects()` accepts `delimiter` and continuation
helpers; and `delete_object()` accepts version and retention options.

If `prefix: raw` is configured, the public key `docs/readme.txt` is stored as
`raw/docs/readme.txt` in S3 and returned to port consumers without the configured
prefix. This keeps application code independent from bucket layout details.
