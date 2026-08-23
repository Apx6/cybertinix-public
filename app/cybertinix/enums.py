"""Traduction des énumérations et conversion des unités Tesla.

Le véhicule envoie ses énumérations sous forme de chaînes préfixées du nom de
l'énumération : `DetailedChargeStateCharging`, `WindowStateClosed`,
`ShiftStateP`. Comparer ces valeurs à `"Charging"` ou `"P"` échoue
silencieusement — c'est exactement ce qui empêchait les notifications de
démarrage et d'interruption de charge de se déclencher.

Côté distances, la documentation est explicite : `Odometer` est « the number of
miles the vehicle has driven ». Les valeurs brutes sont donc en **miles**, quel
que soit le réglage d'affichage du véhicule. On convertit à l'affichage plutôt
que de renommer l'unité, ce qui donnerait des kilomètres faux d'un facteur 1,6.
"""

from typing import Any

MILES_EN_KM = 1.609344


def normalise(valeur: Any, prefixe: str) -> str:
    """Retire le préfixe d'énumération. `DetailedChargeStateCharging` -> `Charging`."""
    texte = str(valeur).strip()
    return texte[len(prefixe):] if texte.startswith(prefixe) else texte


CHARGE = {
    "Unknown": "inconnu",
    "Disconnected": "débranchée",
    "NoPower": "sans alimentation",
    "Starting": "démarrage",
    "Charging": "en charge",
    "Complete": "charge terminée",
    "Stopped": "arrêtée",
}

FENETRE = {
    "Unknown": "inconnu",
    "Closed": "fermée",
    "PartiallyOpen": "entrouverte",
    "Opened": "ouverte",
}

HVAC = {
    "Unknown": "inconnu",
    "Off": "éteinte",
    "On": "allumée",
    "Precondition": "préconditionnement",
    "OverheatProtect": "protection surchauffe",
}

LEVIER = {
    "Unknown": "inconnu",
    "Invalid": "invalide",
    "SNA": "indisponible",
    "P": "stationnement",
    "R": "marche arrière",
    "N": "point mort",
    "D": "marche avant",
}

ECRAN = {
    "Unknown": "inconnu",
    "Off": "éteint",
    "Dim": "tamisé",
    "Accessory": "accessoire",
    "On": "allumé",
    "Driving": "conduite",
    "Charging": "charge",
    "Lock": "verrouillé",
    "Sentry": "sentinelle",
    "Dog": "mode chien",
    "Entertainment": "divertissement",
}

SENTINELLE = {
    "Unknown": "inconnue",
    "Off": "désactivée",
    "Idle": "inactive",
    "Armed": "armée",
    "Aware": "en alerte",
    "Panic": "panique",
    "Quiet": "silencieuse",
}

TRAPPE = {
    "Unknown": "inconnu",
    "SNA": "indisponible",
    "Disengaged": "déverrouillée",
    "Engaged": "verrouillée",
    "Blocking": "bloquée",
}

CLIMAT_MAINTIEN = {
    "Unknown": "inconnu",
    "Off": "désactivé",
    "On": "maintien",
    "Dog": "mode chien",
    "Party": "mode camping",
}

SURCHAUFFE = {
    "Unknown": "inconnue",
    "Off": "désactivée",
    "On": "climatisation",
    "FanOnly": "ventilation seule",
}

SURCHAUFFE_LIMITE = {
    "Unknown": "inconnue",
    "Low": "basse (30 °C)",
    "Medium": "moyenne (35 °C)",
    "High": "haute (40 °C)",
}

# Ceintures. Le proto Tesla déclare `DriverSeatBelt` et `PassengerSeatBelt`
# comme des booléens, et l'énumération `BuckleStatus` ne compte que quatre
# valeurs (0 inconnue, 1 détachée, 2 bouclée, 3 en défaut). Le véhicule, lui,
# envoie `4` sur `PassengerSeatBelt` — y compris voiture vide et verrouillée,
# constaté le 23/08. La documentation ne décrit donc pas ce qu'émet la voiture.
#
# Conséquence assumée : on ne conclut que sur ce qui est vérifié. `4` est
# connu comme « pas de ceinture bouclée » (relevé sur une voiture vide) mais
# son encodage exact reste inconnu ; toute autre valeur est journalisée sans
# rien déclencher, le temps d'observer ce que donne un passager réel.
CEINTURE = {
    "Unknown": "inconnue",
    "Unlatched": "détachée",
    "Latched": "bouclée",
    "Faulted": "en défaut",
    "0": "inconnue",
    "1": "détachée",
    "2": "bouclée",
    "3": "en défaut",
    "4": "aucune bouclée",
    "true": "bouclée",
    "True": "bouclée",
    "false": "détachée",
    "False": "détachée",
}

