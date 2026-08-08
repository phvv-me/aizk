from patos import FrozenModel

from ..config import settings
from ..store.identity import User


class AdminLinks(FrozenModel):
    """The three external tools the operator sidebar links to, read from server config.

    The console origin is configurable, so the browser reads these instead of a hardcoded
    href. The default values are the same relative paths Caddy already answers on the
    operator console's own host, `/logto`, `/grafana/`, and `/traces`.
    """

    logto_url: str
    grafana_url: str
    traces_url: str

    @classmethod
    def current(cls) -> AdminLinks:
        """Read the configured console tool URLs."""
        return cls(
            logto_url=settings.admin_logto_console_url,
            grafana_url=settings.admin_grafana_url,
            traces_url=settings.admin_traces_url,
        )


def require_admin(user: User) -> None:
    """Refuse a caller without the managed operator role.

    Reads the same standing the caller carries into PostgreSQL rather than re-deriving it
    from the role list, so this answer and row security can never disagree about who is an
    operator. Raises `PermissionError`, which the API's shared exception handling already
    maps to 403.
    """
    if not user.operator:
        raise PermissionError("operator access required")
