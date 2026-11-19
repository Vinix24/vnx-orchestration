- Deliverables (ADR-096, B1): migration 0039 adds `deliverable_dependencies` (ADR-007 key `(project_id, deliverable_id, depends_on_id)`).
  `vnx deliverable add --depends-on`, `depend --on [--kind soft]`, `undepend --on --reason` (the row stays) and `deps [--json]`; a cycle or an unknown id is refused.
