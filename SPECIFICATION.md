# Bucket Commander Specification

## 1. Purpose

Bucket Commander is a Python terminal application with a two-panel file manager interface for working with local files and object-storage buckets. It should feel familiar to users of Midnight Commander, while handling bucket traversal, indexing, searching, and transfer operations efficiently for large remote namespaces.

The application must support local filesystem browsing in either panel, bucket browsing in either panel, and transfers between any supported source and destination:

- local to local
- local to bucket
- bucket to local
- bucket to bucket

Bucket exploration must be asynchronous and incremental. Bucket metadata must be persisted as Parquet files so previously explored buckets can be opened and searched quickly without repeatedly listing the full bucket.

## 2. Goals

- Provide a responsive two-panel terminal UI for local and bucket file operations.
- Avoid blocking the UI while listing large buckets.
- Persist bucket object metadata in Parquet for fast reload, filtering, and search.
- Allow partial indexes to be useful immediately while deeper traversal continues in the background.
- Support resumable indexing and refresh of changed bucket prefixes.
- Keep provider-specific bucket APIs behind a small abstraction layer.
- Make common commander operations ergonomic: open, copy, move, delete, rename, mkdir, refresh, search, select, compare, and view metadata.
- Make cloud-specific state understandable at a glance: cache freshness, partial indexes, live refreshes, background jobs, and destructive-operation scope.

## 3. Non-Goals For Initial Version

- Graphical desktop UI.
- Full object version-history management.
- Multi-user collaboration features.
- Server-side daemon requirement.
- Editing remote objects in place without a local temporary file.
- Replacing dedicated provider CLIs for advanced IAM, lifecycle, or policy management.

## 4. Target Runtime

- Python 3.11 or newer.
- Terminal UI based on `urwid`, `textual`, or another async-friendly TUI framework.
- Async runtime based on `asyncio`.
- Parquet access through `pyarrow`.
- Optional DataFrame/query layer through `polars` or `duckdb`.

Recommended initial stack:

- UI: `textual`, because it has first-class async support and richer widgets.
- Storage metadata: `pyarrow` for writing Parquet, `duckdb` for querying cached metadata.
- Local filesystem async bridge: `asyncio.to_thread` for blocking filesystem operations.
- S3-compatible buckets: `aioboto3` or `aiobotocore`.

## 5. Key Concepts

### 5.1 Panel

A panel is one side of the two-panel interface. Each panel has:

- current location
- backend type: `local` or `bucket`
- visible entries
- selection state
- sort mode
- filter/search state
- background task status
- index freshness indicator when viewing a bucket
- visible cache/index state when viewing a bucket
- active operation or job indicator when relevant

### 5.2 Location

Locations are represented as URIs:

- local path: `file:///Users/name/data`
- S3 bucket root: `s3://bucket-name/`
- S3 prefix: `s3://bucket-name/path/to/prefix/`

The UI may show shorter human-readable labels, but internal operations should use normalized URI objects.

### 5.3 Bucket Index

A bucket index is the persisted metadata cache for a bucket or configured bucket namespace. It is stored as one or more Parquet files plus a small manifest.

The index is appendable, refreshable, and queryable while incomplete.

## 6. Functional Requirements

### 6.1 Two-Panel Interface

The application must show:

- left panel
- right panel
- command/status footer
- operation progress area
- global key hints or help screen

Each panel must show entries with:

- name
- type: directory/prefix, file/object, symlink if local, special item
- size
- modified time
- provider/backend
- optional object metadata indicators such as storage class, etag, encryption, or cache status

The user must be able to:

- switch focused panel
- navigate into directories or prefixes
- go to parent
- open a location prompt
- refresh current location
- select/unselect entries
- invert selection
- search/filter visible entries
- sort by name, size, modified time, and type
- copy from active panel to inactive panel
- move from active panel to inactive panel
- delete selected entries
- rename a selected entry
- create local directory or bucket prefix marker where supported
- view metadata/details for the selected entry
- cancel long-running operations

