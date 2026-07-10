# Bucket Commander

Bucket Commander is a two-panel terminal file manager for local files and
S3-compatible object-storage buckets.

It is inspired by commander-style file managers such as Midnight Commander, but
it treats object storage as its own domain: bucket listings can be cached in
Parquet, indexed searches can run against local metadata, and large or
destructive operations are modeled as planned work rather than one-off shell
commands.

## Status

Bucket Commander is early open-source software. The core package, local backend,
S3 backend, Parquet index, indexed search, task plumbing, and durable job models
are implemented and covered by tests, but the user experience is still being
hardened for broad daily use.

Use it carefully with important buckets. Prefer read-only credentials while
evaluating the tool.

## Features

- Two-panel terminal interface for local paths and `s3://` locations.
- Local browsing, file preview, copy, move, delete, rename, and directory
  creation.
- S3-compatible bucket browsing with profile, region, and endpoint support.
- Parquet-backed bucket metadata cache for previously visited prefixes.
- Indexed bucket search using cached metadata.
- Recursive bucket indexing with checkpoint-oriented internals.
- Task progress for long-running UI operations.
- Durable job planning and storage for larger copy, move, delete, and transfer
  workflows.
- Configuration that rejects common credential field names.
- Local MinIO verification setup for development and demos.

## Why This Exists

Object storage often looks like a filesystem until it does not. Listing can be
slow or paginated, prefixes are logical views, cached metadata can be stale, and
recursive delete or move operations deserve a visible plan.

Bucket Commander aims to make that state visible while preserving the speed of a
keyboard-first two-panel workflow.

## Installation

### From Source

```bash
git clone <repository-url>
cd bucket-commander
uv sync
uv run bc --help
```

Run the app with both panels pointed at the current directory:

```bash
uv run bc --left . --right .
```

Open a local path in one panel and an S3-compatible bucket prefix in the other:

```bash
uv run bc --left . --right s3://my-bucket/logs/
```

### From PyPI

After a release is published, install with your preferred Python application
installer:

```bash
uv tool install bucket-commander
```

or:

```bash
pipx install bucket-commander
```

## Basic Controls

| Key | Action |
| --- | --- |
| `Tab` | Switch active panel |
| `Up` / `Down` | Move cursor |
| `Enter`, `Space`, `Right` | Open directory or prefix |
| `Backspace`, `Left` | Go to parent or leave search mode |
| `S` | Toggle selection |
| `R`, `Ctrl-R` | Refresh active panel |
| `/` | Search indexed bucket metadata |
| `\` | Leave indexed search |
| `O` | Cycle sort field |
| `F3` | View selected entry |
| `F4` | Create a new file |
| `F5` | Copy selected entries |
| `F6` | Move selected entries |
| `F7` | Create a new folder |
| `F8`, `Delete` | Delete selected entries |
| `I` | Index current bucket or prefix recursively |
| `J` | Show jobs |
| `G` | Show resolved settings |
| `F1`, `?` | Show help |
| `Q`, `Esc` | Quit |

## S3 Credentials

Bucket Commander does not store access keys, secret keys, session tokens,
passwords, or provider credential files in its application config or cache.

Credentials should be resolved by the AWS SDK through normal provider-native
mechanisms, such as:

- AWS profiles
- AWS SSO
- environment variables
- web identity
- instance or container roles
- compatible endpoint credentials for local services such as MinIO

Configuration files may contain non-secret routing metadata such as bucket URIs,
regions, endpoint URLs, AWS profile names, cache paths, and UI preferences.

## Configuration

Application settings default to:

```text
~/.config/bucket-commander/config.toml
```

Known source shortcuts default to:

```text
~/.config/bucket-commander/sources.toml
```

During development, Bucket Commander also discovers:

```text
config/sources.toml
```

Example known sources:

```toml
[[sources]]
name = "AWS logs"
uri = "s3://example-logs/prod/"
profile = "work-readonly"
region = "us-east-1"
credential_source = "aws-profile"

[[sources]]
name = "Local MinIO"
uri = "s3://bucket-commander/logs/"
region = "us-east-1"
endpoint_url = "http://127.0.0.1:9000"
credential_source = "environment"

