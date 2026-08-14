from ..config import settings
from . import verify_rls
from .backend import DatabaseRole, database_adapter


async def row_security_violations() -> list[str]:
    """Report live row security differences from the mapped catalog."""
    admin = database_adapter().engine(settings.admin_database_url, DatabaseRole.owner)
    try:
        async with admin.connect() as connection:
            return await connection.run_sync(verify_rls)
    finally:
        await admin.dispose()
