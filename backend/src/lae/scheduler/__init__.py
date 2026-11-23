"""The ``scheduler`` process (``lae scheduler``): periodic jobs (Phase 1, Step 1.15).

Modules:

- ``jobs``: what a job is (``Job``: a name, an ``async`` function of a ``JobContext``, and a
  schedule, ``Every`` or ``DailyAt``), ``run_job`` (one run under the job's PostgreSQL advisory
  lock, with its bookkeeping in ``sync_state`` under ``job:<name>``) and ``read_job_state``.
- ``main``: ``registered_jobs`` (the registry), the ``Scheduler`` loop and ``main(settings)``,
  which ``lae scheduler`` runs until ``SIGTERM``.
- ``reaper``: the hourly draft reaper (decision B7): drafts idle for ``draft_ttl_days`` are
  discarded through ``crdt-ws``, never-published records are deleted with them.

Adding a job (Phase 2 and later): write ``async def my_job(ctx: JobContext) -> dict | None`` (the
returned mapping is stored as ``result`` in the job's ``sync_state`` row, so keep it small and
JSON-serializable), check ``ctx.should_stop()`` between items of work, and add
``Job("my_job", my_job, Every(...))`` or ``DailyAt(time(...))`` to ``registered_jobs``.
"""
