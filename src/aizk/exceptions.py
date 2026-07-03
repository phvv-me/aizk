class NoTenantContext(RuntimeError):
    """A scoped table was queried without a principal in context, so the read was refused.

    The database already fails closed to NULL when app.uid is unset, since the scope predicate
    then matches no row, so this raise is defense in depth that turns a forgotten `acting_as` into
    a loud error at the call site instead of a silently empty result.
    """


class ScopeNotFoundError(ValueError):
    """No group is visible under the given name."""


class NotVisibleError(ValueError):
    """A row exists but sits outside the acting principal's row level security visibility."""


class OntologyError(ValueError):
    """A predicate or entity type outside the closed extraction ontology."""


class NotGroupAdminError(PermissionError):
    """A principal with neither group-admin nor server-admin standing tried a curation tool."""
