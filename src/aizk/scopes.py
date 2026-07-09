import uuid

from .store.identity import org_uuid


def scopes_from_org_ids(scopes: str | None) -> tuple[uuid.UUID, ...]:
    """Resolve a comma-separated Logto org-id list to the sorted scope set an operator write into.

    The operator-side scope resolver, counterpart to the MCP caller's `mcp.user.User.scope_ids`
    which resolves org names out of the caller's own token. An operator on the box carries no
    token, so it names the target organizations by their stable Logto ids, each mapped to its
    `uuid5` scope with no local group table to look a name up in. A null or blank string means
    private to the owner, an empty tuple. The ids sort so `a,b` and `b,a` land on the identical
    canonical array every uniqueness and containment check depends on.

    scopes: comma-separated Logto organization ids, null or blank for private.
    """
    ids = [one.strip() for one in (scopes or "").split(",") if one.strip()]
    return tuple(sorted(org_uuid(one) for one in ids))
