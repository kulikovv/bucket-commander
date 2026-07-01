# Implementation PR Plan

This plan splits Bucket Commander into logical, reviewable pull requests. The order is intentionally foundation-first: each PR should leave the app in a runnable or testable state and avoid mixing UI, storage, bucket APIs, and batch-job machinery in one large change.

## PR 1: Project Packaging And Test Harness

Purpose:

- Make the project installable and testable.
- Establish formatting, dependencies, and package boundaries.

Changes:

- Add `pyproject.toml`.
- Add package init files under `src/bc`.
- Add console entry point, for example `bucket-commander`.
- Add `pytest` setup.
- Add base dev dependencies.
- Update devcontainer dependencies to match the project file.

Acceptance:

- `python -m pytest` runs.
- `bucket-commander --help` or equivalent entry point works.
- Existing prototype code still imports or is clearly parked as experimental.

## PR 2: Core Domain Models

Purpose:

- Introduce the shared model layer that all UI, backend, index, and job code will use.

Changes:

- Add `src/bc/core/locations.py`.
- Add `src/bc/core/models.py`.
- Add normalized URI support for `file://` and `s3://`.
- Add `Entry`, `EntryType`, `PanelState`, and operation result models.
- Add unit tests for URI parsing and entry normalization.

Acceptance:

- Local paths and bucket URIs round-trip correctly.
- Invalid locations produce clear errors.
- No UI code depends directly on raw string paths for new code.

## PR 3: Backend Interface And Local Backend

Purpose:

- Move filesystem logic out of the UI and behind a backend abstraction.

Changes:

- Add `src/bc/backends/base.py`.
- Add `src/bc/backends/local.py`.
- Implement local listing, stat, mkdir, copy, move, rename, and delete.
- Use blocking filesystem operations behind a clean async interface.
- Add tests with temporary directories.

Acceptance:

- Local directory listings return `Entry` objects.
- File and directory copy/move/delete work in tests.
- Permission and missing-path errors are represented consistently.

## PR 4: Minimal Two-Panel App On Core Models

Purpose:

- Replace the current exploratory UI with a working local two-panel interface backed by the new model/backend layer.

Changes:

- Add `src/bc/app.py`.
- Add `src/bc/ui/panels.py`.
- Add `src/bc/ui/commands.py`.
- Keep Urwid or switch to Textual, but make the choice explicit in this PR.
- Support panel focus, directory navigation, parent navigation, refresh, and footer status.
- Remove or quarantine prototype code that conflicts with the new structure.

Acceptance:

- App opens with two local panels.
- User can switch panels and navigate local directories.
- UI reads through `LocalBackend`, not `os.listdir` directly.

## PR 5: Task Manager And Real Progress Plumbing

Purpose:

- Create the short-lived task layer needed for responsive UI operations.

Changes:

- Add `src/bc/core/task_manager.py`.
- Add task states: pending, running, cancelling, cancelled, failed, completed.
- Add progress model with item count, byte count, current item, and message.
- Wire local copy/delete operations through tasks.
- Replace simulated progress with task-driven progress UI.

Acceptance:

- Long local operations can report progress.
- Operations can be cancelled where supported.
- UI remains responsive during task execution.

## PR 6: Parquet Index Foundation

Purpose:

- Add the durable metadata cache before bucket support depends on it.

Changes:

- Add `src/bc/index/manifest.py`.
- Add `src/bc/index/parquet_store.py`.
- Add object and prefix schemas from the specification.
- Add append-only batch writing.
- Add manifest updates.
- Add read/query helpers for current-prefix listings.

Acceptance:

- Tests can write object metadata to Parquet and read it back.
- Manifest tracks active Parquet files.
- Existing Parquet files are not mutated in place.

## PR 7: S3 Backend Basic Browsing

Purpose:

- Add first bucket backend with async current-prefix listing.

Changes:

- Add `src/bc/backends/s3.py`.
- Implement `s3://bucket/prefix/` listing.
- Return objects and common prefixes as `Entry` values.
- Support AWS profile/region resolution.
- Add integration-test path for MinIO or LocalStack.

Acceptance:

- A panel can open an S3 bucket or prefix.
- Listing is asynchronous and does not block the UI.
- Direct prefix/object display works without recursive indexing.