The interface must make important state visible without requiring the help screen:

- focused panel
- backend type for each panel
- current location
- sort field and order
- selection count
- visible filter or indexed search query
- loading or live-refresh state
- cache/index state for bucket panels
- current task or job status

Footer commands should be contextual where possible. The footer may remain compact, but it should avoid advertising actions that are unavailable for the current panel, selection, or task without a clear disabled/explanatory state.

### 6.2 Local Filesystem Backend

The local backend must support:

- listing directories
- stat metadata
- file copy
- directory recursive copy
- move/rename
- delete file or directory
- create directory
- optional hidden-file toggle

Local operations that may block should run outside the UI event loop.

### 6.3 Bucket Backend

The bucket backend must support:

- listing bucket prefixes and objects
- retrieving object metadata
- uploading local files
- downloading objects
- copying objects within or between buckets where provider supports server-side copy
- deleting objects
- renaming objects as copy then delete
- creating logical prefixes when provider conventions allow it
- refreshing a prefix index

The first implementation should support S3-compatible APIs. Additional providers must be added through the backend interface rather than UI-specific code.

### 6.4 Async Bucket Exploration

Bucket exploration must be asynchronous and incremental:

- Opening a bucket or prefix should display cached entries immediately if available.
- If no cache exists, the panel should show an initial loading state without freezing the UI.
- Listing must happen in paginated async tasks.
- Newly discovered entries should stream into the in-memory panel model.
- Metadata should be flushed to Parquet in batches.
- The user must be able to keep navigating while deeper indexing continues.
- The user must be able to cancel or pause indexing.
- The user must be able to trigger background full-bucket indexing.

Two exploration modes are required:

- On-demand exploration: list only the current prefix and cache the result.
- Recursive indexing: walk all prefixes under a bucket or selected prefix in the background.

### 6.5 Fast Access From Parquet

The application must use the Parquet index to:

- open previously visited bucket prefixes without full remote listing
- search indexed bucket objects
- sort and filter large object lists locally
- compare local directories against bucket prefixes
- resume interrupted indexing
- identify stale prefixes that need refresh

The UI must communicate cache state:

- live: remote listing in progress
- cached: loaded from Parquet
- partial: index does not cover all descendants
- stale: cache is older than configured TTL or provider marker
- fresh: current prefix has been recently listed

### 6.6 Operation Safety And Planning

Potentially destructive, expensive, or large operations must be planned before execution. This includes:

- delete
- move
- recursive copy
- bucket prefix operations
- bucket-to-bucket transfers
- recursive indexing of large prefixes

Before execution, the UI must show a confirmation or planning view with:

- source and destination where applicable
- selected direct entries
- expanded object/file count when known
- estimated total bytes when known
- whether expansion is complete, partial, stale, or still live
- conflict behavior
- destructive phases such as "delete source after verify"
- expected resumability and retry behavior for durable jobs

Delete and move confirmations must be required by default. Configuration may allow advanced users to reduce confirmation prompts, but destructive scope must still be recoverable or explicit.

### 6.7 Search And Discovery UX

The application must distinguish between:

- visible-panel filtering, which narrows entries already loaded in the panel
- indexed bucket search, which queries persisted Parquet metadata
- live refresh or live recursive indexing, which contacts the provider

Indexed search results must show:

- query text
- total result count when known
- displayed result limit or page
- sort field and order
- coverage state: full, partial, stale, or unknown
- next useful action when coverage is partial or stale, such as refresh current prefix or index recursively

## 7. Parquet Index Design

### 7.1 Index Directory Layout

Default cache location:

```text
~/.cache/bucket-commander/
  indexes/
    s3/
      account-or-profile/
        bucket-name/
          manifest.json
          objects/
            partition_prefix_hash=00/
              part-000001.parquet
            partition_prefix_hash=01/
              part-000002.parquet
          prefixes/
            part-000001.parquet
          checkpoints/
            recursive-index.json
```

