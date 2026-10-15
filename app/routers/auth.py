from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app import emailer, metrics
from app.deps import CurrentUser, RedisDep, SessionDep, SettingsDep, client_ip
from app.logging import get_logger
from app.models import AuthToken, User
from app.schemas import (
    ChangePasswordRequest,
    EmailRequest,
    MessageOut,
    RegisterRequest,
    ResetPasswordRequest,
    TokenOut,
    TokenRequest,
)
from app.security import create_access_token, hash_password, verify_password
from app.services import ratelimit
from app.services.tokens import RESET_PASSWORD, VERIFY_EMAIL, consume_token, issue_token

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
log = get_logger(__name__)

# Identical answers for existing / unknown addresses, so the API can't be used to enumerate accounts.
_REGISTERED = MessageOut(message="If this address can be registered, a verification email is on its way.")
_VERIFICATION_SENT = MessageOut(message="If the account exists and is unverified, a new verification email is on its way.")
_RESET_SENT = MessageOut(message="If an account exists for this address, a password reset email is on its way.")
_BAD_TOKEN = HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired token")


async def _get_by_email(session, email: str) -> User | None:
    return (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()


@router.post("/register", response_model=MessageOut, status_code=status.HTTP_202_ACCEPTED, summary="Create an account")
async def register(
    body: RegisterRequest, background: BackgroundTasks, session: SessionDep, settings: SettingsDep
) -> MessageOut:
    """Creates an unverified account and e-mails a verification token."""
    email = body.email.lower()
    user = User(
        email=email,
        password_hash=await hash_password(settings, body.password),
        full_name=body.full_name,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        log.info("auth.register_duplicate")
        return _REGISTERED
    raw = await issue_token(session, user.id, VERIFY_EMAIL, timedelta(hours=settings.verify_token_ttl_hours))
    await session.commit()
    metrics.USERS_REGISTERED.inc()
    background.add_task(emailer.send_verification_email, settings, email, raw)
    log.info("auth.registered", user_id=str(user.id))
    return _REGISTERED


@router.post("/verify-email", response_model=MessageOut, summary="Confirm an email address")
async def verify_email(body: TokenRequest, session: SessionDep) -> MessageOut:
    user_id = await consume_token(session, body.token, VERIFY_EMAIL)
    if user_id is None:
        raise _BAD_TOKEN
    user = await session.get(User, user_id)
    if user is None:
        raise _BAD_TOKEN
    user.is_verified = True
    await session.commit()
    log.info("auth.email_verified", user_id=str(user.id))
    return MessageOut(message="Email verified. You can now log in.")


@router.post("/resend-verification", response_model=MessageOut, status_code=status.HTTP_202_ACCEPTED)
async def resend_verification(
    body: EmailRequest, background: BackgroundTasks, session: SessionDep, redis: RedisDep, settings: SettingsDep
) -> MessageOut:
    email = body.email.lower()
    user = await _get_by_email(session, email)
    if user and user.is_active and not user.is_verified and await ratelimit.allow_once(redis, "verify", email, 60):
        raw = await issue_token(session, user.id, VERIFY_EMAIL, timedelta(hours=settings.verify_token_ttl_hours))
        await session.commit()
        background.add_task(emailer.send_verification_email, settings, email, raw)
    return _VERIFICATION_SENT


@router.post(
    "/login",
    response_model=TokenOut,
    summary="Log in",
    description="OAuth2 password flow: send the email address in the `username` field. Powers the Authorize button in Swagger UI.",
)
async def login(
    request: Request,
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> TokenOut:
    email = form.username.strip().lower()
    ip = client_ip(request)

    wait = await ratelimit.login_lock_remaining(redis, settings, email, ip)
    if wait:
        metrics.LOGINS.labels("locked").inc()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed attempts. Try again later.",
            headers={"Retry-After": str(wait)},
        )

    user = await _get_by_email(session, email)
    # Always run bcrypt, even for unknown users, to keep response times uniform.
    password_ok = await verify_password(
        form.password, user.password_hash if user else request.app.state.dummy_hash
    )
    if not user or not password_ok:
        await ratelimit.login_failed(redis, settings, email, ip)
        metrics.LOGINS.labels("failure").inc()
        log.info("auth.login_failed", ip=ip)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Incorrect email or password", headers={"WWW-Authenticate": "Bearer"}
        )
    if not user.is_active:
        metrics.LOGINS.labels("failure").inc()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been disabled")
    if not user.is_verified:
        metrics.LOGINS.labels("unverified").inc()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Email address not verified")

    await ratelimit.login_succeeded(redis, email, ip)
    user.last_login_at = datetime.now(timezone.utc)
    await session.commit()
    token, expires_in = create_access_token(settings, user_id=str(user.id), token_version=user.token_version)
    metrics.LOGINS.labels("success").inc()
    log.info("auth.login", user_id=str(user.id))
    return TokenOut(access_token=token, expires_in=expires_in)


@router.post("/forgot-password", response_model=MessageOut, status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    body: EmailRequest, background: BackgroundTasks, session: SessionDep, redis: RedisDep, settings: SettingsDep
) -> MessageOut:
    email = body.email.lower()
    user = await _get_by_email(session, email)
    if user and user.is_active and await ratelimit.allow_once(redis, "reset", email, 60):
        raw = await issue_token(session, user.id, RESET_PASSWORD, timedelta(minutes=settings.reset_token_ttl_minutes))
        await session.commit()
        background.add_task(emailer.send_password_reset_email, settings, email, raw)
    return _RESET_SENT


@router.post("/reset-password", response_model=MessageOut, summary="Set a new password using an emailed token")
async def reset_password(body: ResetPasswordRequest, session: SessionDep, settings: SettingsDep) -> MessageOut:
    user_id = await consume_token(session, body.token, RESET_PASSWORD)
    user = await session.get(User, user_id) if user_id else None
    if user is None or not user.is_active:
        await session.rollback()
        raise _BAD_TOKEN
    user.password_hash = await hash_password(settings, body.new_password)
    user.token_version += 1  # invalidates every JWT issued before the reset
    user.is_verified = True  # they just proved control of the mailbox
    await session.execute(delete(AuthToken).where(AuthToken.user_id == user.id, AuthToken.purpose == RESET_PASSWORD))
    await session.commit()
    log.info("auth.password_reset", user_id=str(user.id))
    return MessageOut(message="Password updated. Please log in again.")


@router.post("/change-password", response_model=MessageOut, summary="Change password (logged in)")
async def change_password(body: ChangePasswordRequest, user: CurrentUser, session: SessionDep, settings: SettingsDep) -> MessageOut:
    if not await verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    user.password_hash = await hash_password(settings, body.new_password)
    user.token_version += 1
    await session.commit()
    log.info("auth.password_changed", user_id=str(user.id))
    return MessageOut(message="Password updated. Please log in again.")