## PR 8: Cache-Backed Bucket Panels

Purpose:

- Connect S3 browsing to Parquet caching.

Changes:

- On bucket open, load cached prefix entries first.
- Start live listing in the background.
- Append live listing results to Parquet.
- Show cache state: cached, live, stale, partial, fresh.
- Add refresh current prefix.

Acceptance:

- Reopening a visited prefix displays cached results quickly.
- Live listing updates the panel incrementally.
- New listing batches are written as new Parquet files and tracked in the manifest.

## PR 9: Recursive Indexer With Checkpoints

Purpose:

- Implement full or prefix-scoped background bucket indexing.

Changes:

- Add `src/bc/index/indexer.py`.
- Add bounded-concurrency recursive listing.
- Add checkpoint files.
- Add resume after interruption.
- Update prefix summary metadata.

Acceptance:

- User can start recursive indexing for a bucket/prefix.
- Indexing can be cancelled and resumed.
- Prefix summaries distinguish partial and fully indexed prefixes.

## PR 10: Indexed Search And Sort For Buckets

Purpose:

- Make the Parquet index useful for fast navigation and discovery.

Changes:

- Add `src/bc/index/query.py`.
- Query Parquet with DuckDB or Polars.
- Support search by name/key, prefix, size, and modified time.
- Support large result pagination or virtualization.
- Show partial/stale coverage warnings.

Acceptance:

- Search over indexed bucket metadata works without remote listing.
- Results indicate whether the index is partial or stale.
- Large result sets do not freeze the UI.

## PR 11: Durable Batch Job Architecture

Purpose:

- Introduce durable jobs for large copy, move, delete, and indexing workflows.

Changes:

- Add `src/bc/jobs/models.py`.
- Add `src/bc/jobs/planner.py`.
- Add `src/bc/jobs/store.py`.
- Add `src/bc/jobs/queue.py`.
- Add SQLite job store.
- Add job item model and status transitions.

Acceptance:

- A copy/move/delete request can be converted into a durable job plan.
- Job state survives app restart.
- Job planning uses indexed bucket metadata when available.

## PR 12: Batch Workers And Transfers

Purpose:

- Execute durable jobs with concurrency, retries, cancellation, and verification.

Changes:

- Add `src/bc/jobs/worker.py`.
- Implement local-to-local, local-to-bucket, bucket-to-local transfers.
- Add move phases: plan, copy, verify, delete source, complete.
- Add retry policy and failure reporting.
- Add conflict behavior options.

Acceptance:

- Batch copy jobs execute outside direct UI ownership.
- Failed items are recorded and retryable.
- Move deletes source only after successful verification.

## PR 13: Job Monitor UI

Purpose:

- Let the user observe and control durable jobs from the TUI.

Changes:

- Add jobs panel/modal.
- Show queued, active, paused, failed, and completed jobs.
- Add pause, resume, cancel, retry failed, and view errors.
- Link file panels to active job status.

Acceptance:

- User can continue browsing while jobs run.
- User can inspect job progress and failures.
- Cancel/pause/resume actions update durable state.

## PR 14: Configuration And Profiles

Purpose:

- Make cache, provider, concurrency, and UI behavior configurable.

Changes:

- Add `src/bc/config/settings.py`.
- Add `src/bc/config/profiles.py`.
- Read `~/.config/bucket-commander/config.toml`.
- Support environment overrides.
- Add cache directory selection and provider settings.

Acceptance:

- Configuration loads with documented defaults.
- Environment variables override config.
- S3 profile/region/endpoint can be configured.

## PR 15: Hardening, Compaction, And Release Readiness

Purpose:

- Finish operational reliability and clean up for a usable release.

Changes:

- Add Parquet compaction.
- Add stale row/tombstone handling.
- Add corrupted-index recovery path.
- Add structured logging.
- Add destructive-operation confirmations.
- Add documentation for cache sensitivity and credentials.
- Add performance tests for large indexes.

Acceptance:

- Compaction keeps newest object metadata rows.
- Damaged indexes do not block live bucket access.
- Credentials are not written to logs or cache.
- Release documentation explains known limitations.

