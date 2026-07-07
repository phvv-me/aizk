# The handful of entity and relation types referenced by name in code rather than only through
# the live catalog, always seeded, always active, never deactivatable (RAPTOR_SUMMARY, OBSERVATION,
# and OBSERVES carry `structural=True` in the seed migration for exactly that reason). A plain
# string constant rather than an enum member, since `EntityContent.type`/`FactContent.predicate`
# are themselves plain `str` columns and the actual closed vocabulary now lives in `entity_kind`/
# `relation_kind`, not in Python.

RAPTOR_SUMMARY = "RaptorSummary"
OBSERVATION = "Observation"
PROJECT = "Project"
AREA = "Area"
CONCEPT = "Concept"

OBSERVES = "observes"
RELATED_TO = "related_to"
DEPENDS_ON = "depends_on"
PART_OF = "part_of"
CITES = "cites"
SUPERSEDES = "supersedes"
