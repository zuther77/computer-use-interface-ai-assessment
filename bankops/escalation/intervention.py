"""InterventionRequest model + persistence (Phase 8; DECISIONS.md §9a).

A structured Pydantic model persisted to /pending_interventions/{id}.json
the moment it is raised; notification routing itself is stubbed as a log
line.
"""
