"""Tasks & scheduling (Phase 3): task store, reminders, and background jobs
that wake the agent without a user prompt.

Layout (built up over Milestone 3):

* ``store.py``    — TaskStore: SQLite persistence for tasks + run history (schema v3)
* ``triggers.py`` — pure next-fire computation over APScheduler triggers
* ``service.py``  — TaskService: scheduling semantics (misfire, recurrence, failure cap)
* ``runner.py``   — BackgroundRunner: the asyncio wake loop that fires due tasks
"""
