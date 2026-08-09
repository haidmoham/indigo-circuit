# Indigo Circuit agent instructions

- Read `CONTEXT.md` before changing application, ingest, dbt, pipeline, or deployment behavior.
- Treat `CONTEXT.md` as project-local state. Do not duplicate it into a global memory system.
- For on-demand ingest, recovery, mart refresh, or pipeline verification work, use `skills/ingest-loop/SKILL.md`.
- Preserve the existing maker/checker pipeline boundary. A successful build is not sufficient; the production gate must pass.
- Prefer the narrowest fix that restores a failed invariant. Do not add broad pipeline machinery when one source, model, or gate is failing.
- Keep direct evidence distinct from inference. Verify important data claims against DuckDB, dbt output, the pipeline gate, or the source API/site.
- Preserve idempotent ingest behavior and last-known-good protection.
- Keep credentials in environment configuration. Never copy secrets into repository docs, logs, or agent context.
- Use agents for implementation friction and diagnosis support. Keep product judgment, statistical interpretation, and changes to trust boundaries with the human unless explicitly delegated.