The cache root must be configurable through settings and environment variables.

### 7.2 Manifest

`manifest.json` stores index metadata:

- schema version
- provider
- profile/account identifier
- bucket name
- region or endpoint
- created time
- last updated time
- indexing mode history
- covered prefixes
- stale TTL policy
- Parquet object file list
- Parquet prefix file list
- last successful checkpoint

### 7.3 Object Schema

Object metadata Parquet files must use a stable schema:

| Column | Type | Description |
| --- | --- | --- |
| provider | string | Storage provider, for example `s3` |
| account_id | string | Profile/account namespace |
| bucket | string | Bucket name |
| key | string | Full object key |
| parent_prefix | string | Immediate parent prefix |
| name | string | Final path component |
| size | int64 | Object size in bytes |
| last_modified | timestamp[us, tz=UTC] | Provider modified time |
| etag | string | Provider etag/checksum when available |
| checksum | string | Strong checksum when available |
| storage_class | string | Provider storage class |
| content_type | string | Content type if known |
| encryption | string | Encryption mode if known |
| version_id | string | Optional object version |
| is_delete_marker | bool | Optional versioning marker |
| discovered_at | timestamp[us, tz=UTC] | Time this row was listed |
| refreshed_at | timestamp[us, tz=UTC] | Time this row was confirmed current |
| source_listing_id | string | Identifier for the indexing run |

### 7.4 Prefix Schema

Prefix Parquet files must store:

| Column | Type | Description |
| --- | --- | --- |
| provider | string | Storage provider |
| account_id | string | Profile/account namespace |
| bucket | string | Bucket name |
| prefix | string | Full logical prefix |
| parent_prefix | string | Parent prefix |
| name | string | Display name |
| object_count | int64 | Known direct object count |
| recursive_object_count | int64 | Known descendant count if indexed |
| total_size | int64 | Known direct object size |
| recursive_total_size | int64 | Known descendant size if indexed |
| fully_indexed | bool | Whether descendants are completely indexed |
| listed_at | timestamp[us, tz=UTC] | Last direct listing time |
| recursive_indexed_at | timestamp[us, tz=UTC] | Last recursive completion time |

### 7.5 Partitioning Strategy

The index should support many objects without creating one file per prefix. Recommended partitioning:

- partition by a stable hash of `parent_prefix` or top-level prefix
- target Parquet row group size: 50,000 to 250,000 rows
- compact small Parquet files after indexing runs
- avoid partitioning by high-cardinality full key

For very large buckets, support multiple datasets:

- object metadata dataset
- prefix summary dataset
- optional full-text/search sidecar
- optional transfer history dataset

### 7.6 Update Semantics

Parquet files are immutable once written. Updates should be handled through:

- append-only listing batches
- manifest pointer to active files
- periodic compaction that keeps the newest row per `(bucket, key, version_id)`
- tombstone rows or a separate delete dataset for objects no longer found

Prefix refresh should mark prior rows under that direct prefix as stale until replaced or compacted.

## 8. Architecture

### 8.1 Main Components

```text
bucket_commander/
  app.py
  ui/
    panels.py
    commands.py
    progress.py
  core/
    locations.py
    models.py
    operations.py
    task_manager.py
  backends/
    base.py
    local.py
    s3.py
  index/
    manifest.py
    parquet_store.py
    query.py
    indexer.py
    compaction.py
  config/
    settings.py
    profiles.py
```

### 8.2 Backend Interface

Each backend must implement:

```python
class Backend(Protocol):
    async def list(self, location: Location) -> AsyncIterator[EntryBatch]: ...
    async def stat(self, location: Location) -> Entry: ...
    async def copy(self, source: Location, destination: Location, progress: ProgressSink) -> None: ...
    async def move(self, source: Location, destination: Location, progress: ProgressSink) -> None: ...
    async def delete(self, location: Location, recursive: bool, progress: ProgressSink) -> None: ...
    async def mkdir(self, location: Location) -> None: ...
```

