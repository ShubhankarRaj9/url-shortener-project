from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logging import get_logger
from app.models import User
from app.security import hash_password

log = get_logger(__name__)
_SEED_LOCK_ID = 7_421_981  # arbitrary, shared by all replicas


async def seed_admin(settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    """Create the initial admin when (and only when) there are no users yet."""
    async with sessionmaker() as session, session.begin():
        # Serialise concurrent replicas starting at the same time.
        await session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _SEED_LOCK_ID})
        if await session.scalar(select(func.count()).select_from(User)):
            return False
        if not settings.admin_email or not settings.admin_password:
            log.warning("seed.admin_skipped", reason="ADMIN_EMAIL / ADMIN_PASSWORD not set and no users exist")
            return False
        email = str(settings.admin_email).lower()
        session.add(
            User(
                email=email,
                password_hash=await hash_password(settings, settings.admin_password.get_secret_value()),
                full_name="Administrator",
                role="admin",
                is_verified=True,
            )
        )
    log.info("seed.admin_created", email=email)
    return True
