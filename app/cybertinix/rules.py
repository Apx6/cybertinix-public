"""Moteur de règles : transforme un flux de signaux en notifications.

Le flux est déjà filtré à la source — le véhicule n'émet que sur changement de
valeur, au-delà du `minimum_delta` configuré. Une règle voit donc des
transitions, pas un échantillonnage.

Deux besoins imposent de la mémoire malgré tout :

  - Les seuils. « Batterie sous 20 % » se déclencherait à chaque signal reçu
    tant que la valeur reste basse. On mémorise le dernier état annoncé et on
    n'émet que sur bascule.
  - Les trajets. Une distance parcourue se calcule entre deux instants, donc
    il faut retenir l'odomètre du départ.

C'est le rôle de la table `alert_states`.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import alerts, enums, prefs, security, trips
from .models import AlertState, Signal
from .notify import send

log = logging.getLogger(__name__)


# --- Décodage ----------------------------------------------------------------


def decode(raw: str) -> Any:
    """Les valeurs arrivent encodées en JSON, mais pas toujours.

    Tesla prévient que le type d'un champ peut changer d'une version de
    firmware à l'autre : 12.3 ici, "12.3" ailleurs. On décode au mieux et les
    règles restent tolérantes.
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def as_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_GEARS = {
    "P": {"P", "PARK"},
    "R": {"R", "REVERSE"},
    "N": {"N", "NEUTRAL"},
    "D": {"D", "DRIVE"},
}


def as_gear(value: Any) -> str | None:
    """Normalise la position du levier.

    Le véhicule envoie `ShiftStateP` ; on retire le préfixe puis on accepte
    quelques variantes, au cas où une version de firmware s'en écarterait.
    """
    text = enums.normalise(value, "ShiftState").strip().upper().replace("_", "")
    for gear, variants in _GEARS.items():
        if text in variants:
            return gear
    return None


# Tesla nomme ses ouvrants en anglais : sans traduction, la notification
# annoncerait « Ouverture : DriverFront ».
OUVRANTS = {
    "DriverFront": "porte conducteur",
    "PassengerFront": "porte passager",
    "DriverRear": "porte arrière gauche",
    "PassengerRear": "porte arrière droite",
    "TrunkFront": "coffre avant",
    "TrunkRear": "coffre arrière",
}


def open_doors(value: Any) -> list[str]:
    """Portes et coffres ouverts. `DoorState` arrive comme un objet par ouvrant."""
    if isinstance(value, dict):
        return [OUVRANTS.get(nom, nom) for nom, ouvert in value.items() if ouvert]
    return []


# --- Contexte d'évaluation ---------------------------------------------------


@dataclass
class Context:
    session: AsyncSession
    vin: str
    name: str
    value: Any
    previous: Any

    async def latest(self, field: str) -> Any:
        """Dernière valeur connue d'un autre champ.

        Utile pour enrichir une notification : le niveau de batterie au moment
        où la charge se termine, par exemple. `include_fields` garantit que ces
        champs viennent d'être publiés, donc la valeur est fraîche.
        """
        raw = await self.session.scalar(
            select(Signal.value)
            .where(Signal.vin == self.vin, Signal.name == field)
            .order_by(Signal.id.desc())
            .limit(1)
        )
        return decode(raw) if raw is not None else None

    async def recall(self, key: str) -> str | None:
        return await self.session.scalar(
            select(AlertState.state).where(AlertState.vin == self.vin, AlertState.key == key)
        )

    async def remember(self, key: str, state: str) -> None:
        row = await self.session.scalar(
            select(AlertState).where(AlertState.vin == self.vin, AlertState.key == key)
        )
        if row is None:
            self.session.add(AlertState(vin=self.vin, key=key, state=state))
        else:
            row.state = state
        await self.session.commit()

    async def latch(self, key: str, state: str) -> bool:
        """Vrai une seule fois par transition.

        Rend une règle de seuil idempotente : tant que l'état annoncé ne change
        pas, elle se tait.
        """
        if await self.recall(key) == state:
            return False
        await self.remember(key, state)
        return True


