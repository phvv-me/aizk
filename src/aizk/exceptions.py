class NoTenantContext(RuntimeError):
    """A scoped table was queried without a user in context, so the read was refused."""


class ScopeNotFoundError(ValueError):
    """The requested write destination is unknown or outside the caller's authority."""


class NotVisibleError(ValueError):
    """A row exists but sits outside the acting user's row level security visibility."""


class OntologyError(ValueError):
    """An ontology operation that violates a structural invariant, such as deactivating a
    system- written entity type or relation."""


class OntologyNotReadyError(RuntimeError):
    """The ontology cache was read before `ops.setup()` ever refreshed it."""
