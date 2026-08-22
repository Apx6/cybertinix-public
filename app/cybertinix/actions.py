"""Commandes véhicule exposées à l'interface.

Une liste blanche, pas un passe-plat vers l'API. Fleet API expose une
soixantaine de commandes, dont `erase_user_data`, la gestion des conducteurs et
les codes PIN — rien qui ait sa place derrière un bouton d'interface. On
n'expose que ce qui est utile au quotidien et sans conséquence irréversible.

Chaque commande est facturée dans la catégorie `commands`, soit un millième de
dollar. Le coût n'est pas le sujet : c'est la portée qui l'est.
"""

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class Action:
    cle: str
    libelle: str
    commande: str
    groupe: Literal["acces", "coffres", "climat", "charge", "reperage", "securite", "maj", "vitres"]
    icone: str
    corps: dict = field(default_factory=dict)
    confirmation: bool = False
    aide: str = ""
    parametre: dict | None = None
    # Clés recevant la même valeur que le paramètre principal. `set_temps`
    # attend une consigne par côté ; on n'en demande qu'une à l'utilisateur.
    miroir: tuple[str, ...] = ()


ACTIONS: tuple[Action, ...] = (
    # --- Accès ---
    Action("verrouiller", "Verrouiller", "door_lock", "acces", "🔒"),
    Action(
        "deverrouiller", "Déverrouiller", "door_unlock", "acces", "🔓",
        confirmation=True,
        aide="Le véhicule reste déverrouillé jusqu'à ce que tu le reverrouilles.",
    ),
    # --- Coffres ---
    Action(
        "coffre", "Coffre arrière", "actuate_trunk", "coffres", "🎒",
        corps={"which_trunk": "rear"},
        aide="Bascule : ouvre s'il est fermé, ferme s'il est ouvert.",
    ),
    Action(
        "frunk", "Coffre avant", "actuate_trunk", "coffres", "📦",
        corps={"which_trunk": "front"},
        confirmation=True,
        aide="Le coffre avant ne se referme pas tout seul, il faut le rabattre à la main.",
    ),
    # --- Vitres ---
    Action(
        "vitres_fermer", "Fermer les vitres", "window_control", "vitres", "🪟",
        corps={"command": "close"},
        aide="Ferme toutes les vitres. Utile quand il se met à pleuvoir.",
    ),
    Action(
        "vitres_entrouvrir", "Entrouvrir les vitres", "window_control", "vitres", "🌬️",
        corps={"command": "vent"},
        aide="Aère l'habitacle. Ne pas laisser la voiture ainsi sans surveillance.",
    ),
    # --- Climat ---
    Action("clim_on", "Démarrer la clim", "auto_conditioning_start", "climat", "❄️"),
    Action("clim_off", "Arrêter la clim", "auto_conditioning_stop", "climat", "⏹️"),
    # Le mode chien maintient la température et affiche un message rassurant
    # à l'écran ; le mode camping maintient clim, écran et prises allumés.
    Action(
        "mode_chien", "Mode chien", "set_climate_keeper_mode", "climat", "🐕",
        corps={"climate_keeper_mode": 2},
        confirmation=True,
        aide="Maintient la température et affiche « Mon maître revient bientôt » "
             "à l'écran. Consomme en continu : à couper au retour.",
    ),
    Action(
        "mode_camping", "Mode camping", "set_climate_keeper_mode", "climat", "⛺",
        corps={"climate_keeper_mode": 3},
        aide="Clim, écran et prises restent actifs, sentinelle désactivée.",
    ),
    Action(
        "maintien_clim", "Maintenir la clim", "set_climate_keeper_mode", "climat", "🌡️",
        corps={"climate_keeper_mode": 1},
        aide="Garde la température après avoir quitté la voiture.",
    ),
    Action(
        "maintien_off", "Arrêter le maintien", "set_climate_keeper_mode", "climat", "🚫",
        corps={"climate_keeper_mode": 0},
        aide="Coupe mode chien, camping et maintien.",
    ),
    Action(
        "surchauffe_on", "Protection surchauffe", "set_cabin_overheat_protection", "climat", "☀️",
        corps={"on": True, "fan_only": False},
        aide="Plafonne la température de l'habitacle au soleil, pendant 12 h après "
             "le stationnement. Consomme de la batterie.",
    ),
    Action(
        "surchauffe_ventilation", "Surchauffe : ventilation", "set_cabin_overheat_protection", "climat", "🌀",
        corps={"on": True, "fan_only": True},
        aide="Même chose avec la ventilation seule, moins gourmande.",
    ),
    Action(
        "surchauffe_off", "Surchauffe : désactiver", "set_cabin_overheat_protection", "climat", "🚫",
        corps={"on": False, "fan_only": False},
    ),
    Action(
        "surchauffe_seuil", "Seuil de surchauffe", "set_cop_temp", "climat", "🎚️",
        parametre={
            "nom": "cop_temp", "type": "int", "min": 0, "max": 2, "pas": 1, "defaut": 1,
            "libelle": "Seuil : 0 = 30 °C, 1 = 35 °C, 2 = 40 °C",
        },
        aide="Température au-delà de laquelle la protection intervient.",
    ),
    Action(
        "temperature", "Température", "set_temps", "climat", "🌡️",
        parametre={
            "nom": "driver_temp",
            "type": "float",
            "min": 15,
            "max": 28,
            "pas": 0.5,
            "defaut": 21,
            "libelle": "Consigne en °C",
        },
        miroir=("passenger_temp",),
        aide="Applique la même consigne aux deux côtés de l'habitacle.",
    ),
    # --- Charge ---
    Action("charge_start", "Démarrer la charge", "charge_start", "charge", "⚡"),
    Action("charge_stop", "Arrêter la charge", "charge_stop", "charge", "🛑"),
    Action("trappe_ouvrir", "Ouvrir la trappe", "charge_port_door_open", "charge", "🔌"),
    Action("trappe_fermer", "Fermer la trappe", "charge_port_door_close", "charge", "🔻"),
    Action(
        "limite_charge", "Limite de charge", "set_charge_limit", "charge", "🎚️",
        parametre={
            "nom": "percent",
            "type": "int",
            "min": 50,
            "max": 100,
            "pas": 5,
            "defaut": 80,
            "libelle": "Pourcentage",
        },
        aide="Au quotidien, Tesla recommande 80 % ; 100 % est à réserver aux longs trajets.",
    ),
    # --- Sentinelle, à la main ---
    Action(
        "sentinelle_on", "Activer la sentinelle", "set_sentry_mode", "securite", "📹",
        corps={"on": True},
        aide="Garde la voiture éveillée, caméras allumées : environ 1 % de batterie "
             "par heure. À réserver aux stationnements à risque.",
    ),
    Action(
        "sentinelle_off", "Désactiver la sentinelle", "set_sentry_mode", "securite", "📴",
        corps={"on": False},
    ),
    # --- Mise à jour logicielle Tesla ---
    Action(
        "maj_installer", "Installer la mise à jour", "schedule_software_update", "maj", "⬇️",
        corps={"offset_sec": 120},
        confirmation=True,
        aide="Lance l'installation dans deux minutes. La voiture est indisponible "
             "pendant la durée annoncée, souvent 25 à 45 minutes.",
    ),
    Action(
        "maj_ce_soir", "Installer cette nuit", "schedule_software_update", "maj", "🌙",
        parametre={
            "nom": "offset_sec", "type": "int", "min": 300, "max": 86400, "pas": 900,
            "defaut": 4 * 3600, "libelle": "Délai avant installation, en secondes",
        },
        aide="Par défaut dans quatre heures. La voiture doit rester garée et connectée.",
    ),
    Action(
        "maj_annuler", "Annuler la mise à jour", "cancel_software_update", "maj", "✋",
        aide="Ne fonctionne plus une fois l'installation commencée.",
    ),
    # --- Repérage ---
    Action(
        "klaxon", "Klaxonner", "honk_horn", "reperage", "📢",
        confirmation=True,
        aide="Nécessite que le véhicule soit en stationnement.",
    ),
    Action(
        "phares", "Appels de phares", "flash_lights", "reperage", "💡",
        aide="Nécessite que le véhicule soit en stationnement.",
    ),
)