Rule = Callable[[Context], Awaitable[str | None]]
_rules: list[Rule] = []


def rule(fn: Rule) -> Rule:
    _rules.append(fn)
    return fn


async def evaluate(session: AsyncSession, vin: str, name: str, raw: str, previous: str | None) -> None:
    ctx = Context(
        session=session,
        vin=vin,
        name=name,
        value=decode(raw),
        previous=decode(previous) if previous is not None else None,
    )
    for fn in _rules:
        try:
            message = await fn(ctx)
        except Exception:  # noqa: BLE001 — une règle fautive ne doit pas bloquer les autres
            log.exception("règle %s en échec sur %s", fn.__name__, name)
            continue
        if message:
            await send(message)


# --- Sécurité ----------------------------------------------------------------


@rule
async def deverrouillage(ctx: Context) -> str | None:
    if not prefs.get("notif_acces"):
        return None
    if ctx.name != "Locked":
        return None
    # Uniquement sur transition verrouillé -> déverrouillé. Sans le test sur
    # l'état précédent, un redémarrage du serveur rejouerait l'alerte.
    if ctx.value in (False, "false", "Unlocked") and ctx.previous not in (None, False, "false"):
        return "🔓 Véhicule déverrouillé."
    return None


@rule
async def ouverture(ctx: Context) -> str | None:
    if not prefs.get("notif_acces"):
        return None
    if ctx.name != "DoorState":
        return None
    now_open = open_doors(ctx.value)
    was_open = open_doors(ctx.previous)
    nouvelles = [d for d in now_open if d not in was_open]
    if nouvelles:
        return f"🚪 Ouverture : {', '.join(nouvelles)}"
    if was_open and not now_open:
        return "🚪 Tout est refermé."
    return None


@rule
async def _levier_inconnu(ctx: Context) -> str | None:
    """Filet : signale une variante de `Gear` que la normalisation ignore.

    Tesla ne documente pas la forme exacte de ce champ. Sans ce contrôle, une
    valeur inattendue ferait disparaître les notifications de trajet en
    silence, sans que rien n'indique pourquoi.
    """
    if ctx.name != "Gear" or ctx.value is None:
        return None
    if as_gear(ctx.value) is None and str(ctx.value).strip().lower() not in ("", "invalid"):
        log.warning("valeur de Gear non reconnue : %r", ctx.value)
    return None


@rule
async def sentinelle(ctx: Context) -> str | None:
    """Si la sentinelle est malgré tout active, on relaie ses alertes."""
    if ctx.name == "SentryMode":
        etat = enums.normalise(ctx.value, "SentryModeState")
        avant = enums.normalise(ctx.previous, "SentryModeState")
        if etat in ("Aware", "Panic") and avant not in ("Aware", "Panic"):
            return f"🚨 Sentinelle : {enums.SENTINELLE.get(etat, etat)}."
        return None
    if ctx.name.startswith("alert:") and "Sentry" in ctx.name:
        return f"🚨 Alerte sentinelle : {ctx.name.removeprefix('alert:')}"
    return None


# --- Mises à jour logicielles Tesla ------------------------------------------


@rule
async def mise_a_jour_disponible(ctx: Context) -> str | None:
    """Une nouvelle version est proposée par Tesla, ou vient d'être installée."""
    if ctx.name == "SoftwareUpdateVersion":
        nouvelle = str(ctx.value or "").strip()
        avant = str(ctx.previous or "").strip()
        if nouvelle and nouvelle != avant:
            return (f"⬇️ Mise à jour Tesla disponible : {nouvelle}.\n"
                    "Notes de version et installation dans l'onglet Entretien.")
        return None
    if ctx.name == "Version":
        nouvelle = str(ctx.value or "").strip()
        avant = str(ctx.previous or "").strip()
        if nouvelle and avant and nouvelle != avant:
            return f"✅ Firmware installé : {nouvelle} (précédent : {avant})."
    return None


# --- Sécurité sans sentinelle ------------------------------------------------
# La logique de corrélation vit dans security.py ; ces règles ne font que lui
# transmettre les événements et alerter sur les cas sans ambiguïté.


