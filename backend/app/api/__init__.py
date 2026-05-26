"""Backend's ``app.api`` namespace — FastAPI routers and dependencies.

Modules:
  - ``deps``       — DI resolvers (get_job_store)
  - ``health``     — /health/live, /health/ready
  - ``jobs``       — /api/jobs/*
  - ``providers``  — /api/providers/*
  - ``rate_limit`` — shared slowapi Limiter

Intentionally empty at the package level so each router stays
importable via ``app.api.X``.
"""