PAR_CLE = {a.cle: a for a in ACTIONS}

GROUPES = {
    "securite": "Sentinelle",
    "acces": "Verrouillage",
    "coffres": "Coffres",
    "vitres": "Vitres",
    "climat": "Climatisation",
    "charge": "Charge",
    "reperage": "Repérage",
    "maj": "Mise à jour logicielle",
}


def corps_pour(action: Action, parametre: Any | None) -> dict:
    """Corps de la requête, en validant l'éventuel paramètre."""
    corps = dict(action.corps)

    if action.parametre is None:
        return corps

    spec = action.parametre
    if parametre is None:
        parametre = spec["defaut"]

    try:
        valeur = int(float(parametre)) if spec["type"] == "int" else float(parametre)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{spec['libelle']} : valeur numérique attendue") from exc

    if not spec["min"] <= valeur <= spec["max"]:
        raise ValueError(f"{spec['libelle']} : attendu entre {spec['min']} et {spec['max']}")

    corps[spec["nom"]] = valeur
    for cle in action.miroir:
        corps[cle] = valeur
    return corps


def describe() -> list[dict]:
    """Catalogue pour l'interface."""
    return [
        {
            "cle": a.cle,
            "libelle": a.libelle,
            "groupe": a.groupe,
            "groupe_libelle": GROUPES[a.groupe],
            "icone": a.icone,
            "confirmation": a.confirmation,
            "aide": a.aide,
            "parametre": a.parametre,
        }
        for a in ACTIONS
    ]
