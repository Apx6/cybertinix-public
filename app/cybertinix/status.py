"""État consolidé du système.

Socle commun à l'endpoint `/status`, au chien de garde et à la commande
Telegram. Chaque section est isolée : une source indisponible remonte son
erreur sans faire échouer le reste, parce qu'un état partiel reste utile —
c'est même souvent quand quelque chose casse qu'on vient le consulter.

Les appels Tesla utilisés ici (`fleet_status`, la configuration de télémétrie,
les erreurs) ne sont pas facturés. Interroger cet état ne coûte rien et ne
réveille pas le véhicule.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography import x509
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import enums, geocode, oauth, prefs, security
from .config import settings
from .fleet import FleetClient
from .models import ConnectivityEvent, OAuthToken, Signal

log = logging.getLogger(__name__)

# Tarif Tesla : 150 000 signaux pour 1 $.
SIGNALS_PER_DOLLAR = 150_000


def local_now() -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def humanize(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} j"


async def known_vin(session: AsyncSession) -> str | None:
    """VIN le plus récemment vu dans les signaux."""
    return await session.scalar(select(Signal.vin).order_by(Signal.id.desc()).limit(1))


# --- Sections ----------------------------------------------------------------


async def _flux(session: AsyncSession) -> dict:
    now = datetime.now(UTC)

    last = await session.scalar(select(func.max(Signal.received_at)))
    depuis_24h = await session.scalar(
        select(func.count(Signal.id)).where(Signal.received_at > now - timedelta(hours=24))
    )
    champs = await session.scalar(
        select(func.count(distinct(Signal.name))).where(
            Signal.received_at > now - timedelta(days=7)
        )
    )
    connexion = await session.scalar(
        select(ConnectivityEvent).order_by(ConnectivityEvent.id.desc()).limit(1)
    )

    age = (now - last) if last else None
    return {
        "dernier_signal": last.isoformat() if last else None,
        "age": humanize(age) if age else None,
        "age_heures": round(age.total_seconds() / 3600, 1) if age else None,
        "signaux_24h": depuis_24h or 0,
        "champs_distincts_7j": champs or 0,
        "derniere_connectivite": (
            {"statut": connexion.status, "quand": connexion.received_at.isoformat()}
            if connexion
            else None
        ),
    }


async def _cout(session: AsyncSession) -> dict:
    """Coût du mois en cours, estimé sur le volume de signaux."""
    debut = local_now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = await session.scalar(
        select(func.count(Signal.id)).where(Signal.received_at >= debut.astimezone(UTC))
    )
    total = total or 0
    return {
        "signaux_ce_mois": total,
        "dollars_estimes": round(total / SIGNALS_PER_DOLLAR, 3),
        "remise_mensuelle": 10,
        "commentaire": "La remise de 10 $/mois couvre très largement ce volume.",
    }


def _certificat() -> dict:
    chemin = Path(settings.telemetry_cert_path)
    if not chemin.is_file():
        return {"erreur": f"certificat introuvable : {chemin}"}
    try:
        cert = x509.load_pem_x509_certificate(chemin.read_bytes())
        expire = cert.not_valid_after_utc
        restant = expire - datetime.now(UTC)
        return {
            "expire_le": expire.isoformat(),
            "jours_restants": restant.days,
            "renouvellement_automatique": True,
        }
    except Exception as exc:  # noqa: BLE001
        return {"erreur": str(exc)}


async def _jeton(session: AsyncSession) -> dict:
    token = await session.scalar(select(OAuthToken).limit(1))
    if token is None:
        return {"present": False, "commentaire": "aucune autorisation — passer par /auth/login"}

    # Le jeton de rafraîchissement expire trois mois après son émission, et il
    # est à usage unique : on date son dernier échange pour estimer l'échéance.
    age = datetime.now(UTC) - token.updated_at
    reste = settings.refresh_token_max_age_days - age.days
    return {
        "present": True,
        "acces_expire_le": token.expires_at.isoformat(),
        "acces_valide": token.expires_at > datetime.now(UTC),
        "refresh_dernier_echange": token.updated_at.isoformat(),
        "refresh_jours_restants_estimes": reste,
        "scopes": token.scopes,
    }


async def _vehicule(session: AsyncSession) -> dict:
    vin = await known_vin(session)
    try:
        access = await oauth.valid_access_token(session, "default")
    except Exception as exc:  # noqa: BLE001
        return {"vin": vin, "erreur": f"jeton indisponible : {exc}"}

    client = FleetClient(access)
    if vin is None:
        try:
            liste = await client.list_vehicles()
            vins = [v.get("vin") for v in liste.get("response", [])]
            vin = vins[0] if vins else None
        except Exception as exc:  # noqa: BLE001
            return {"erreur": f"liste des véhicules indisponible : {exc}"}
    if vin is None:
        return {"erreur": "aucun véhicule connu"}

    out: dict = {"vin": vin}

    try:
        statut = (await client.fleet_status([vin])).get("response", {})
        infos = (statut.get("vehicle_info") or {}).get(vin, {})
        out |= {
            "cle_appairee": vin in (statut.get("key_paired_vins") or []),
            "firmware": infos.get("firmware_version"),
            "client_telemetrie": infos.get("fleet_telemetry_version"),
            "commandes_signees_requises": infos.get("vehicle_command_protocol_required"),
            "nombre_de_cles": infos.get("total_number_of_keys"),
        }
    except Exception as exc:  # noqa: BLE001
        out["erreur_statut"] = str(exc)

    try:
        config = (await client.get_telemetry_config(vin)).get("response", {})
        out["telemetrie"] = {
            "synced": config.get("synced"),
            "limite_atteinte": config.get("limit_reached"),
            "champs": sorted((config.get("config") or {}).get("fields", {})),
        }
    except Exception as exc:  # noqa: BLE001
        out["erreur_config"] = str(exc)

    try:
        # La réponse enveloppe la liste : {"response": {"fleet_telemetry_errors": [...]}}.
        brut = (await client.telemetry_errors(vin)).get("response", {})
        erreurs = brut.get("fleet_telemetry_errors", []) if isinstance(brut, dict) else brut
        out["erreurs_telemetrie"] = erreurs
        # Tesla conserve un historique : sans filtre sur l'âge, une panne
        # corrigée resterait signalée indéfiniment.
        out["erreurs_recentes"] = recent_errors(erreurs)
    except Exception as exc:  # noqa: BLE001
        out["erreur_erreurs"] = str(exc)

    return out


def recent_errors(erreurs: list, heures: float = 24.0) -> list:
    limite = datetime.now(UTC) - timedelta(hours=heures)
    recentes = []
    for erreur in erreurs:
        horodatage = erreur.get("created_at") if isinstance(erreur, dict) else None
        if not horodatage:
            continue
        try:
            quand = datetime.fromisoformat(horodatage.replace("Z", "+00:00"))
        except ValueError:
            continue
        if quand > limite:
            recentes.append(erreur)
    return recentes


CHAMPS_VITRINE = (
    "Soc",
    "EstBatteryRange",
    "Odometer",
    "Locked",
    "DoorState",
    "DetailedChargeState",
    "ChargeAmps",
    "ChargeLimitSoc",
    "TimeToFullCharge",
    "Gear",
    "VehicleSpeed",
    "VehicleName",
    "Location",
    "FdWindow",
    "FpWindow",
    "RdWindow",
    "RpWindow",
    "HvacPower",
    "SentryMode",
    "CenterDisplay",
    "DriverSeatOccupied",
    "LocatedAtHome",
    "LocatedAtWork",
    "LocatedAtFavorite",
    "ClimateKeeperMode",
    "CabinOverheatProtectionMode",
    "CabinOverheatProtectionTemperatureLimit",
    "Version",
    "SoftwareUpdateVersion",
    "SoftwareUpdateDownloadPercentComplete",
    "SoftwareUpdateInstallationPercentComplete",
    "SoftwareUpdateScheduledStartTime",
    "SoftwareUpdateExpectedDurationMinutes",
    "InsideTemp",
    "OutsideTemp",
    "HvacLeftTemperatureRequest",
    "TpmsPressureFl",
    "TpmsPressureFr",
    "TpmsPressureRl",
    "TpmsPressureRr",
    "TpmsHardWarnings",
    "TpmsLastSeenPressureTimeFl",
    "TpmsLastSeenPressureTimeFr",
    "TpmsLastSeenPressureTimeRl",
    "TpmsLastSeenPressureTimeRr",
)


async def live(session: AsyncSession) -> dict:
    """Dernière valeur connue des champs affichés par l'interface.

    Lu depuis la base, donc gratuit et instantané — aucun appel à Tesla, aucun
    risque de réveiller le véhicule. Ces valeurs viennent de la télémétrie, donc
    elles datent du dernier changement, pas de maintenant.

    Les énumérations et les distances sont traduites ici, côté serveur : elles
    dépendent des tables de correspondance et du réglage d'unité, qui n'ont pas
    à être dupliqués dans le navigateur.
    """
    vin = await known_vin(session)
    if vin is None:
        return {"vin": None, "champs": {}, "position": None}

    unite = prefs.get("distance_unit")
    champs: dict[str, dict] = {}

    for nom in CHAMPS_VITRINE:
        ligne = await session.execute(
            select(Signal.value, Signal.received_at)
            .where(Signal.vin == vin, Signal.name == nom)
            .order_by(Signal.id.desc())
            .limit(1)
        )
        row = ligne.first()
        if row is None:
            continue
        brut, quand = row
        try:
            valeur = json.loads(brut)
        except (json.JSONDecodeError, TypeError):
            valeur = brut

        entree: dict = {"valeur": valeur, "quand": quand.isoformat()}
        libelle = enums.libelle(nom, valeur, unite)
        if libelle is not None:
            entree["libelle"] = libelle
        champs[nom] = entree

    return {
        "vin": vin,
        "champs": champs,
        "position": await _position(session, champs),
        "unite_distance": unite,
        "connexion": await _connexion(session, vin),
        "armee": await security.armee_pour(vin),
    }


async def _connexion(session: AsyncSession, vin: str) -> dict | None:
    """Éveillée ou endormie, d'après le dernier événement de connectivité.

    C'est la seule source fiable : une voiture éveillée mais immobile n'émet
    aucun signal nouveau, donc l'âge du dernier signal ne dit rien de son
    sommeil. Tesla documente d'ailleurs ces événements comme « proxy de
    l'état en ligne du véhicule », fiable à 99 %.
    """
    evt = await session.scalar(
        select(ConnectivityEvent)
        .where(ConnectivityEvent.vin == vin)
        .order_by(ConnectivityEvent.id.desc())
        .limit(1)
    )
    if evt is None:
        return None
    return {
        "eveillee": evt.status.upper() == "CONNECTED",
        "statut": evt.status,
        "depuis": evt.received_at.isoformat(),
    }


async def _position(session: AsyncSession, champs: dict) -> dict | None:
    """Coordonnées et, si le réglage l'autorise, nom de rue."""
    brut = (champs.get("Location") or {}).get("valeur")
    if not isinstance(brut, dict):
        return None

    lat = brut.get("latitude", brut.get("Latitude", brut.get("lat")))
    lon = brut.get("longitude", brut.get("Longitude", brut.get("lon")))
    if lat is None or lon is None:
        return None

    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None

    return {"lat": lat, "lon": lon, "rue": await geocode.rue(session, lat, lon)}


