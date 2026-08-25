"""Historique des déplacements.

Aucun champ supplémentaire n'est demandé au véhicule : `Gear` borne le
trajet, `Location`, `Odometer` et `Soc` sont déjà dans le flux. Un trajet
s'ouvre quand le levier quitte P et se ferme à son retour — un arrêt au feu
ne le coupe pas, la vitesse n'est pas regardée.

Le trajet en cours est repérable par `ended_at` nul. Si l'application
redémarre pendant un déplacement, l'arrivée suivante ferme normalement le
trajet resté ouvert : on ne perd que ce qui n'a jamais été reçu.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import enums, geocode, prefs
from .models import Trip

log = logging.getLogger(__name__)

# En deçà, ce n'est pas un déplacement : une manœuvre dans l'allée, un
# changement de place. Gardé tout de même s'il a duré plus d'une minute.
DISTANCE_MIN_KM = 0.2


def _coords(brut: Any) -> tuple[float | None, float | None]:
    if not isinstance(brut, dict):
        return None, None
    lat = brut.get("latitude", brut.get("Latitude", brut.get("lat")))
    lon = brut.get("longitude", brut.get("Longitude", brut.get("lon")))
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None


def _nombre(valeur: Any) -> float | None:
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return None


async def _rue(session: AsyncSession, lat: float | None, lon: float | None) -> str:
    if lat is None or lon is None:
        return ""
    return await geocode.rue(session, lat, lon) or ""


async def en_cours(session: AsyncSession, vin: str) -> Trip | None:
    return await session.scalar(
        select(Trip).where(Trip.vin == vin, Trip.ended_at.is_(None)).order_by(Trip.id.desc()).limit(1)
    )


async def demarrer(session: AsyncSession, vin: str, *, position: Any, odometre: Any, soc: Any) -> Trip:
    """Ouvre un trajet. Un trajet resté ouvert est fermé d'abord, sans fin
    connue : mieux vaut un trajet tronqué que deux fusionnés."""
    ouvert = await en_cours(session, vin)
    if ouvert is not None:
        ouvert.ended_at = ouvert.started_at
        await session.commit()
        log.warning("trajet %s resté ouvert, clos sans arrivée", ouvert.id)

    lat, lon = _coords(position)
    trajet = Trip(
        vin=vin,
        started_at=datetime.now(timezone.utc),
        start_lat=lat, start_lon=lon,
        start_street=await _rue(session, lat, lon),
        start_odometer=_nombre(odometre),
        start_soc=_nombre(soc),
    )
    session.add(trajet)
    # Validation explicite : l'ingestion valide sa transaction *avant*
    # d'appeler les règles, donc rien de ce qu'elles ajoutent ne survivrait à
    # la fermeture de la session. C'est ce qui a rendu l'historique muet du
    # 23 au 25/08 — le code paraissait juste et ne levait aucune erreur.
    # Même convention que `Context.remember` : qui écrit, valide.
    await session.commit()
    return trajet


async def terminer(session: AsyncSession, vin: str, *, position: Any, odometre: Any, soc: Any) -> Trip | None:
    trajet = await en_cours(session, vin)
    if trajet is None:
        return None

    lat, lon = _coords(position)
    trajet.ended_at = datetime.now(timezone.utc)
    trajet.end_lat, trajet.end_lon = lat, lon
    trajet.end_street = await _rue(session, lat, lon)
    trajet.end_odometer = _nombre(odometre)
    trajet.end_soc = _nombre(soc)

    km = _distance_km(trajet)
    duree = (trajet.ended_at - trajet.started_at).total_seconds()
    if km is not None and km < DISTANCE_MIN_KM and duree < 60:
        await session.delete(trajet)
        await session.commit()
        log.info("manœuvre de %.2f km ignorée", km)
        return None
    await session.commit()
    return trajet


def _distance_km(t: Trip) -> float | None:
    if t.start_odometer is None or t.end_odometer is None:
        return None
    return max(0.0, enums.en_km(t.end_odometer - t.start_odometer))


async def recents(session: AsyncSession, *, limite: int = 30) -> list[dict]:
    lignes = (await session.scalars(select(Trip).order_by(Trip.id.desc()).limit(limite))).all()
    unite = prefs.get("distance_unit")
    out = []
    for t in lignes:
        km = _distance_km(t)
        distance = None if km is None else (km / enums.MILES_EN_KM if unite == "mi" else km)
        duree = None if t.ended_at is None else int((t.ended_at - t.started_at).total_seconds())
        out.append({
            "id": t.id,
            "depart": t.started_at.isoformat(),
            "arrivee": t.ended_at.isoformat() if t.ended_at else None,
            "duree_s": duree,
            "distance": None if distance is None else round(distance, 1),
            "unite": unite,
            "de": {"rue": t.start_street, "lat": t.start_lat, "lon": t.start_lon},
            "a": {"rue": t.end_street, "lat": t.end_lat, "lon": t.end_lon},
            "soc_depart": t.start_soc,
            "soc_arrivee": t.end_soc,
            "conso_pct": None if t.start_soc is None or t.end_soc is None else round(t.start_soc - t.end_soc, 1),
        })
    return out


async def vider(session: AsyncSession) -> int:
    resultat = await session.execute(delete(Trip))
    return resultat.rowcount
