# Release Readiness Notes

Bucket Commander keeps credentials outside application configuration and cache files. Use provider
profiles, environment variables, or provider-native credential stores for secrets. The application
configuration may contain non-secret routing metadata such as profile names, regions, endpoint URLs,
cache paths, UI preferences, and operation defaults.

## Cache Sensitivity

The cache directory stores bucket names, prefixes, object names, sizes, timestamps, ETags, and other
non-secret object metadata in Parquet files plus `manifest.json`. Treat the cache as sensitive if
object names or prefixes reveal private information.

The cache must not contain access keys, secret keys, session tokens, passwords, or provider SDK
credential files. Configuration loaders reject common credential field names before settings are
accepted.

## CI and Release Package Checks

Pushes and pull requests run Ruff, mypy, and pytest on Python 3.11 and 3.12 using the locked uv
dependencies. The MinIO integration test remains opt-in through `BC_MINIO_ENDPOINT`.

Pushing a tag named `v<version>` (for example, `v0.1.0`) also runs release package checks after CI
passes. The tag must match `project.version` in `pyproject.toml`. The workflow builds a wheel and
source distribution, installs each in an isolated environment, and checks the CLI version and help.
Tested packages are saved as the `release-packages` workflow artifact. Publishing remains manual.

## Damaged Index Recovery

Damaged or incompatible index files should not block live bucket browsing. If a panel cache cannot be
loaded, Bucket Commander ignores that cache for the current refresh and loads the live bucket prefix.
Indexed search returns an explanatory damaged-index result instead of raising into the UI.

Useful recovery options:

- Ignore the damaged cache and keep browsing live.
- Refresh the current prefix to rewrite direct-prefix cache rows.
- Rebuild the bucket with recursive indexing.
- Delete only the affected cache scope under the configured cache root when manual cleanup is needed.

## Compaction

The Parquet index is append-only during normal browsing and indexing. Compaction writes new active
Parquet files containing the newest row per object or prefix identity, removes object delete markers
from the active view, and atomically updates the manifest to point at the compacted files. Older
Parquet parts are left inactive for now and may be removed by a later retention pass.

## Known Limitations

- Durable jobs are visible in the monitor, but normal UI copy, move, and delete still use the
  short-lived task manager path.
- Recovery prompts are represented as status messages and documented actions; a full interactive
  rebuild prompt is still future work.
- Provider permission and expired-credential errors are surfaced through backend errors. Exact wording
  depends on the provider SDK response.
- Very large directories rely on provider pagination and local terminal rendering limits; the UI keeps
  selection stable, but it does not yet virtualize rows beyond Urwid list rendering.
