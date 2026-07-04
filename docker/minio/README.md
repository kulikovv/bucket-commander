# MinIO Verification

Start a local S3-compatible endpoint:

```bash
docker compose -f docker-compose.minio.yml up -d
```

Run the opt-in integration test:

```bash
AWS_ACCESS_KEY_ID=bucketcommander \
AWS_SECRET_ACCESS_KEY=bucketcommander123 \
AWS_EC2_METADATA_DISABLED=true \
BC_MINIO_ENDPOINT=http://127.0.0.1:9000 \
uv --cache-dir /private/tmp/uv-cache run pytest tests/test_s3_minio_integration.py
```

The seeded bucket is `bucket-commander`.

MinIO console: http://127.0.0.1:9001

Credentials:

- User: `bucketcommander`
- Password: `bucketcommander123`

Stop the stack:

```bash
docker compose -f docker-compose.minio.yml down
```
