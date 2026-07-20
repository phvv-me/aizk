import sys

import httpx
from cyclopts import App
from pydantic import ValidationError

from .client import ProtocolError
from .commands import admin_app, auth_app, recall, remember, share, status

app = App(
    name="aizk",
    help="Use an AIZK memory server or operate one through the explicit admin boundary.",
)
app.command(auth_app)
app.command(admin_app)
app.command(recall)
app.command(remember)
app.command(share)
app.command(status)


def main() -> None:
    """Run the command tree with concise errors for expected operator mistakes."""
    try:
        app()
    except (
        FileNotFoundError,
        PermissionError,
        ProtocolError,
        ValidationError,
        ValueError,
        httpx.HTTPError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from None
