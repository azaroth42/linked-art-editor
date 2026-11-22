"""JSON-LD to Y.Doc projection and hydration.

The rules are R1-R25 of ``docs/spikes/S3-projection.md`` (ADR-0018). ``hydrate`` builds the
``Doc`` of a record, ``project`` reads it back, ``strip_internal_iris`` prepares public output,
and ``lae.projection.ops`` holds every operation that changes a hydrated document.
"""

from lae.projection.canonical_json import canonical_json
from lae.projection.hydrate import assign_node_iris, hydrate
from lae.projection.project import project, strip_internal_iris
from lae.projection.rules import ProjectionError, is_reference

__all__ = [
    "ProjectionError",
    "assign_node_iris",
    "canonical_json",
    "hydrate",
    "is_reference",
    "project",
    "strip_internal_iris",
]
