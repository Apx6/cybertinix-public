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

from . import enums, geocode, oauth, prefs, reconcile, security
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
        recentes = recent_errors(erreurs)
        out["erreurs_recentes"] = recentes
        out["erreurs_detail"] = decrire_erreurs(recentes)
    except Exception as exc:  # noqa: BLE001
        out["erreur_erreurs"] = str(exc)

    return out


# Les erreurs de flux sont écrites pour un ingénieur Tesla, pas pour le
# propriétaire. On dit ce qu'elles signifient et si elles demandent une action.
# Presque toutes viennent du **véhicule**, pas du serveur : au réveil, sa liaison
# cellulaire n'est pas toujours prête quand il compose l'adresse. Il réessaie
# seul quelques secondes plus tard.
# Dernier champ : la gravité. « info » = le véhicule s'en remet seul, rien à
# faire ; « action » = quelque chose est cassé de notre côté. Seules les
# secondes méritent de réveiller quelqu'un.
_ERREURS = (
    ("network is unreachable",
     "Le véhicule n'avait pas encore de réseau au réveil",
     "Sans gravité : il se reconnecte seul quelques secondes après. Rien à faire.",
     "info"),
    ("connection refused",
     "Le serveur a refusé la connexion",
     "À traiter : le serveur de télémétrie était arrêté. Vérifier avec make check.",
     "action"),
    ("i/o timeout",
     "Le véhicule n'a pas obtenu de réponse à temps",
     "Sans gravité si isolé : liaison cellulaire faible, parking souterrain.",
     "info"),
    # Même famille que le précédent, formulée par la bibliothèque Go du client
    # Tesla : le délai imparti à la connexion a expiré. Rencontrée le 02/09 à
    # 08:47, trois fois, encadrée par une coupure réseau ; la voiture s'est
    # reconnectée seule à 08:49:33 sans intervention.
    ("context deadline exceeded",
     "Délai de connexion dépassé côté véhicule",
     "Sans gravité si isolé : la liaison n'a pas abouti dans le temps imparti, la voiture réessaie seule.",
     "info"),
    ("EOF",
     "Connexion coupée en cours d'échange",
     "Sans gravité si isolé : la voiture s'est rendormie ou a perdu le réseau.",
     "info"),
    ("bad certificate",
     "Le véhicule a refusé notre certificat",
     "À traiter : le certificat TLS est expiré ou incomplet. Relancer make certs.",
     "action"),
)

# Au-delà, même des erreurs bénignes cessent de l'être : une voiture qui
# n'arrive pas à se connecter vingt fois en une journée a un vrai problème,
# ou c'est notre serveur qui vacille.
SEUIL_ERREURS_BENIGNES = 12


def expliquer_erreur(texte: str) -> tuple[str, str, str]:
    """Libellé, conduite à tenir et gravité d'une erreur de flux."""
    for motif, libelle, conseil, gravite in _ERREURS:
        if motif.lower() in texte.lower():
            return libelle, conseil, gravite
    # Une erreur inconnue n'est pas présumée bénigne : on la signale, c'est
    # précisément celle sur laquelle on n'a pas encore d'expérience.
    return "Erreur de flux non répertoriée", "À examiner : signature jamais rencontrée.", "action"


def decrire_erreurs(erreurs: list) -> list[dict]:
    out = []
    for erreur in erreurs:
        texte = str(erreur.get("error", "")) if isinstance(erreur, dict) else str(erreur)
        libelle, conseil, gravite = expliquer_erreur(texte)
        out.append({
            "quand": erreur.get("created_at") if isinstance(erreur, dict) else None,
            "libelle": libelle,
            "conseil": conseil,
            "gravite": gravite,
            "brut": texte.strip('"'),
        })
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
    "DriverSeatBelt",
    "PassengerSeatBelt",
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

    # Le verrouillage cesse parfois d'être émis pendant des heures. Plutôt que
    # d'afficher une valeur périmée avec aplomb, on la redemande au véhicule —
    # au plus une fois par cinq minutes, et sans jamais le réveiller.
    if await reconcile.verrou_perime(session, vin):
        await reconcile.rafraichir_verrou(session, vin)

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
        "verrou": await security.etat_verrou(session, vin),
    }


async def _connexion(session: AsyncSession, vin: str) -> dict | None:
    """Éveillée ou endormie, d'après le dernier événement de connectivité.

    L'âge du dernier signal ne peut pas *prouver le sommeil* — une voiture
    éveillée mais immobile n'émet rien. Il peut en revanche prouver l'éveil :
    une voiture déconnectée n'émet pas. Un signal postérieur au dernier
    événement `DISCONNECTED` l'emporte donc sur celui-ci, sans quoi l'interface
    annonce « endormie » sur une voiture qui roule à 103 km/h.

    Tesla documente ces événements comme « proxy de l'état en ligne du
    véhicule », fiable à 99 % — le pour cent restant est exactement ce cas.
    """
    evt = await session.scalar(
        select(ConnectivityEvent)
        .where(ConnectivityEvent.vin == vin)
        .order_by(ConnectivityEvent.id.desc())
        .limit(1)
    )
    if evt is None:
        return None

    eveillee = evt.status.upper() == "CONNECTED"
    if not eveillee:
        dernier = await session.scalar(
            select(Signal.received_at)
            .where(Signal.vin == vin)
            .order_by(Signal.id.desc())
            .limit(1)
        )
        if dernier is not None and dernier > evt.received_at:
            eveillee = True

    return {
        "eveillee": eveillee,
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