async def collect(session: AsyncSession, *, avec_tesla: bool = True) -> dict:
    """État complet. `avec_tesla=False` reste purement local et instantané."""
    etat = {
        "genere_le": local_now().isoformat(),
        "flux": await _flux(session),
        "certificat": _certificat(),
        "jeton": await _jeton(session),
        "cout": await _cout(session),
    }
    if avec_tesla:
        etat["vehicule"] = await _vehicule(session)
    return etat


# --- Rendu pour Telegram -----------------------------------------------------


def render(etat: dict) -> str:
    """Version lisible sur un téléphone, sans JSON."""
    lignes: list[str] = ["📊 État de CyberTinix", ""]

    flux = etat["flux"]
    if flux["dernier_signal"]:
        lignes.append(f"Dernier signal : il y a {flux['age']}")
    else:
        lignes.append("Dernier signal : aucun")
    lignes.append(f"Signaux sur 24 h : {flux['signaux_24h']}")
    if flux["derniere_connectivite"]:
        lignes.append(f"Connectivité : {flux['derniere_connectivite']['statut']}")

    vehicule = etat.get("vehicule", {})
    if vehicule and "erreur" not in vehicule:
        lignes += ["", "🚗 Véhicule"]
        lignes.append(f"Clé appairée : {'oui' if vehicule.get('cle_appairee') else 'NON'}")
        telemetrie = vehicule.get("telemetrie") or {}
        synced = telemetrie.get("synced")
        lignes.append(
            f"Télémétrie : {'synchronisée' if synced else 'NON synchronisée'}"
            f" ({len(telemetrie.get('champs', []))} champs)"
        )
        if vehicule.get("firmware"):
            lignes.append(f"Firmware : {vehicule['firmware']}")
        if vehicule.get("erreurs_telemetrie"):
            lignes.append(f"⚠️ Erreurs remontées : {len(vehicule['erreurs_telemetrie'])}")
    elif vehicule:
        lignes += ["", f"🚗 Véhicule : {vehicule.get('erreur', 'indisponible')}"]

    cert = etat["certificat"]
    jeton = etat["jeton"]
    lignes += ["", "🔧 Infrastructure"]
    if "jours_restants" in cert:
        lignes.append(f"Certificat : {cert['jours_restants']} j restants")
    if jeton.get("present"):
        lignes.append(f"Jeton : ~{jeton['refresh_jours_restants_estimes']} j avant expiration")
    else:
        lignes.append("Jeton : absent")

    cout = etat["cout"]
    lignes += ["", f"💶 Ce mois : {cout['signaux_ce_mois']} signaux ≈ {cout['dollars_estimes']} $"]
    lignes.append("(couvert par la remise de 10 $)")

    return "\n".join(lignes)
