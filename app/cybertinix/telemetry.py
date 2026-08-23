"""Construction de la configuration Fleet Telemetry envoyée au véhicule.

La configuration est signée par le proxy avec la clé privée de l'application,
puis adoptée par la voiture elle-même. Tesla ne peut pas la modifier après
coup : c'est ce qui garantit à l'utilisateur que seuls les champs demandés
sont diffusés.
"""

from pathlib import Path

from .config import settings

# Jeu de champs par défaut.
#
# Les intervalles ne sont pas des fréquences d'envoi : un signal ne part que si
# sa valeur a changé ET que l'intervalle est écoulé. Mettre 1 sur un champ
# stable comme `Locked` ne coûte donc rien — il n'émet qu'aux changements, et
# on veut l'apprendre tout de suite. À l'inverse `Soc` bouge en permanence
# pendant une charge, d'où un intervalle long qui plafonne le débit.
#
# `minimum_delta` filtre en amont les variations insignifiantes, sur lesquelles
# on paierait sans rien apprendre : le bruit du GPS à l'arrêt, un dixième de
# pourcent de batterie. Il exige le firmware 2024.44.32 ou plus récent — à
# retirer si la configuration est refusée sur un véhicule plus ancien.
# Pour les positions, l'écart se mesure en mètres.
DEFAULT_FIELDS: dict[str, dict] = {
    # État instantané, utile aux notifications
    "Locked": {"interval_seconds": 1},
    "DoorState": {"interval_seconds": 1},
    "VehicleName": {"interval_seconds": 1},
    # Sécurité sans sentinelle. Une voiture endormie n'émet rien, mais une
    # intrusion la réveille — et au réveil elle pousse ces trois champs.
    # `CenterDisplay` à `Lock` signifie que l'écran s'est allumé dans un
    # habitacle verrouillé : quelqu'un est dedans. `ChargePortLatch` sert à
    # écarter le faux positif du branchement de la trappe.
    "CenterDisplay": {"interval_seconds": 1},
    "ChargePortLatch": {"interval_seconds": 1},
    "SentryMode": {"interval_seconds": 1},
    # Quelqu'un d'assis dans une voiture verrouillée : le signal d'intrusion
    # le plus direct, sans corrélation temporelle à faire.
    "DriverSeatOccupied": {"interval_seconds": 1},
    # Seul le siège conducteur a un capteur d'occupation. Pour les autres
    # places, les ceintures sont le seul indice : une ceinture qui se boucle
    # dans une voiture armée, c'est quelqu'un à bord.
    "DriverSeatBelt": {"interval_seconds": 1},
    "PassengerSeatBelt": {"interval_seconds": 1},
    # La voiture sait elle-même si elle est chez toi. Sert de contexte : plus
    # stricte ailleurs, et un départ du domicile sans ta clé est un vol.
    "LocatedAtHome": {"interval_seconds": 1},
    "LocatedAtWork": {"interval_seconds": 1},
    "LocatedAtFavorite": {"interval_seconds": 1},
    # Mises à jour logicielles Tesla
    "Version": {"interval_seconds": 1},
    "SoftwareUpdateVersion": {"interval_seconds": 1},
    "SoftwareUpdateDownloadPercentComplete": {"interval_seconds": 60, "minimum_delta": 5},
    "SoftwareUpdateInstallationPercentComplete": {"interval_seconds": 60, "minimum_delta": 5},
    "SoftwareUpdateScheduledStartTime": {"interval_seconds": 1},
    "SoftwareUpdateExpectedDurationMinutes": {"interval_seconds": 1},
    # Modes de climatisation
    "ClimateKeeperMode": {"interval_seconds": 1},
    "CabinOverheatProtectionMode": {"interval_seconds": 1},
    "CabinOverheatProtectionTemperatureLimit": {"interval_seconds": 1},
    "ChargeAmps": {"interval_seconds": 1, "minimum_delta": 1},
    # L'état de charge accompagné de son contexte : sans cela, une notification
    # « charge terminée » arriverait sans le niveau de batterie associé.
    "DetailedChargeState": {
        "interval_seconds": 1,
        "include_fields": ["Soc", "EstBatteryRange"],
    },
    # Suivi, volontairement lent
    "Soc": {"interval_seconds": 60, "minimum_delta": 1},
    "EstBatteryRange": {"interval_seconds": 60, "minimum_delta": 3},
    "Odometer": {"interval_seconds": 60, "minimum_delta": 1},
    # Conduite. `Gear` borne les trajets : la vitesse retombe à zéro à chaque
    # feu rouge, alors que le passage en P marque une vraie fin de trajet.
    "Gear": {"interval_seconds": 1},
    "VehicleSpeed": {"interval_seconds": 10, "minimum_delta": 3},
    "Location": {"interval_seconds": 10, "minimum_delta": 25},
    # Ouvrants vitrés
    "FdWindow": {"interval_seconds": 1},
    "FpWindow": {"interval_seconds": 1},
    "RdWindow": {"interval_seconds": 1},
    "RpWindow": {"interval_seconds": 1},
    # Climatisation. Les températures dérivent en permanence par petits pas :
    # sans `minimum_delta`, elles domineraient à elles seules le volume facturé.
    "HvacPower": {"interval_seconds": 1},
    "InsideTemp": {"interval_seconds": 60, "minimum_delta": 0.5},
    "OutsideTemp": {"interval_seconds": 60, "minimum_delta": 0.5},
    "HvacLeftTemperatureRequest": {"interval_seconds": 1, "minimum_delta": 0.5},
    # Charge : cible et temps restant, utiles à l'affichage
    "ChargeLimitSoc": {"interval_seconds": 1},
    "TimeToFullCharge": {"interval_seconds": 60, "minimum_delta": 0.2},
    # Pression des pneus — variations lentes, un dixième de bar suffit
    "TpmsPressureFl": {"interval_seconds": 1, "minimum_delta": 0.1},
    "TpmsPressureFr": {"interval_seconds": 1, "minimum_delta": 0.1},
    "TpmsPressureRl": {"interval_seconds": 1, "minimum_delta": 0.1},
    "TpmsPressureRr": {"interval_seconds": 1, "minimum_delta": 0.1},
    "TpmsHardWarnings": {"interval_seconds": 1},
}

