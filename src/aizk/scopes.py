import uuid

from .store import Group, acting_as


async def resolve_scopes(scopes: str | None, principal_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
    """Resolve a comma-separated group-name list to the sorted scope-set rows are shared with.

    The one scope-name bridge both the MCP write verbs and the operator CLI read through, so a
    name-to-id lookup lives in one place rather than duplicated across the two entrypoints. A null
    or blank string means private to the caller, so an empty tuple returns unchanged. Otherwise
    every named group resolves under the caller's own row level security, an unknown name failing
    fast rather than silently writing private, and the ids sort so `finance,business` and
    `business,finance` land on the identical canonical array every uniqueness and containment
    check depends on.

    scopes: comma-separated human-readable group names, null or blank for private.
    principal_id: identity whose visibility scopes the group lookups.
    """
    names = [name.strip() for name in (scopes or "").split(",") if name.strip()]
    if not names:
        return ()
    async with acting_as(principal_id) as session:
        ids = [(await Group.named(session, name)).id for name in names]
    return tuple(sorted(ids))
