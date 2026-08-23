"""Commandes en attente d'un véhicule éveillé.

Une commande envoyée à une voiture endormie échoue avec « vehicle unavailable ».
Deux réponses, dans l'ordre :

  1. **Réveiller et réessayer.** `wake_up` est l'appel le plus cher de l'API
     (limité à 3/min) mais c'est ce que fait l'application Tesla elle-même.
     On patiente jusqu'à 45 s que la voiture réponde.
  2. **Mettre en file.** Si elle ne se réveille pas — pas de réseau dans le
     parking — la commande est conservée et rejouée au prochain événement de
     connectivité, avec un Telegram pour dire ce qui s'est passé.

La file vit dans `alert_states` (clé `file:<n>`), donc survit à un redémarrage.
Une commande en file expire après 12 h : une clim demandée hier n'a plus
de sens ce matin.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import actions, oauth
from .db import SessionLocal
from .fleet import FleetClient
from .models import AlertState
from .notify import send

log = logging.getLogger(__name__)

SUBJECT = "default"
PREFIXE = "file:"
EXPIRATION = timedelta(hours=12)
ATTENTE_REVEIL = 45  # secondes
_INDISPONIBLE = ("vehicle unavailable", "vehicle_unavailable", "asleep", "offline", "timeout")


def indisponible(exc: httpx.HTTPStatusError) -> bool:
    texte = exc.response.text.lower()
    return exc.response.status_code == 408 or any(m in texte for m in _INDISPONIBLE)


async def envoyer(client: FleetClient, vin: str, action: actions.Action, corps: dict) -> dict:
    fn = client.command_direct if action.direct else client.command
    return await fn(vin, action.commande, corps)


async def reveiller_et_reessayer(client: FleetClient, vin: str, action: actions.Action, corps: dict) -> dict | None:
    """Réveille la voiture puis réessaie jusqu'à ATTENTE_REVEIL. None si elle
    ne répond toujours pas."""
    try:
        await client.wake_up(vin)
    except httpx.HTTPStatusError as exc:
        log.warning("wake_up refusé : %s", exc.response.text[:200])
        return None
    echeance = asyncio.get_event_loop().time() + ATTENTE_REVEIL
    while asyncio.get_event_loop().time() < echeance:
        await asyncio.sleep(5)
        try:
            return await envoyer(client, vin, action, corps)
        except httpx.HTTPStatusError as exc:
            if not indisponible(exc):
                raise
    return None


async def mettre_en_file(session: AsyncSession, vin: str, cle: str, parametre: str | None) -> int:
    n = len(await en_attente(session, vin)) + 1
    session.add(AlertState(
        vin=vin, key=f"{PREFIXE}{cle}",
        state=json.dumps({"cle": cle, "parametre": parametre,
                          "quand": datetime.now(timezone.utc).isoformat()}),
    ))
    await session.commit()
    return n


async def en_attente(session: AsyncSession, vin: str) -> list[dict]:
    lignes = (await session.scalars(
        select(AlertState).where(AlertState.vin == vin, AlertState.key.like(f"{PREFIXE}%"))
    )).all()
    out = []
    for l in lignes:
        try:
            d = json.loads(l.state)
        except (TypeError, ValueError):
            continue
        d["id"] = l.id
        d["libelle"] = getattr(actions.PAR_CLE.get(d.get("cle")), "libelle", d.get("cle"))
        out.append(d)
    return out


async def vider(session: AsyncSession, vin: str) -> int:
    r = await session.execute(delete(AlertState).where(AlertState.vin == vin, AlertState.key.like(f"{PREFIXE}%")))
    await session.commit()
    return r.rowcount


async def rejouer(vin: str) -> None:
    """Appelé quand la voiture se connecte : envoie ce qui attendait."""
    async with SessionLocal() as session:
        attente = await en_attente(session, vin)
        if not attente:
            return
        token = await oauth.valid_access_token(session, SUBJECT)
        client = FleetClient(token)
        maintenant = datetime.now(timezone.utc)
        for d in attente:
            await session.execute(delete(AlertState).where(AlertState.id == d["id"]))
            await session.commit()
            action = actions.PAR_CLE.get(d["cle"])
            if action is None:
                continue
            quand = datetime.fromisoformat(d["quand"])
            if maintenant - quand > EXPIRATION:
                await send(f"⏳ « {action.libelle} » attendait depuis plus de 12 h — abandonnée.")
                continue
            try:
                corps = actions.corps_pour(action, d.get("parametre"))
                await envoyer(client, vin, action, corps)
                await send(f"✅ « {action.libelle} » envoyée au réveil de la voiture.")
            except (httpx.HTTPError, ValueError) as exc:
                detail = getattr(getattr(exc, "response", None), "text", str(exc))[:200]
                await send(f"❌ « {action.libelle} » a échoué au réveil : {detail}")
