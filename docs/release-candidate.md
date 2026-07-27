# `muscles-data-s3` RC checklist

The package ships the S3-compatible implementation of `ObjectStorePort`.
The dependency on `muscles-data` is versioned as `>=0.1.0,<1.0.0`.

Before publishing a GitHub Release, run:

```bash
PYTHONPATH=../muscles-data/src:src python -m pytest -q
python -m build --wheel --sdist
```

The integration scenario is enabled with `MUSCLES_DATA_INTEGRATION=1` and a
running S3-compatible service configured through `S3_ENDPOINT_URL`. Object
content, URLs and credentials must stay out of diagnostics and telemetry.
Multipart and streaming operations remain an explicit native-client escape
hatch outside the MVP port contract.

The PyPI workflow publishes only after a GitHub Release is published. It uses
the versioned `muscles-data` dependency and trusted publishing.