# Le véhicule retransmet les messages non acquittés par le serveur plutôt que
# de les perdre. Exige le client 1.0.0 côté véhicule et fleet-telemetry 0.7.1+.
DEFAULT_DELIVERY_POLICY = "latest"

# `service` remonte les alertes techniques du véhicule, `customer` celles
# destinées au conducteur — dont l'alarme antivol intégrée, distincte de la
# sentinelle. Plus bavard, mais chaque alerte porte une information réelle.
DEFAULT_ALERT_TYPES = ["service", "customer"]


def _root_domain(host: str) -> str:
    """Domaine de second niveau + TLD, tel que Tesla l'entend.

    Approximation assumée : elle est fausse pour les suffixes composés du type
    `.co.uk`. Suffisant ici, et la vraie règle exigerait la Public Suffix List.
    """
    return ".".join(host.rsplit(".", 2)[-2:])


def read_ca(path: str | None = None) -> str:
    """Chaîne de certification du serveur de télémétrie.

    Le véhicule s'en sert pour valider le certificat présenté à la connexion.
    Sans elle, la voiture refuse le flux avec une erreur `bad certificate` —
    Tesla insiste d'ailleurs sur une autorité « communément reconnue ».
    """
    ca_path = Path(path or settings.telemetry_ca_path)
    if not ca_path.is_file():
        raise FileNotFoundError(
            f"chaîne de certification introuvable : {ca_path}. "
            "Le conteneur app doit monter le volume certbot en lecture."
        )
    content = ca_path.read_text().strip()
    if "BEGIN CERTIFICATE" not in content:
        raise ValueError(f"{ca_path} ne contient pas de certificat PEM")
    return content


def build_config(
    *,
    fields: dict[str, dict] | None = None,
    alert_types: list[str] | None = None,
    delivery_policy: str | None = None,
    exp: int | None = None,
    ca: str | None = None,
) -> dict:
    """Assemble le bloc `config` de la requête fleet_telemetry_config."""
    hostname = settings.telemetry_hostname

    # Contrainte documentée par Tesla : le nom du serveur de télémétrie doit
    # partager le domaine racine de l'application enregistrée. On échoue ici
    # plutôt que de laisser l'API renvoyer un refus opaque.
    if _root_domain(hostname) != _root_domain(settings.domain):
        raise ValueError(
            f"hostname de télémétrie ({hostname}) et domaine de l'application "
            f"({settings.domain}) doivent partager le même domaine racine"
        )

    # Contrainte de notre propre infrastructure : nginx aiguille sur le motif
    # `~^telemetry\.`. Un autre préfixe enverrait le véhicule vers l'app, qui
    # fermerait la connexion sans que rien n'indique pourquoi.
    if not hostname.startswith("telemetry."):
        raise ValueError(
            f"hostname de télémétrie ({hostname}) doit commencer par 'telemetry.' "
            "pour que le routage SNI de nginx l'atteigne"
        )

    config: dict = {
        "hostname": hostname,
        "port": settings.telemetry_port,
        "ca": ca if ca is not None else read_ca(),
        "fields": fields or DEFAULT_FIELDS,
        "alert_types": alert_types if alert_types is not None else DEFAULT_ALERT_TYPES,
    }

    # `None` = valeur par défaut, chaîne vide = ne rien envoyer. La distinction
    # compte pour pouvoir retomber sur le comportement d'origine si un véhicule
    # trop ancien refusait l'option.
    policy = DEFAULT_DELIVERY_POLICY if delivery_policy is None else delivery_policy
    if policy:
        config["delivery_policy"] = policy

    if exp:
        config["exp"] = exp

    return config


def summarize_response(payload: dict) -> dict:
    """Extrait ce qui compte d'une réponse de configuration.

    L'API répond 200 même quand elle n'a rien configuré : les refus sont
    listés dans `skipped_vehicles`, par motif. Les ignorer donnerait
    l'illusion d'un succès.
    """
    response = payload.get("response", payload)
    skipped = response.get("skipped_vehicles") or {}

    # Motifs possibles : missing_key (clé virtuelle absente),
    # unsupported_hardware (S/X pré-2018), unsupported_firmware,
    # max_configs (cinq configurations déjà présentes).
    reasons = {motif: vins for motif, vins in skipped.items() if vins}

    return {
        "configured": response.get("updated_vehicles", 0),
        "skipped": reasons,
        "ok": not reasons,
        "raw": response,
    }
