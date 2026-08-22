import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import OAuthToken


def authorize_url(state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.tesla_client_id,
        "redirect_uri": settings.redirect_uri,
        "scope": settings.tesla_scopes,
        "state": state,
        "nonce": secrets.token_urlsafe(16),
        # Prévient l'utilisateur qu'une seconde étape (appairage de la clé
        # virtuelle) suit l'autorisation. Sans clé, ni commandes ni télémétrie.
        "show_keypair_step": "true",
    }
    return f"{settings.tesla_authorize_url}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            settings.tesla_token_url,
            data={
                "grant_type": "authorization_code",
                "client_id": settings.tesla_client_id,
                "client_secret": settings.tesla_client_secret,
                "code": code,
                "audience": settings.tesla_audience,
                "redirect_uri": settings.redirect_uri,
            },
        )
        response.raise_for_status()
        return response.json()


async def refresh(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            settings.tesla_token_url,
            data={
                "grant_type": "refresh_token",
                "client_id": settings.tesla_client_id,
                "refresh_token": refresh_token,
            },
        )
        response.raise_for_status()
        return response.json()


async def store_tokens(session: AsyncSession, subject: str, payload: dict) -> OAuthToken:
    expires_at = datetime.now(UTC) + timedelta(seconds=payload.get("expires_in", 28800))

    token = await session.scalar(select(OAuthToken).where(OAuthToken.subject == subject))
    if token is None:
        token = OAuthToken(subject=subject)
        session.add(token)

    token.access_token = payload["access_token"]
    token.refresh_token = payload["refresh_token"]
    token.expires_at = expires_at
    token.scopes = payload.get("scope", settings.tesla_scopes)

    await session.commit()
    return token


async def valid_access_token(session: AsyncSession, subject: str) -> str:
    """Renvoie un access token utilisable, en rafraîchissant si nécessaire.

    Le refresh token est à usage unique : le nouveau est écrit en base
    immédiatement. L'ancien reste accepté 24 h, ce qui laisse une marge si
    l'écriture échoue, mais on ne compte pas dessus.
    """
    token = await session.scalar(select(OAuthToken).where(OAuthToken.subject == subject))
    if token is None:
        raise LookupError(f"aucun jeton enregistré pour {subject!r} — refaire /auth/login")

    if token.expires_at > datetime.now(UTC) + timedelta(minutes=5):
        return token.access_token

    payload = await refresh(token.refresh_token)
    token = await store_tokens(session, subject, payload)
    return token.access_token