@rule
async def _memoriser_ouvertures(ctx: Context) -> str | None:
    """Les causes innocentes d'un réveil, pour la corrélation.

    Le déverrouillage est le plus important : avec la clé téléphone, il
    précède l'ouverture de porte de plusieurs secondes, et c'est lui qui
    distingue le propriétaire d'un intrus.
    """
    if ctx.name == "Locked":
        if ctx.value in (False, "false") and ctx.previous not in (None, False, "false"):
            security.noter(ctx.vin, "deverrouillage")
    elif ctx.name == "DoorState" and open_doors(ctx.value):
        security.noter(ctx.vin, "porte")
    elif ctx.name == "ChargePortLatch":
        if enums.normalise(ctx.value, "ChargePortLatch") == "Disengaged":
            security.noter(ctx.vin, "trappe")
    return None


@rule
async def ecran_dans_habitacle_verrouille(ctx: Context) -> str | None:
    """Le signal d'intrusion principal. Vérification différée dans security.py."""
    if ctx.name != "CenterDisplay":
        return None
    etat = enums.normalise(ctx.value, "DisplayState")
    avant = enums.normalise(ctx.previous, "DisplayState")
    if etat == "Lock" and avant != "Lock":
        await security.ecran_verrouille(ctx.vin)
    return None


@rule
async def porte_forcee(ctx: Context) -> str | None:
    """Une porte s'ouvre alors que la voiture reste verrouillée.

    Avec une clé, la voiture se déverrouille avant que la porte s'ouvre — mais
    `Locked` et `DoorState` arrivent dans des messages distincts, sans ordre
    garanti. On laisse donc quelques secondes au déverrouillage pour arriver
    avant de conclure ; la vérification est dans security.py.
    """
    if ctx.name != "DoorState":
        return None
    nouvelles = [d for d in open_doors(ctx.value) if d not in open_doors(ctx.previous)]
    if nouvelles:
        await security.porte_ouverte(ctx.vin, nouvelles)
    return None


@rule
async def armement(ctx: Context) -> str | None:
    """Tient à jour l'état armé / désarmé du véhicule.

    Armé : verrouillé à l'arrêt, siège vide — le propriétaire est parti.
    Désarmé : déverrouillé par la clé, ou en route. Une Tesla verrouille ses
    portes toute seule quand elle roule ; sans cet état, le conducteur qui
    bouge sur son siège est pris pour un intrus.
    """
    if ctx.name == "Locked":
        if ctx.value in (False, "false") and ctx.previous not in (False, "false"):
            await security.armer(ctx.vin, False, "déverrouillage")
        elif ctx.value in (True, "true") and ctx.previous not in (True, "true"):
            gear = as_gear(await ctx.latest("Gear") or "")
            siege = await ctx.latest("DriverSeatOccupied")
            if gear in (None, "P") and siege is not True:
                await security.armer(ctx.vin, True, "verrouillage à l'arrêt, siège vide")
            else:
                await security.armer(ctx.vin, False, "verrouillage en route ou avec quelqu'un à bord")
    elif ctx.name == "Gear":
        if as_gear(ctx.value) not in (None, "P"):
            await security.armer(ctx.vin, False, "véhicule en route")
    elif ctx.name == "VehicleSpeed":
        if (as_number(ctx.value) or 0) > 0:
            await security.armer(ctx.vin, False, "véhicule en mouvement")
    elif ctx.name == "SentryMode":
        # La sentinelle qui s'arme prouve un verrouillage que la voiture n'a
        # pas forcément signalé par `Locked`. Sans ce rattrapage, la
        # surveillance reste désarmée sur une voiture pourtant fermée.
        etat = enums.normalise(ctx.value, "SentryModeState").strip('"')
        if etat in ("Armed", "Aware", "Panic") and await security.evaluer_armement(ctx.vin):
            await security.armer(ctx.vin, True, "sentinelle armée (verrouillage déduit)")
    return None


@rule
async def siege_occupe(ctx: Context) -> str | None:
    """Quelqu'un s'assoit : vérification différée dans security.py."""
    if ctx.name != "DriverSeatOccupied":
        return None
    if ctx.value in (True, "true") and ctx.previous not in (True, "true"):
        await security.siege_occupe(ctx.vin)
    return None


