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
    """An ontology operation that violates a structural invariant, such as deactivating a
    system-written entity type or relation."""


class OntologyNotReadyError(RuntimeError):
    """The ontology cache was read before `ops.setup()` ever refreshed it.

    `setup` runs at server start, worker start, and test bootstrap alike, so reaching this
    genuinely means a missed bootstrap, not a race worth papering over with a lazy default.
    """


class NotGroupAdminError(PermissionError):
    """A principal with neither group-admin nor server-admin standing tried a curation tool."""


class ExtractionUnreachableError(RuntimeError):
    """The graph-extraction chat endpoint refused the connection, not merely one slow call.

    Raised instead of grinding through every remaining pending chunk against a dead endpoint, one
    `APITimeoutError` per call; names `AIZK_LLM_URL` and the opt-in `vllm-llm` compose profile so
    the fix is the error message itself rather than a stack trace into httpx.
    """
