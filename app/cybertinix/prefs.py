"""Réglages modifiables à chaud.

Les valeurs du `.env` restent les défauts ; l'interface écrit des écarts en
base. Un cache mémoire évite d'interroger PostgreSQL dans les chemins chauds —
le moteur de règles consulte un seuil à chaque signal reçu.

Seules les clés déclarées ici sont modifiables. Une liste blanche plutôt qu'un
accès libre aux `Settings` : sans elle, l'interface pourrait réécrire le jeton
d'API ou l'URL de la base.
"""

import logging
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import Preference

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reglage:
    cle: str
    libelle: str
    type: Literal["float", "int", "bool", "str"]
    unite: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choix: tuple[str, ...] = ()
    aide: str = ""
    # Onglet de l'interface où le réglage s'affiche. La sécurité a le sien :
    # c'est l'objectif premier du projet, pas une case parmi d'autres.
    section: Literal["general", "securite"] = "general"


REGLAGES: tuple[Reglage, ...] = (
    Reglage(
        "notif_acces", "Notifier les ouvertures", "bool",
        aide="Déverrouillage, portes, coffres. Raconte tes propres gestes : bruyant. "
             "Les intrusions sont signalées de toute façon.",
    ),
    Reglage(
        "notif_trajets", "Notifier les trajets", "bool",
        aide="Départ et arrivée avec la distance parcourue.",
    ),
    Reglage(
        "notif_charge", "Notifier la charge", "bool",
        aide="Démarrée, terminée, interrompue.",
    ),
    Reglage(
        "notif_seuils", "Notifier les seuils", "bool",
        aide="Batterie basse et pression des pneus.",
    ),
    Reglage(
        "battery_low_percent", "Seuil de batterie basse", "float", "%", 5, 90,
        aide="En dessous, une alerte part une seule fois. Elle se réarme après remontée.",
    ),
    Reglage(
        "tpms_min_pressure", "Seuil de pression des pneus", "float", "bar", 1.5, 3.5,
        aide="Une Model X tourne autour de 2,9-3,1 bar. Le froid en retire 0,1 à 0,2.",
    ),
    Reglage(
        "distance_unit", "Unité de distance", "str", choix=("km", "mi"),
        aide="Sert uniquement à libeller les trajets, aucune conversion n'est faite.",
    ),
    Reglage(
        "security_auto_sentry", "Sentinelle à la demande", "bool",
        aide="Sur intrusion détectée, allume le mode Sentinelle pour que les caméras "
             "enregistrent. Aucun coût tant qu'il ne se passe rien.",
        section="securite",
    ),
    Reglage(
        "security_honk", "Klaxon sur intrusion", "bool",
        aide="Dissuasion immédiate. À activer en connaissance de cause : un faux "
             "positif la nuit réveille le quartier.",
        section="securite",
    ),
    Reglage(
        "reverse_geocode", "Nom de rue sur la carte", "bool",
        aide="Interroge OpenStreetMap pour nommer la position. Transmet donc "
             "les coordonnées du véhicule à un service tiers. Résultat mis en cache.",
    ),
    Reglage(
        "watchdog_enabled", "Chien de garde", "bool",
        aide="Surveille ce qui casse en silence : télémétrie, clé, certificat, jeton.",
    ),
    Reglage("watchdog_interval_minutes", "Fréquence de contrôle", "int", "min", 5, 240),
    Reglage(
        "watchdog_no_data_hours", "Silence toléré", "int", "h", 6, 168,
        aide="Une voiture endormie n'émet rien : un seuil court alerterait pour rien.",
    ),
    Reglage(
        "sync_grace_hours", "Délai avant alerte de désynchronisation", "float", "h", 1, 168,
        aide="La voiture ne peut adopter une configuration qu'une fois réveillée.",
    ),
    Reglage("cert_expiry_warning_days", "Alerte certificat", "int", "j", 3, 60),
    Reglage("refresh_token_warning_days", "Alerte jeton", "int", "j", 3, 60),
    Reglage("digest_enabled", "Résumé quotidien", "bool"),
    Reglage("digest_hour", "Heure du résumé", "int", "h", 0, 23),
)

_PAR_CLE = {r.cle: r for r in REGLAGES}
_cache: dict[str, Any] = {}


def _convertir(reglage: Reglage, brut: str) -> Any:
    if reglage.type == "bool":
        return brut.strip().lower() in ("1", "true", "vrai", "oui", "on")
    if reglage.type == "int":
        return int(float(brut))
    if reglage.type == "float":
        return float(brut)
    return brut


def valider(cle: str, valeur: Any) -> Any:
    """Convertit et borne. Lève ValueError sur une valeur inacceptable."""
    reglage = _PAR_CLE.get(cle)
    if reglage is None:
        raise ValueError(f"réglage inconnu : {cle}")

    valeur = _convertir(reglage, str(valeur))

    if reglage.choix and valeur not in reglage.choix:
        raise ValueError(f"{cle} : valeur attendue parmi {', '.join(reglage.choix)}")
    if reglage.minimum is not None and valeur < reglage.minimum:
        raise ValueError(f"{cle} : minimum {reglage.minimum}")
    if reglage.maximum is not None and valeur > reglage.maximum:
        raise ValueError(f"{cle} : maximum {reglage.maximum}")
    return valeur


def defaut(cle: str) -> Any:
    return getattr(settings, cle)


def get(cle: str) -> Any:
    """Valeur effective : l'écart enregistré, sinon le défaut du .env."""
    if cle in _cache:
        return _cache[cle]
    return defaut(cle)


async def load(session: AsyncSession) -> None:
    _cache.clear()
    for pref in (await session.scalars(select(Preference))).all():
        if pref.key not in _PAR_CLE:
            continue  # réglage retiré du code depuis : on l'ignore
        try:
            _cache[pref.key] = _convertir(_PAR_CLE[pref.key], pref.value)
        except ValueError:
            log.warning("réglage illisible ignoré : %s=%r", pref.key, pref.value)
    if _cache:
        log.info("réglages personnalisés : %s", ", ".join(sorted(_cache)))


async def set(session: AsyncSession, cle: str, valeur: Any) -> Any:
    valeur = valider(cle, valeur)

    row = await session.scalar(select(Preference).where(Preference.key == cle))
    if row is None:
        session.add(Preference(key=cle, value=str(valeur)))
    else:
        row.value = str(valeur)
    await session.commit()

    _cache[cle] = valeur
    return valeur


async def reset(session: AsyncSession, cle: str) -> Any:
    """Revient au défaut du .env en supprimant l'écart."""
    row = await session.scalar(select(Preference).where(Preference.key == cle))
    if row is not None:
        await session.delete(row)
        await session.commit()
    _cache.pop(cle, None)
    return defaut(cle)


def describe() -> list[dict]:
    """Description complète pour l'interface : valeur, défaut, bornes, aide."""
    return [
        {
            "cle": r.cle,
            "libelle": r.libelle,
            "type": r.type,
            "unite": r.unite,
            "min": r.minimum,
            "max": r.maximum,
            "choix": list(r.choix),
            "aide": r.aide,
            "valeur": get(r.cle),
            "defaut": defaut(r.cle),
            "personnalise": r.cle in _cache,
            "section": r.section,
        }
        for r in REGLAGES
    ]