[[sources]]
name = "Home"
uri = "~/"
```

See [config/sources.toml.example](config/sources.toml.example) for a copyable
template.

Useful environment overrides:

```bash
export BUCKET_COMMANDER_CONFIG=~/.config/bucket-commander/config.toml
export BUCKET_COMMANDER_CACHE_DIR=~/.cache/bucket-commander
export BUCKET_COMMANDER_S3_PROFILE=work-readonly
export BUCKET_COMMANDER_S3_ENDPOINT_URL=http://127.0.0.1:9000
export AWS_REGION=us-east-1
```

## Local MinIO Demo

Start the demo S3-compatible endpoint:

```bash
docker compose -f docker-compose.minio.yml up -d
```

Run the MinIO integration test:

```bash
AWS_ACCESS_KEY_ID=bucketcommander \
AWS_SECRET_ACCESS_KEY=bucketcommander123 \
AWS_EC2_METADATA_DISABLED=true \
BC_MINIO_ENDPOINT=http://127.0.0.1:9000 \
uv --cache-dir .uv-cache run pytest tests/test_s3_minio_integration.py
```

Open Bucket Commander against the seeded bucket:

```bash
AWS_ACCESS_KEY_ID=bucketcommander \
AWS_SECRET_ACCESS_KEY=bucketcommander123 \
AWS_EC2_METADATA_DISABLED=true \
BUCKET_COMMANDER_S3_ENDPOINT_URL=http://127.0.0.1:9000 \
uv --cache-dir .uv-cache run bc --left . --right s3://bucket-commander/logs/
```

Stop the demo endpoint:

```bash
docker compose -f docker-compose.minio.yml down
```

More details are in [docker/minio/README.md](docker/minio/README.md).

## Cache And Privacy

Bucket Commander stores bucket metadata in Parquet files plus manifests under
the configured cache root. The cache can include bucket names, prefixes, object
keys, object sizes, timestamps, ETags, and related non-secret metadata.

Treat the cache as sensitive if object names or prefixes reveal private
information. The cache must not contain provider access keys or session secrets.

## Development

Install dependencies:

```bash
uv sync
```

Run the test suite:

```bash
uv --cache-dir .uv-cache run pytest
```

Run linting:

```bash
uv --cache-dir .uv-cache run ruff check .
```

Run formatting:

```bash
uv --cache-dir .uv-cache run ruff format .
```

Run type checks:

```bash
uv --cache-dir .uv-cache run mypy src tests
```

Run the app from the source tree:

```bash
uv --cache-dir .uv-cache run bc --left . --right .
```

## Architecture

The package is organized around a small set of boundaries:

```text
src/bc/
  app.py
  ui/        terminal rendering and commands
  core/      locations, entries, panel state, task abstractions
  backends/  local filesystem, S3, and transfer integrations
  index/     manifests, Parquet storage, query, panel cache, indexing
  jobs/      durable job models, planning, queue, store, worker
  config/    settings and known source loading
```

Important dependency direction:

```text
ui -> core -> backends/index/jobs/config
```

Provider SDK details belong in `backends/`. Parquet details belong in `index/`.
Durable job state belongs in `jobs/`.

## Known Limitations

- Normal UI copy, move, and delete operations still use the short-lived task
  manager path, while durable jobs are available through the job architecture
  and monitor.
- Recovery prompts for damaged indexes are documented as actions and surfaced as
  status messages; a full interactive rebuild prompt is future work.
- Provider permission and expired-credential errors depend on the underlying SDK
  response.
- Very large directories rely on provider pagination and terminal rendering
  limits; row virtualization is not yet implemented.

See [docs/release-readiness.md](docs/release-readiness.md) for more operational
notes.

## Roadmap

The project direction is captured in:

- [SPECIFICATION.md](SPECIFICATION.md)
- [IMPLEMENTATION_PRS.md](IMPLEMENTATION_PRS.md)

Near-term priorities:

- Harden first-run documentation and packaging.
- Improve destructive-operation confirmation flows.
- Tighten damaged-index recovery UX.
- Add more integration coverage for S3-compatible endpoints.
- Add release automation and public changelog.

## Contributing

Issues and pull requests are welcome once the public repository is available.
Good first contributions include documentation fixes, additional S3-compatible
endpoint testing, UI polish for narrow terminals, and focused backend or index
tests.

Before sending changes, run the narrowest useful checks for your change. For
shared or core behavior, run tests, linting, and type checks.

## License

Bucket Commander is licensed under the Apache License, Version 2.0. See
[LICENSE](LICENSE).
