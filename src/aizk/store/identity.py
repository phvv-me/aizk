import uuid

from ..config import settings

# aizk keeps no user, org, or membership table: identity lives in Logto, and a scoped row's
# `owner_id` and `scopes` are derived from Logto's own subject and organization ids rather than a
# local surrogate key. `uuid5` under one fixed namespace turns a Logto id into a stable uuid, so
# the whole scoped-table schema stays `uuid`/`uuid[]` and the mapping needs no lookup row: the same
# subject or org always yields the same uuid, and `store.events.bind_user` computes the acting
# user's own uuid and org set from the verified token the same way, per transaction, into the GUCs
# the row level security policies read.
NAMESPACE = uuid.UUID("a12c0de5-0000-4000-8000-000000000000")

# the reserved organization every session belongs to, member and anonymous alike, so a row scoped
# to it alone is world-readable with no `public` flag or table: `bind_user` folds it into every
# caller's `app.orgs`, and content is published by writing it into this org's scope.
PUBLIC_ORG = uuid.uuid5(NAMESPACE, "aizk:public")


def user_uuid(oidc_subject: str) -> uuid.UUID:
    """The stable owner uuid a Logto subject maps to, `owner_id` on every row that identity writes.

    oidc_subject: the `sub` claim of a verified token.
    """
    return uuid.uuid5(NAMESPACE, f"user:{oidc_subject}")


def org_uuid(oidc_org_id: str) -> uuid.UUID:
    """The stable scope uuid a Logto organization maps to, one element of a row's `scopes` set.

    oidc_org_id: a Logto organization id, from a token's org claim or an admin action.
    """
    return uuid.uuid5(NAMESPACE, f"org:{oidc_org_id}")


# the two reserved identities that predate any Logto subject: the system user that owns background
# and pre-lattice rows, and the anonymous reader. Both are fixed uuids from settings rather than
# derived, so `as_system` and an unauthenticated session bind them directly.
SYSTEM_USER = settings.system_user_id
ANONYMOUS_USER = settings.anonymous_user_id
