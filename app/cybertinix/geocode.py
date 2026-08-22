"""Nom de rue à partir des coordonnées, via Nominatim (OpenStreetMap).

⚠️ Cela transmet la position du véhicule à un service tiers. C'est le seul
appel sortant du projet vers autre chose que Tesla ou Telegram. Trois garde-fous
en découlent :

  - **Réglage désactivable** : `reverse_geocode` dans les préférences.
  - **Cache agressif** : la position est arrondie à ~11 m avant interrogation,
    et le résultat conservé. Une voiture garée au même endroit n'interroge
    Nominatim qu'une fois, pas à chaque ouverture de la page.
  - **Appel côté serveur** : le navigateur ne contacte jamais Nominatim
    directement, ce qui éviterait le cache et exposerait l'adresse IP du
    téléphone en plus de la position.

La politique d'usage de Nominatim impose un `User-Agent` identifiant et limite
à une requête par seconde. Le cache nous place très en deçà.
"""

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import prefs
from .models import AlertState

log = logging.getLogger(__name__)

URL = "https://nominatim.openstreetmap.org/reverse"
AGENT = "CyberTinix/1.0 (auto-hébergé, usage personnel)"

CACHE = "_geocode"


def _cle(lat: float, lon: float) -> str:
    """Arrondi à quatre décimales, soit environ 11 mètres.

    Assez fin pour distinguer deux rues, assez grossier pour que le bruit GPS
    d'une voiture à l'arrêt ne déclenche pas une nouvelle requête.
    """
    return f"{lat:.4f},{lon:.4f}"


async def rue(session: AsyncSession, lat: float, lon: float) -> str | None:
    if not prefs.get("reverse_geocode"):
        return None

    cle = _cle(lat, lon)
    cache = await session.scalar(
        select(AlertState).where(AlertState.vin == CACHE, AlertState.key == cle)
    )
    if cache is not None:
        return cache.state or None

    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": AGENT}) as client:
            reponse = await client.get(
                URL,
                params={
                    "lat": lat,
                    "lon": lon,
                    "format": "jsonv2",
                    "zoom": 17,  # niveau rue
                    "accept-language": "fr",
                },
            )
            reponse.raise_for_status()
            data = reponse.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("géocodage inverse indisponible : %s", exc)
        return None

    adresse = data.get("address") or {}
    morceaux = [
        adresse.get("road") or adresse.get("pedestrian") or adresse.get("neighbourhood"),
        adresse.get("village") or adresse.get("town") or adresse.get("city"),
    ]
    libelle = ", ".join(m for m in morceaux if m) or data.get("display_name", "")

    # Un résultat vide est mémorisé aussi : sans cela, une position en pleine
    # campagne relancerait une requête à chaque affichage.
    session.add(AlertState(vin=CACHE, key=cle, state=libelle))
    await session.commit()

    return libelle or None
