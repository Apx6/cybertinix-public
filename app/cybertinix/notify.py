import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)


async def send(text: str) -> None:
    """Envoie une notification Telegram.

    Un échec ne doit jamais interrompre l'ingestion : on journalise et on
    continue. Perdre une notification est moins grave que perdre le flux.
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.info("[notification non configurée] %s", text)
        return

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url, json={"chat_id": settings.telegram_chat_id, "text": text}
            )
            response.raise_for_status()
    except httpx.HTTPError:
        log.exception("échec de l'envoi Telegram : %s", text)
