# Claude Instructions

Follow the repository guidance in `AGENTS.md`.

Important highlights:

- Use `uv`, not `pip`.
- Use `ruff` for formatting and linting.
- Use type checking with `mypy` or `pyright`.
- Keep UI, core models, backends, Parquet indexing, durable jobs, and config separated.
- Use async carefully: no blocking filesystem, Parquet, or SDK calls on the event loop.
- Treat Parquet metadata files as immutable batch outputs and compact later.
- Read `SPECIFICATION.md` and `IMPLEMENTATION_PRS.md` before making architectural changes.

