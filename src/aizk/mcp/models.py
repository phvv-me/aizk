import uuid

from mainboard.profiling import SpanStat
from patos import FrozenModel


class WriteResult(FrozenModel):
    """The id one write tool wrote, the common return for remember, reference, and ingest_image.

    id: identity of the row the write landed as.
    """

    id: uuid.UUID


class IngestResult(FrozenModel):
    """How many documents one ingest call wrote, and from where.

    count: documents stored, a file whose content hash already existed skipped.
    path: the file or directory ingested.
    """

    count: int
    path: str


class GraphBuildResult(FrozenModel):
    """How many entities and facts one graph build created.

    entities: entity content rows created.
    facts: fact content rows created.
    """

    entities: int
    facts: int


class DecayResult(FrozenModel):
    """How many stale facts one decay pass archived.

    archived: latest claims whose relevance fell below the floor and were closed out of the live
        graph, still readable through history.
    """

    archived: int


class ReembedResult(FrozenModel):
    """How many stored vectors one re-embed pass wrote.

    written: chunk, entity, fact, community, and profile embeddings re-encoded.
    """

    written: int


class RaptorBuildResult(FrozenModel):
    """How many RAPTOR summaries one build wrote across the levels above the leaves.

    written: summary entities created.
    """

    written: int


class PromoteResult(FrozenModel):
    """How many rows one promote call published, and into which scope set.

    promoted: document, chunk, and claim rows copied into the target scope.
    to_scopes: the target group set the copy was published into.
    """

    promoted: int
    to_scopes: str


class PendingFact(FrozenModel):
    """One curated group's unreviewed fact awaiting a group admin's approval.

    id: identity of the pending claim.
    owner_id: principal that authored the claim.
    predicate: ontology relation type the fact asserts.
    statement: self-contained natural-language rendering of the fact.
    """

    id: uuid.UUID
    owner_id: uuid.UUID
    predicate: str
    statement: str


class ReviewResult(FrozenModel):
    """How many of a curated group's pending facts one approve or reject call changed.

    group: name of the curated group the facts belong to.
    count: pending facts approved or rejected.
    """

    group: str
    count: int


class PrincipalSummary(FrozenModel):
    """One principal's identity and standing, the admin roster's own row shape.

    id: stable identity.
    display_name: human-readable label when one is known.
    is_admin: whether this principal manages the operational surface.
    """

    id: uuid.UUID
    display_name: str | None
    is_admin: bool


class GroupSummary(FrozenModel):
    """One group's visibility and member count, the sharing roster's own row shape.

    name: unique human-readable label.
    public: whether the group's rows are readable by anyone, member or not.
    members: how many principals belong to the group.
    """

    name: str
    public: bool
    members: int


class GroupCreated(FrozenModel):
    """A newly created group's id, the scope memberships and promotions target.

    id: identity of the new group.
    """

    id: uuid.UUID


class MembershipChange(FrozenModel):
    """The membership change one add or remove call made.

    principal: identity that joined or left the group.
    group: name of the group.
    role: standing granted, null once removed.
    """

    principal: uuid.UUID
    group: str
    role: str | None = None


class GroupFlag(FrozenModel):
    """A group's new public or curated flag after a flip.

    group: name of the group whose flag changed.
    public: the group's new public visibility, unset when this flip was a curation change.
    curated: the group's new curation gate, unset when this flip was a publish change.
    """

    group: str
    public: bool | None = None
    curated: bool | None = None


class GroupDeleted(FrozenModel):
    """A deleted group's name, memberships cascaded and its rows fallen back to their owners.

    group: name of the deleted group.
    """

    group: str


class ProfileReport(FrozenModel):
    """The process-wide `mainboard.profiling` span stats, the admin `profile_report` tool's read.

    stats: one row per dotted span path, slowest total first, empty when `settings.profiling`
        never turned span recording on.
    """

    stats: list[SpanStat]


class WriteRecord(FrozenModel):
    """One recent visible document write, the audit roster's own row shape.

    id: identity of the written document.
    kind: coarse type tag, note, code, image, or reference.
    owner_id: principal that wrote the document.
    scopes: group ids the document is shared with, empty when private.
    promoted_from: the source document this one was promoted from, null for an ordinary write.
    title: human-readable label when one is known.
    """

    id: uuid.UUID
    kind: str
    owner_id: uuid.UUID
    scopes: list[uuid.UUID]
    promoted_from: uuid.UUID | None
    title: str | None
