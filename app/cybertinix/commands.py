"""Écoute des messages Telegram entrants.

Permet d'interroger le système depuis le téléphone, sans SSH. Le bot reste le
seul point d'entrée, ce qui garde le projet cohérent avec son choix initial :
des notifications, pas d'interface.

Sécurité : seul le `chat_id` configuré est servi. Un identifiant de bot finit
toujours par circuler, et sans ce filtre n'importe qui pourrait apprendre où en
est le véhicule. Les messages venant d'ailleurs sont consommés puis ignorés.
"""

import asyncio
import logging

import httpx

from . import status
from .config import settings
from .db import SessionLocal
from .notify import send

log = logging.getLogger(__name__)

AIDE = """Commandes disponibles :

/etat — état complet du système et du véhicule
/aide — ce message"""


async def _handle(texte: str) -> None:
    commande = texte.strip().split()[0].lower().removesuffix("@bot")

    if commande in ("/etat", "/status"):
        async with SessionLocal() as session:
            await send(status.render(await status.collect(session)))
    elif commande in ("/aide", "/help", "/start"):
        await send(AIDE)
    else:
        await send(f"Commande inconnue : {commande}\n\n{AIDE}")


async def run() -> None:
    if not settings.telegram_commands_enabled:
        return
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.info("commandes Telegram inactives : bot non configuré")
        return

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/getUpdates"
    offset: int | None = None

    # On repart des messages en attente sans les traiter : au démarrage, rejouer
    # d'anciennes commandes n'aurait aucun sens.
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            réponse = await client.get(url, params={"timeout": 0})
            updates = réponse.json().get("result", [])
            if updates:
                offset = updates[-1]["update_id"] + 1
    except httpx.HTTPError:
        log.warning("impossible de purger les messages en attente")

    log.info("écoute des commandes Telegram")
    while True:
        try:
            # Long polling : la requête reste ouverte jusqu'à 25 s côté Telegram,
            # ce qui évite d'interroger l'API en boucle pour rien.
            async with httpx.AsyncClient(timeout=40) as client:
                params: dict = {"timeout": 25}
                if offset is not None:
                    params["offset"] = offset
                réponse = await client.get(url, params=params)
                updates = réponse.json().get("result", [])

            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                texte = message.get("text")
                expediteur = str((message.get("chat") or {}).get("id", ""))

                if not texte:
                    continue
                if expediteur != str(settings.telegram_chat_id):
                    log.warning("message ignoré, expéditeur non autorisé : %s", expediteur)
                    continue

                await _handle(texte)

        except httpx.HTTPError as exc:
            log.warning("écoute Telegram interrompue (%s), reprise dans 10 s", exc)
            await asyncio.sleep(10)
        except Exception:  # noqa: BLE001 — l'écoute ne doit jamais s'arrêter
            log.exception("erreur pendant le traitement d'une commande")
            await asyncio.sleep(5)