@rule
async def ceinture(ctx: Context) -> str | None:
    """Observation des ceintures, sans conclusion propre.

    Une ceinture bouclée **ne déclenche pas d'alerte** : ce n'est pas un
    critère décisif — un intrus ne s'attache pas, et `PassengerSeatBelt`
    émet une valeur (`4`) que la documentation Tesla ne décrit pas. Choix du
    propriétaire, le 25/08 : la détection reste fondée sur l'occupation du
    siège, seul capteur qui dise vraiment que quelqu'un est à bord.

    Les ceintures gardent un rôle d'appui dans `security._corroboration` :
    elles ne concluent jamais seules, elles confirment un autre signal. On
    continue donc à les recevoir et à journaliser les encodages inconnus.
    """
    if ctx.name not in ("DriverSeatBelt", "PassengerSeatBelt"):
        return None
    if enums.ceinture(ctx.value) is None:
        log.warning("valeur de %s non reconnue : %r — aucune conclusion tirée",
                    ctx.name, ctx.value)
    return None


@rule
async def quitte_le_domicile(ctx: Context) -> str | None:
    """`LocatedAtHome` passe à faux : la voiture n'est plus chez toi."""
    if ctx.name != "LocatedAtHome":
        return None
    if ctx.value in (False, "false") and ctx.previous in (True, "true"):
        await security.depart_du_domicile(ctx.vin)
    return None


@rule
async def alarme_integree(ctx: Context) -> str | None:
    """L'alarme antivol de série, distincte de la sentinelle, remonte en alerte.

    Le véhicule émet en continu des alertes de diagnostic (Autopilot, charge,
    service) sans rapport avec la sécurité. Seules celles du calculateur VCSEC
    ou au libellé explicite comptent — voir alerts.py.
    """
    if not ctx.name.startswith("alert:"):
        return None
    nom = ctx.name.removeprefix("alert:")
    if alerts.est_securite(nom):
        # L'alarme de série a ses propres capteurs : on la relaie sans exiger
        # l'état de verrouillage, qu'on ne connaît pas toujours à cet instant.
        await security.intrusion(ctx.vin, f"alarme du véhicule : {nom}", exiger_verrou=False)
    return None


@rule
async def pneu_signale_par_le_vehicule(ctx: Context) -> str | None:
    """Le véhicule a sa propre surveillance des pneus ; on la relaie comme un
    seuil, pas comme une alarme."""
    if not prefs.get("notif_seuils") or not ctx.name.startswith("alert:"):
        return None
    nom = ctx.name.removeprefix("alert:")
    if alerts.est_pneu(nom):
        niveau = "sévère" if "hard" in nom.lower() else "légère"
        return f"🛞 Le véhicule signale une pression de pneu anormale ({niveau})."
    return None


# --- Charge ------------------------------------------------------------------

_EN_CHARGE = {"Charging", "Starting"}
_ARRETS = {"Stopped", "NoPower", "Disconnected"}


@rule
async def charge(ctx: Context) -> str | None:
    if not prefs.get("notif_charge"):
        return None
    if ctx.name != "DetailedChargeState":
        return None

    # Le véhicule envoie `DetailedChargeStateCharging`, pas `Charging` : sans
    # retrait du préfixe, aucune de ces comparaisons ne réussit.
    etat = enums.normalise(ctx.value, "DetailedChargeState")
    avant = enums.normalise(ctx.previous, "DetailedChargeState")

    if etat == "Complete" and avant != "Complete":
        soc = await ctx.latest("Soc")
        autonomie = await ctx.latest("EstBatteryRange")
        details = " · ".join(
            part
            for part in (
                f"{soc} %" if soc is not None else "",
                f"{autonomie} d'autonomie" if autonomie is not None else "",
            )
            if part
        )
        return f"🔋 Charge terminée{' — ' + details if details else ''}."

    if etat in _EN_CHARGE and avant not in _EN_CHARGE:
        soc = await ctx.latest("Soc")
        return f"⚡ Charge démarrée{f' à {soc} %' if soc is not None else ''}."

    # Une charge qui s'arrête sans être terminée mérite une alerte : câble
    # débranché, borne coupée, ou coupure secteur pendant la nuit.
    if etat in _ARRETS and avant in _EN_CHARGE:
        soc = await ctx.latest("Soc")
        motif = enums.CHARGE.get(etat, etat)
        return f"⚠️ Charge interrompue{f' à {soc} %' if soc is not None else ''} — {motif}."

    return None