Bucket backends may additionally implement:

```python
class BucketBackend(Backend, Protocol):
    async def list_page(self, bucket: str, prefix: str, token: str | None) -> BucketPage: ...
    async def head_object(self, bucket: str, key: str) -> ObjectMetadata: ...
    async def server_side_copy(self, source: Location, destination: Location) -> None: ...
```

### 8.3 Task Manager

All long-running work must be represented as tasks:

- listing
- recursive indexing
- upload
- download
- copy
- delete
- compaction
- search

Task records must include:

- task id
- type
- source/destination
- status
- progress counts and bytes
- start/end time
- cancellation token
- latest error

The UI subscribes to task updates rather than polling backend internals.

### 8.4 Job And Task Observability

Short-lived tasks and durable jobs must be visible without blocking panel navigation. The UI must provide:

- a compact active-task or active-job indicator
- a detailed task/job view
- status: queued, running, paused, cancelling, cancelled, failed, completed
- phase for multi-step work, such as planning, copying, verifying, deleting, retrying, or compacting
- item progress, byte progress, current item, and recent error
- throughput and ETA when enough data is available
- failure counts and retry controls where durable jobs support retry
- pause, resume, cancel, and retry failed actions where supported

If multiple tasks or jobs are active, the UI must make it clear which one is currently summarized in the footer and provide a way to inspect the rest.

## 9. User Workflows

### 9.1 Open Bucket

1. User opens location prompt in a panel.
2. User enters `s3://bucket-name/path/`.
3. Application loads cached entries for the prefix if present.
4. Application starts async listing for the current prefix.
5. Panel updates as live entries arrive.
6. New metadata is written to Parquet in batches.
7. Panel header and footer show cache and listing status.
8. User can keep navigating while the live refresh continues.

### 9.2 Recursive Bucket Index

1. User selects "Index recursively" for a bucket or prefix.
2. Application creates an index task.
3. Indexer lists pages breadth-first or depth-first with bounded concurrency.
4. Batches are written to Parquet.
5. Checkpoints are saved regularly.
6. User can cancel and resume later.
7. On completion, prefix summaries are updated.
8. The job/task view shows current prefix, checkpoint status, indexed object count, bytes indexed, and whether the viewed prefix is partially or fully indexed.

### 9.3 Search Bucket

1. User enters a search query in a bucket panel.
2. Application queries Parquet first.
3. If the index is partial or stale, UI shows that result coverage is partial.
4. User can optionally start live recursive search/indexing from the query view.
5. Search mode remains visually distinct from normal browsing until the user exits it.

### 9.4 Copy From Bucket To Local

1. User selects objects or prefixes in the bucket panel.
2. User invokes copy to the local panel.
3. Application shows an operation plan with destination, selected entries, known expansion coverage, conflict behavior, estimated count, and estimated bytes.
4. Application expands selected prefixes using the index plus live listing where needed.
5. Downloads run with bounded concurrency.
6. Progress shows object count, byte count, throughput, current file, failures.
7. Failed files can be retried.

## 10. Performance Requirements

- The UI must remain responsive during bucket listing, indexing, and transfers.
- Current-prefix cached listing should render within 300 ms for up to 10,000 direct entries on a typical laptop.
- The indexer should use bounded concurrency to avoid provider throttling.
- Batch writes should avoid writing Parquet files smaller than necessary during steady indexing.
- Large result sets should be virtualized or paginated in the UI.
- Search over indexed metadata should complete within seconds for millions of objects when using DuckDB or Polars over Parquet.
- Task and cache state updates should be lightweight enough that they do not cause visible panel flicker during large listings or transfers.

## 11. Error Handling

The application must handle:

- network failures
- provider throttling
- expired credentials
- permission denied
- missing bucket
- object changed during operation
- local permission errors
- insufficient disk space for cache or downloads
- corrupted index files

Index corruption should not prevent live bucket access. The application should offer clear recovery choices such as ignore cache, rebuild current prefix, or rebuild bucket index.

