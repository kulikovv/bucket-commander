# Agent Instructions

These instructions apply to the whole repository. Follow them when implementing Bucket Commander, reviewing changes, or creating plans.

## Product Context

Bucket Commander is a Python terminal application with a two-panel file manager interface for local files and object-storage buckets. It should support responsive local browsing, async bucket exploration, Parquet-backed bucket metadata indexes, and durable batch jobs for large operations such as copy, move, delete, and recursive indexing.

Known locations are configured through TOML source files. These files may store non-secret routing metadata such as local paths, S3 URIs, regions, endpoints, and AWS profile names, but they must not store access keys, secret keys, session tokens, passwords, or other credentials. S3 credentials should be resolved by the AWS SDK through profiles, SSO, environment variables, web identity, or instance/container roles.

Primary design references:

- `SPECIFICATION.md`
- `IMPLEMENTATION_PRS.md`

When code and documentation disagree, preserve working behavior and update the documentation or ask for direction before making a broad architectural change.

## Tooling

- Use `uv`, not `pip`, for dependency management and running project commands.
- Prefer:
  - `uv sync`
  - `uv add <package>`
  - `uv add --dev <package>`
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run ruff format .`
  - `uv run mypy src tests` or `uv run pyright`
- Do not add `requirements.txt` unless explicitly requested.
- Do not install dependencies globally.
- Keep dependency changes in `pyproject.toml` and `uv.lock`.
- Use `ruff` for linting and formatting.
- Use a type checker. Prefer `mypy` for Python package internals unless the project has already standardized on `pyright`.
- Before finishing a code change, run the narrowest useful checks. For shared/core changes, run tests, ruff, and type checks.

## Python Best Practices

- Target Python 3.11+.
- Use clear modules with small public APIs.
- Prefer dataclasses, enums, typed protocols, and explicit value objects for core domain concepts.
- Keep I/O, UI, indexing, and provider-specific code separated.
- Avoid global mutable state.
- Avoid broad `except Exception` blocks unless re-raising as a domain-specific error with useful context.
- Preserve exception chains with `raise NewError(...) from exc`.
- Use `pathlib.Path` for local paths.
- Use structured logging instead of `print` in library code.
- Keep comments sparse and useful. Explain non-obvious decisions, not line-by-line mechanics.
- Prefer simple functions until there is a clear reason for a class.
- Keep functions focused. If a function both plans work and performs I/O, split it.
- Make behavior testable without real cloud credentials.

## Typing Standards

- Add type hints to new public functions, methods, and dataclasses.
- Use `Protocol` for backend interfaces.
- Use `Literal` or `Enum` for closed status/type sets.
- Avoid `Any` unless wrapping an untyped third-party API. Contain it at the boundary.
- Prefer `Mapping`/`Sequence` for read-only inputs and concrete types for return values.
- Make optional values explicit with `T | None`.
- Keep domain models typed independently from UI widgets and provider SDK response dictionaries.

## Async Programming Best Practices

- Use `asyncio` for concurrent bucket exploration, indexing, transfers, and background tasks.
- Never block the event loop with local filesystem calls, CPU-heavy work, compression, Parquet writes, or provider SDK calls that are not truly async.
- Use `asyncio.to_thread` for blocking local filesystem operations when needed.
- Use bounded concurrency with `asyncio.Semaphore` or worker queues for provider calls and transfers.
- Always design cancellation paths for long-running operations.
- Propagate cancellation cleanly. Do not swallow `asyncio.CancelledError` unless you re-raise it after cleanup.
- Prefer structured task ownership:
  - UI starts or observes tasks.
  - task manager owns short-lived tasks.
  - job worker owns durable batch jobs.
- Avoid orphaned background tasks. Every created task should be tracked, awaited, or explicitly managed.
- Stream results incrementally for large listings. Do not gather an entire bucket into memory before updating the UI or index.
- Batch writes to Parquet. Do not write one Parquet file per object.
- Use backpressure for queues between listing, indexing, and UI updates.

## System Design Expectations

- Keep architecture aligned with the specification:
  - `ui/` renders state and gathers commands.
  - `core/` owns domain models, locations, operations, and task abstractions.
  - `backends/` owns local and bucket provider integrations.
  - `index/` owns manifests, Parquet storage, query, indexing, and compaction.
  - `jobs/` owns durable batch planning and execution.
  - `config/` owns settings and profiles.
- Do not let UI code directly call cloud SDKs or parse provider-specific responses.
- Do not let backend code depend on UI widgets.
- Keep local/S3 transfer behavior in backend or operation layers. UI code should select locations and start operations, not implement upload/download logic.
- Treat object-storage prefixes as logical views, not real directories.
- Use immutable, append-only Parquet update semantics:
  - write new batch files
  - update manifest pointers
  - compact later
  - represent deletion/staleness explicitly
- Use SQLite or another transactional store for frequently changing durable job state. Do not use Parquet for high-frequency job-status updates.
- Large operations should be planned before execution. A move should be modeled as copy, verify, then delete source.
- Cross-provider copy and move should support local-to-S3 and S3-to-local flows. Preserve provider metadata on locations so selected S3-compatible endpoints, regions, and profiles continue to work while navigating prefixes.
- Design for resume and retry from the beginning for bucket indexing and batch jobs.
- Keep provider-specific capabilities optional. For example, server-side copy should be used when available but not required by the common operation model.

## Testing Guidance

- Add unit tests for core models, parsing, backend behavior, index manifest updates, and job state transitions.
- Use temporary directories for local backend tests.
- Use fakes or stub backends for operation planning tests.
- Keep cloud integration tests optional and gated behind environment variables.
- Prefer deterministic tests over sleeps. For async tests, use explicit synchronization or controlled fakes.
- Test cancellation, partial failure, and resume behavior for long-running tasks.
- For Parquet index tests, verify both schema and query behavior.

## Code Organization

Expected package direction:

```text
src/bc/
  app.py
  ui/
  core/
  backends/
  index/
  jobs/
  config/
tests/
```

When adding a new module, place it where the dependency direction remains clean:

```text
ui -> core -> backends/index/jobs/config
```

Provider SDK details should remain inside `backends/`. Parquet details should remain inside `index/`. Durable queue details should remain inside `jobs/`.

## Review Checklist

Before handing off a change, check:

- Does it preserve the two-panel app direction?
- Does it keep long-running work out of the UI event loop?
- Does it avoid blocking async code?
- Does it use `uv` commands and project dependencies?
- Does it pass `ruff` and type checks where applicable?
- Are new public APIs typed?
- Are provider-specific details isolated?
- Are Parquet files treated as immutable batch outputs?
- Are destructive operations explicit and recoverable where practical?
- Are tests added at the right level?
