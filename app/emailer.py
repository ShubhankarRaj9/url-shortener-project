from __future__ import annotations

from email.message import EmailMessage

import aiosmtplib

from app.config import Settings
from app.logging import get_logger
from app.metrics import EMAILS

log = get_logger(__name__)

# Used by the "memory" backend (tests).
OUTBOX: list[dict] = []


async def send_email(settings: Settings, to: str, subject: str, body: str) -> None:
    """Never raises: this runs as a background task and must not break requests."""
    try:
        if settings.email_backend == "memory":
            OUTBOX.append({"to": to, "subject": subject, "body": body})
        elif settings.email_backend == "log":
            # Bodies contain secrets (tokens), so only log them outside production.
            extra = {} if settings.environment == "production" else {"body": body}
            log.info("email.suppressed", to=to, subject=subject, **extra)
        else:
            msg = EmailMessage()
            msg["From"] = settings.email_from
            msg["To"] = to
            msg["Subject"] = subject
            msg.set_content(body)
            await aiosmtplib.send(
                msg,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_username,
                password=settings.smtp_password.get_secret_value() if settings.smtp_password else None,
                start_tls=settings.smtp_security == "starttls",
                use_tls=settings.smtp_security == "tls",
                timeout=15,
            )
        EMAILS.labels("sent").inc()
    except Exception:
        EMAILS.labels("failed").inc()
        log.exception("email.failed", to=to, subject=subject)


async def send_verification_email(settings: Settings, to: str, token: str) -> None:
    link = f"{settings.web_url}/verify-email?token={token}"
    await send_email(
        settings,
        to,
        f"Verify your email for {settings.app_name}",
        "Welcome!\n\n"
        f"Confirm your email address by opening this link (valid for {settings.verify_token_ttl_hours}h):\n"
        f"{link}\n\n"
        f"Or submit this token to POST /api/v1/auth/verify-email:\n{token}\n\n"
        "If you did not create an account you can ignore this message.\n",
    )


async def send_password_reset_email(settings: Settings, to: str, token: str) -> None:
    link = f"{settings.web_url}/reset-password?token={token}"
    await send_email(
        settings,
        to,
        f"Reset your {settings.app_name} password",
        f"Use this link to choose a new password (valid for {settings.reset_token_ttl_minutes} minutes):\n"
        f"{link}\n\n"
        f"Or submit this token to POST /api/v1/auth/reset-password:\n{token}\n\n"
        "If you did not request a reset you can ignore this message; your password is unchanged.\n",
    )