# --- Seuils batterie et pneus ------------------------------------------------


@rule
async def batterie_basse(ctx: Context) -> str | None:
    if not prefs.get("notif_seuils"):
        return None
    if ctx.name != "Soc":
        return None
    soc = as_number(ctx.value)
    if soc is None:
        return None

    seuil = prefs.get("battery_low_percent")
    etat = "basse" if soc < seuil else "ok"
    if not await ctx.latch("batterie", etat):
        return None
    if etat == "basse":
        return f"🪫 Batterie à {soc:.0f} %, sous le seuil de {seuil:.0f} %."
    return None


@rule
async def pression_pneu(ctx: Context) -> str | None:
    if not ctx.name.startswith("TpmsPressure"):
        return None
    pression = as_number(ctx.value)
    if pression is None:
        return None

    roue = {
        "TpmsPressureFl": "avant gauche",
        "TpmsPressureFr": "avant droit",
        "TpmsPressureRl": "arrière gauche",
        "TpmsPressureRr": "arrière droit",
    }.get(ctx.name, ctx.name)

    seuil = prefs.get("tpms_min_pressure")
    etat = "basse" if pression < seuil else "ok"
    if not await ctx.latch(f"tpms:{ctx.name}", etat):
        return None
    if etat == "basse":
        return f"🛞 Pneu {roue} à {pression} bar, sous le seuil de {seuil} bar."
    return f"🛞 Pneu {roue} de nouveau correct ({pression} bar)."


# --- Trajets -----------------------------------------------------------------


@rule
async def _enregistrer_trajet(ctx: Context) -> None:
    """Historique des déplacements, tenu que les notifications de trajet
    soient activées ou non. Même bornage que la notification : le levier."""
    if ctx.name != "Gear":
        return None
    gear, avant = as_gear(ctx.value), as_gear(ctx.previous)
    if gear is None or gear == avant:
        return None

    contexte = dict(
        position=await ctx.latest("Location"),
        odometre=await ctx.latest("Odometer"),
        soc=await ctx.latest("Soc"),
    )
    if gear != "P" and (avant == "P" or avant is None):
        await trips.demarrer(ctx.session, ctx.vin, **contexte)
    elif gear == "P" and avant is not None:
        await trips.terminer(ctx.session, ctx.vin, **contexte)
    return None


@rule
async def trajet(ctx: Context) -> str | None:
    """Départ et arrivée, bornés par la position du levier.

    Plus fiable que la vitesse : un arrêt à un feu ramène la vitesse à zéro,
    alors que le passage en P marque une vraie fin de trajet.
    """
    if not prefs.get("notif_trajets"):
        return None
    if ctx.name != "Gear":
        return None

    gear, avant = as_gear(ctx.value), as_gear(ctx.previous)
    if gear is None or gear == avant:
        return None

    odometre = as_number(await ctx.latest("Odometer"))

    if gear != "P" and (avant == "P" or avant is None):
        if odometre is not None:
            await ctx.remember("trajet:depart", str(odometre))
        return "🚗 Départ."

    if gear == "P" and avant is not None:
        depart = as_number(await ctx.recall("trajet:depart"))
        if depart is not None and odometre is not None and odometre > depart:
            # L'odomètre est en miles à la source, quelle que soit l'unité
            # affichée dans la voiture : on convertit avant d'annoncer.
            unite = prefs.get("distance_unit")
            parcouru = enums.distance(odometre - depart, unite)
            return f"🅿️ Arrivée — {parcouru:.1f} {unite} parcourus."
        return "🅿️ Arrivée."

    return None
