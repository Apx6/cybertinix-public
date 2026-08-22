"""Lecture des alertes remontées par le véhicule.

Les noms suivent le schéma `<calculateur>_<w|a><numéro>_<libellé>` :
`APP_w207_autosteerUnavailable` vient du calculateur Autopilot (APP), en
avertissement (w) numéro 207. Un `a` signale une alerte plus sérieuse.

Le calculateur qui gère l'alarme antivol s'appelle **VCSEC** (Vehicle
Security). C'est son préfixe qui distingue une alerte de sécurité du flot de
diagnostics Autopilot, charge ou service que le véhicule émet en permanence.

Le véhicule livre son historique récent en bloc au moment où il adopte une
configuration de télémétrie : les alertes arrivent alors avec des heures
d'événement bien antérieures à leur réception. On affiche `StartedAt`, jamais
l'heure de réception.
"""

import json
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import VehicleAlert

# Alertes relevant de la sécurité, sur le libellé uniquement.
#
# Le calculateur VCSEC (Vehicle Security) ne gère pas que l'alarme : il émet
# aussi les avertissements de pression des pneus, de pile de clé, d'ouvrants.
# Filtrer sur son seul préfixe a transformé `VCSEC_a221_TPMSSoftWarning` — un
# pneu un peu dégonflé — en intrusion, avec klaxon. On ne retient donc que les
# libellés qui parlent explicitement d'alarme ou d'effraction.
_SECURITE = re.compile(
    r"alarm|intrusion|theft|tilt|sentry|break.?in|glass|tamper|unauthori[sz]|forced",
    re.I,
)

# Alertes VCSEC qui concernent les pneus : relayées dans la famille « seuils ».
_PNEUS = re.compile(r"tpms|tire|tyre|pressure", re.I)

_CALCULATEURS = {
    "APP": "Autopilot",
    "APS": "Autopilot",
    "VCSEC": "Sécurité",
    "VCFRONT": "Carrosserie avant",
    "VCLEFT": "Carrosserie gauche",
    "VCRIGHT": "Carrosserie droite",
    "BMS": "Batterie",
    "CP": "Port de charge",
    "CMPD": "Compresseur clim",
    "THC": "Thermique",
    "DI": "Moteur",
    "DIF": "Moteur avant",
    "DIR": "Moteur arrière",
    "EPAS": "Direction",
    "ESP": "Stabilité",
    "PCS": "Chargeur embarqué",
    "UI": "Écran",
    "MCU": "Calculateur central",
    "TPMS": "Pneus",
    "HVP": "Haute tension",
    "UMC": "Câble mobile",
}


def est_securite(nom: str) -> bool:
    return bool(_SECURITE.search(nom)) and not _PNEUS.search(nom)


def est_pneu(nom: str) -> bool:
    return bool(_PNEUS.search(nom))


def _humaniser(code: str) -> str:
    """`autosteerUnavailable` -> « autosteer unavailable »."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", code).lower()


def decoder(alerte: VehicleAlert) -> dict:
    """Structure une alerte brute pour l'affichage."""
    nom = alerte.name
    parts = nom.split("_", 2)
    calculateur = parts[0] if len(parts) >= 2 else ""
    code = parts[2] if len(parts) == 3 else (parts[-1] if parts else nom)
    niveau = ""
    if len(parts) >= 2 and parts[1][:1] in ("w", "a"):
        niveau = "avertissement" if parts[1][0] == "w" else "alerte"

    try:
        payload = json.loads(alerte.payload)
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    debut = payload.get("StartedAt")
    fin = payload.get("EndedAt")
    duree = None
    if debut and fin:
        try:
            d0 = datetime.fromisoformat(debut.replace("Z", "+00:00"))
            d1 = datetime.fromisoformat(fin.replace("Z", "+00:00"))
            duree = max(0, int((d1 - d0).total_seconds()))
        except ValueError:
            pass

    return {
        "nom": nom,
        "calculateur": calculateur,
        "systeme": _CALCULATEURS.get(calculateur, calculateur),
        "niveau": niveau,
        "libelle": _humaniser(code) if code else nom,
        "securite": est_securite(nom),
        "audiences": payload.get("Audiences") or [],
        "debut": debut,
        "fin": fin,
        "duree_s": duree,
        "recu": alerte.received_at.isoformat(),
    }


async def recentes(session: AsyncSession, limite: int = 50) -> list[VehicleAlert]:
    rows = await session.scalars(
        select(VehicleAlert).order_by(VehicleAlert.id.desc()).limit(limite)
    )
    return list(rows)
