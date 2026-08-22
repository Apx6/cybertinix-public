"""Planification de charge en heures creuses.

On s'appuie sur la planification **native** du véhicule plutôt que de piloter
la charge nous-mêmes avec `charge_start` / `charge_stop`. Trois raisons :

  - Elle est programmée une fois et vit dans la voiture. Aucun appel récurrent,
    donc aucun coût récurrent.
  - Elle continue de fonctionner si le serveur tombe. Un pilotage maison qui
    lance la charge à 22 h mais meurt avant 6 h laisserait la voiture charger
    en heures pleines toute la nuit.
  - Elle n'exige pas que le véhicule soit réveillé au bon moment.

Une planification est liée à un lieu (`lat`/`lon` obligatoires) : elle ne
s'applique qu'à proximité, ce qui évite qu'elle ne se déclenche en déplacement.
"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Signal

log = logging.getLogger(__name__)

JOURS = {
    "lundi": "Monday",
    "mardi": "Tuesday",
    "mercredi": "Wednesday",
    "jeudi": "Thursday",
    "vendredi": "Friday",
    "samedi": "Saturday",
    "dimanche": "Sunday",
    "tous": "All",
    "semaine": "Weekdays",
}


def minutes_depuis_minuit(heure: str) -> int:
    """« 22:30 » -> 1350. L'API compte en minutes écoulées depuis minuit."""
    try:
        h, m = heure.strip().split(":")
        total = int(h) * 60 + int(m)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"heure invalide : {heure!r}, attendu HH:MM") from exc
    if not 0 <= total < 1440:
        raise ValueError(f"heure hors bornes : {heure!r}")
    return total


def jours_api(jours: str) -> str:
    """Accepte le français et le laisse passer tel quel s'il est déjà en anglais."""
    parts = [p.strip().lower() for p in jours.split(",") if p.strip()]
    return ",".join(JOURS.get(p, p.capitalize()) for p in parts)


async def derniere_position(session: AsyncSession, vin: str) -> tuple[float, float] | None:
    """Dernière position connue, issue de la télémétrie.

    Sert à déduire le domicile sans appel facturé ni réveil du véhicule :
    si la voiture est garée chez toi, sa dernière position *est* le domicile.
    """
    brut = await session.scalar(
        select(Signal.value)
        .where(Signal.vin == vin, Signal.name == "Location")
        .order_by(Signal.id.desc())
        .limit(1)
    )
    if brut is None:
        return None

    try:
        valeur = json.loads(brut)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(valeur, dict):
        return None

    # Tesla ne garantit pas le nom des clés d'une version de firmware à l'autre.
    for cle_lat, cle_lon in (
        ("latitude", "longitude"),
        ("Latitude", "Longitude"),
        ("lat", "lon"),
        ("lat", "long"),
    ):
        if cle_lat in valeur and cle_lon in valeur:
            try:
                return float(valeur[cle_lat]), float(valeur[cle_lon])
            except (TypeError, ValueError):
                return None
    return None


def build_schedule(
    *,
    lat: float,
    lon: float,
    debut: str | None,
    fin: str | None,
    jours: str = "tous",
    enabled: bool = True,
    one_time: bool = False,
    schedule_id: int | None = None,
) -> dict:
    """Corps de la commande `add_charge_schedule`.

    `debut` à None laisse le véhicule libre de commencer quand il veut ;
    `fin` à None le laisse charger jusqu'au bout. Pour des heures creuses on
    veut généralement les deux bornes : commencer à 22 h *et* s'arrêter à 6 h,
    sinon une charge longue déborderait en heures pleines.
    """
    if debut is None and fin is None:
        raise ValueError("il faut au moins une borne : début, fin, ou les deux")

    corps: dict = {
        "days_of_week": jours_api(jours),
        "enabled": enabled,
        "lat": lat,
        "lon": lon,
        "start_enabled": debut is not None,
        "end_enabled": fin is not None,
    }
    if debut is not None:
        corps["start_time"] = minutes_depuis_minuit(debut)
    if fin is not None:
        corps["end_time"] = minutes_depuis_minuit(fin)
    if one_time:
        corps["one_time"] = True
    if schedule_id is not None:
        # Sans cet identifiant, l'API crée une planification supplémentaire au
        # lieu de modifier l'existante — on se retrouve vite avec des doublons.
        corps["id"] = schedule_id

    return corps


def build_precondition(
    *,
    lat: float,
    lon: float,
    heure: str,
    jours: str = "semaine",
    enabled: bool = True,
    one_time: bool = False,
    schedule_id: int | None = None,
) -> dict:
    """Corps de la commande `add_precondition_schedule`.

    `heure` est l'instant où le préconditionnement doit être **terminé**, pas
    celui où il commence : la voiture calcule son avance seule, en fonction de
    la température extérieure. Programmer 8:00 signifie « habitacle tempéré à
    8:00 », pas « démarre à 8:00 ».

    Préconditionner branché consomme le réseau plutôt que la batterie — d'où
    l'intérêt de le combiner avec la charge en heures creuses.
    """
    corps: dict = {
        "days_of_week": jours_api(jours),
        "enabled": enabled,
        "lat": lat,
        "lon": lon,
        "precondition_time": minutes_depuis_minuit(heure),
    }
    if one_time:
        corps["one_time"] = True
    if schedule_id is not None:
        corps["id"] = schedule_id
    return corps


def decrire_precondition(corps: dict) -> str:
    minutes = corps["precondition_time"]
    return f"prêt à {minutes // 60:02d}:{minutes % 60:02d} — {corps['days_of_week']}"


def decrire(corps: dict) -> str:
    def hhmm(minutes: int) -> str:
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    morceaux = []
    if corps.get("start_enabled"):
        morceaux.append(f"début {hhmm(corps['start_time'])}")
    if corps.get("end_enabled"):
        morceaux.append(f"arrêt {hhmm(corps['end_time'])}")
    return f"{' · '.join(morceaux)} — {corps['days_of_week']}"
