"""Rattrapage des états que la télémétrie cesse d'émettre.

La télémétrie n'envoie un champ que lorsqu'il change — et le véhicule ne
signale pas toujours son re-verrouillage. Constaté le 23/08 : dernier `Locked`
à **12:38:41 (`false`)**, plus rien pendant deux heures sur une voiture
pourtant fermée. L'interface affichait « Déverrouillé » avec aplomb, et la
surveillance restait désarmée.

Déduire le verrou de la sentinelle armée dépannait, mais ne tenait plus dès que
la sentinelle était coupée. Ici on arrête de deviner : on demande son état au
véhicule.

Trois garde-fous, parce que cet appel est le plus cher de l'API :
  - **jamais de réveil** : `vehicle_data` échoue proprement sur une voiture
    endormie (`408`), et on s'en contente — une voiture qui dort n'a de toute
    façon pas changé d'état depuis qu'elle s'est endormie ;
  - **seulement si la valeur est périmée**, au-delà de `PERIME` ;
  - **au plus un appel par `INTERVALLE`**, succès ou échec.
"""

import json
import logging
import time

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from . import oauth
from .fleet import FleetClient
from .models import Signal

log = logging.getLogger(__name__)

SUBJECT = "default"
PERIME = 15 * 60.0
INTERVALLE = 5 * 60.0

_dernier_essai: dict[str, float] = {}


async def verrou_perime(session: AsyncSession, vin: str) -> bool:
    from .security import _dernier_date

    _, quand = await _dernier_date(session, vin, "Locked")
    if quand is None:
        return True
    from datetime import UTC, datetime

    return (datetime.now(UTC) - quand).total_seconds() > PERIME


async def rafraichir_verrou(session: AsyncSession, vin: str) -> bool:
    """Redemande `locked` et les ouvrants au véhicule. True si la base a bougé.

    Les valeurs obtenues sont écrites comme des signaux ordinaires : tout ce
    qui lit la télémétrie — affichage, armement, détecteurs — en profite sans
    connaître ce rattrapage.
    """
    maintenant = time.monotonic()
    precedent = _dernier_essai.get(vin)
    if precedent is not None and maintenant - precedent < INTERVALLE:
        return False
    _dernier_essai[vin] = maintenant

    try:
        token = await oauth.valid_access_token(session, SUBJECT)
        reponse = await FleetClient(token).vehicle_data(vin, endpoints="vehicle_state")
    except httpx.HTTPStatusError as exc:
        # 408 = véhicule endormi. Ce n'est pas une panne : c'est la réponse.
        niveau = log.info if exc.response.status_code == 408 else log.warning
        niveau("état du verrou non rafraîchi (%s)", exc.response.status_code)
        return False
    except httpx.HTTPError as exc:
        log.warning("état du verrou non rafraîchi : %s", exc)
        return False

    etat = ((reponse.get("response") or {}).get("vehicle_state")) or {}
    if "locked" not in etat:
        return False

    session.add(Signal(vin=vin, name="Locked", value=json.dumps(bool(etat["locked"]))))

    # Les ouvrants viennent du même relevé : autant les remettre d'aplomb.
    ouvrants = {
        "DriverFront": etat.get("df"), "PassengerFront": etat.get("pf"),
        "DriverRear": etat.get("dr"), "PassengerRear": etat.get("pr"),
        "TrunkFront": etat.get("ft"), "TrunkRear": etat.get("rt"),
    }
    if all(v is not None for v in ouvrants.values()):
        session.add(Signal(vin=vin, name="DoorState",
                           value=json.dumps({k: bool(v) for k, v in ouvrants.items()})))

    await session.commit()
    log.info("verrou rafraîchi auprès de Tesla : %s", etat["locked"])
    return True