# Valeurs dont on sait qu'elles signifient « bouclée », et celles dont on sait
# qu'elles signifient le contraire. Tout le reste est un inconnu déclaré.
CEINTURE_BOUCLEE = {"Latched", "2", "true", "True"}
CEINTURE_LIBRE = {"Unknown", "Unlatched", "Faulted", "0", "1", "3", "4", "false", "False"}


def ceinture(valeur: Any) -> bool | None:
    """True bouclée, False libre, None si l'encodage n'est pas reconnu.

    Le None est délibéré : il vaut mieux ne rien conclure que déclencher une
    alarme sur une valeur qu'on n'a jamais observée.
    """
    if isinstance(valeur, bool):
        return valeur
    court = normalise(valeur, "BuckleStatus").strip('"')
    if court in CEINTURE_BOUCLEE:
        return True
    if court in CEINTURE_LIBRE:
        return False
    return None

# Champ -> (préfixe de l'énumération, table de traduction)
ENUMS: dict[str, tuple[str, dict[str, str]]] = {
    "PassengerSeatBelt": ("BuckleStatus", CEINTURE),
    "DriverSeatBelt": ("BuckleStatus", CEINTURE),
    "ClimateKeeperMode": ("ClimateKeeperModeState", CLIMAT_MAINTIEN),
    "CabinOverheatProtectionMode": ("CabinOverheatProtectionModeState", SURCHAUFFE),
    "CabinOverheatProtectionTemperatureLimit": ("ClimateOverheatProtectionTempLimit", SURCHAUFFE_LIMITE),
    "CenterDisplay": ("DisplayState", ECRAN),
    "SentryMode": ("SentryModeState", SENTINELLE),
    "ChargePortLatch": ("ChargePortLatch", TRAPPE),
    "DetailedChargeState": ("DetailedChargeState", CHARGE),
    "FdWindow": ("WindowState", FENETRE),
    "FpWindow": ("WindowState", FENETRE),
    "RdWindow": ("WindowState", FENETRE),
    "RpWindow": ("WindowState", FENETRE),
    "HvacPower": ("HvacPowerState", HVAC),
    "Gear": ("ShiftState", LEVIER),
}

# Champs dont la valeur brute est exprimée en miles.
DISTANCES = {"Odometer", "EstBatteryRange"}

# Vitesse, en miles par heure à la source.
VITESSES = {"VehicleSpeed"}

# Champs exprimés en degrés Celsius.
TEMPERATURES = {"InsideTemp", "OutsideTemp", "HvacLeftTemperatureRequest",
                "HvacRightTemperatureRequest"}


def valeur_courte(champ: str, valeur: Any) -> str | None:
    """Valeur d'énumération sans son préfixe, ou None si le champ n'en est pas une."""
    entree = ENUMS.get(champ)
    if entree is None:
        return None
    return normalise(valeur, entree[0])


def en_km(miles: float) -> float:
    return miles * MILES_EN_KM


def distance(valeur: Any, unite: str) -> float | None:
    """Convertit une distance brute (miles) vers l'unité d'affichage."""
    try:
        brut = float(valeur)
    except (TypeError, ValueError):
        return None
    return brut if unite == "mi" else en_km(brut)


def libelle(champ: str, valeur: Any, unite_distance: str = "km") -> str | None:
    """Chaîne prête à afficher, ou None si la valeur brute suffit."""
    if champ in ENUMS:
        prefixe, table = ENUMS[champ]
        court = normalise(valeur, prefixe).strip('"')
        return table.get(court, court)

    if champ in DISTANCES:
        converti = distance(valeur, unite_distance)
        if converti is None:
            return None
        return f"{converti:,.0f} {unite_distance}".replace(",", " ")

    if champ in VITESSES:
        converti = distance(valeur, unite_distance)
        if converti is None:
            return None
        return f"{converti:.0f} {unite_distance}/h"

    if champ in TEMPERATURES:
        try:
            return f"{float(valeur):.1f} °C"
        except (TypeError, ValueError):
            return None

    if champ == "Locked":
        return "verrouillé" if valeur else "déverrouillé"

    return None