Errors shown in the UI must explain the problem and the next useful action when one exists. Examples include retry, refresh credentials, refresh prefix, rebuild cache, free disk space, or open job details.

## 12. Configuration

Configuration should include:

- cache root
- provider profiles
- default provider
- endpoint URLs for S3-compatible storage
- region
- index TTL
- max listing concurrency
- max transfer concurrency
- multipart upload/download thresholds
- default conflict behavior
- destructive confirmation behavior
- hidden file visibility
- theme/keymap
- optional advanced metadata columns

Configuration file location:

```text
~/.config/bucket-commander/config.toml
```

Environment overrides:

- `BUCKET_COMMANDER_CACHE_DIR`
- `BUCKET_COMMANDER_CONFIG`
- provider-standard variables such as `AWS_PROFILE`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`

## 13. Security

- Do not store secret access keys in the Parquet index.
- Do not log credentials, signed URLs, or authorization headers.
- Use provider SDK credential resolution where possible.
- Cache files should be created with user-only permissions where the platform supports it.
- The index may contain sensitive object names; document this clearly.
- Destructive operations must require confirmation unless explicitly disabled.

## 14. Testing Requirements

Unit tests:

- location parsing
- local backend operations
- backend interface behavior with fakes
- Parquet schema writing and reading
- manifest update logic
- index query behavior
- task cancellation

Integration tests:

- local filesystem panel operations using temporary directories
- S3-compatible backend against LocalStack or MinIO
- interrupted indexing and resume
- stale prefix refresh
- copy/move/delete flows

UI tests:

- panel navigation
- focus switching
- command handling
- progress display
- cancellation flow
- destructive-operation confirmation
- cache/index state indicators
- indexed-search coverage warnings
- job monitor and retry controls

Performance tests:

- render large cached prefix
- index synthetic bucket with millions of keys
- search indexed metadata
- compaction behavior

## 15. Implementation Phases

### Phase 1: Core Local Two-Panel App

- Build stable panel model.
- Implement local backend.
- Implement copy/move/delete/rename for local files.
- Add task manager and progress reporting.

### Phase 2: S3 Bucket Browsing

- Add location parser for `s3://`.
- Implement async S3 listing for current prefix.
- Display bucket prefixes and objects in panels.
- Add basic upload and download.

### Phase 3: Parquet Index

- Add manifest and Parquet object/prefix schemas.
- Cache current-prefix listings.
- Load cached prefix before live listing.
- Add cache state indicators.
- Show cache/index state in panel headers and relevant rows.

### Phase 4: Recursive Indexing And Search

- Add recursive background indexer.
- Add checkpoints and resume.
- Add indexed search with partial/stale coverage indicators.
- Make indexed search visually distinct from visible-panel filtering.
- Add compaction.

### Phase 5: Robust Operations

- Add retry policies.
- Add transfer conflict handling.
- Add server-side bucket copy where supported.
- Add batch failure report and retry.
- Add operation planning and confirmation views.
- Add job monitor UI for durable operations.
- Add configuration and profile management.

## 16. Acceptance Criteria

The application is acceptable when:

- A user can open two local directories and copy files between panels.
- A user can open an S3 bucket or prefix in either panel.
- Bucket listing does not freeze the UI.
- Bucket entries appear incrementally during remote listing.
- Current-prefix bucket metadata is saved to Parquet.
- Reopening an indexed bucket prefix displays cached entries quickly.
- A user can start recursive indexing and continue using the UI.
- Interrupted recursive indexing can resume from a checkpoint.
- Search can query indexed bucket metadata from Parquet.
- Local-to-bucket and bucket-to-local transfers show progress and can be cancelled.
- Users can see whether bucket panels and search results are live, cached, fresh, stale, partial, or fully indexed.
- Destructive operations show a plan and require confirmation by default.
- Durable jobs can be inspected, paused, resumed, cancelled, and retried where supported.
- Credentials are not written to cache or logs.
