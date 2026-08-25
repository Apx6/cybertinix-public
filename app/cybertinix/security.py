"""Sécurité du véhicule sans mode Sentinelle.

Le principe, emprunté à SentryGuard après lecture de son code : une voiture
endormie n'émet rien, mais **une intrusion la réveille**. Au réveil, le client
télémétrie se reconnecte et pousse l'état du véhicule. Ce n'est donc pas le
serveur qui surveille — c'est l'effraction elle-même qui déclenche le rapport.
Coût énergétique tant qu'il ne se passe rien : zéro. La sentinelle, elle,
maintient la voiture éveillée en permanence pour environ 1 % de batterie par
heure.

Le signal décisif est `CenterDisplay` passant à `Lock` : l'écran central s'est
allumé en affichant le cadenas, ce qui n'arrive que si quelqu'un se trouve dans
un habitacle verrouillé. Deux causes légitimes produisent le même signal, et
sont écartées par corrélation temporelle :

  - le propriétaire ouvre une porte avec sa clé (DoorState dans les 4 s)
  - quelqu'un branche ou débranche la trappe (ChargePortLatch dans les 5 s)

L'alerte est donc différée de quelques secondes, le temps de voir si une cause
innocente l'explique. C'est le prix d'un détecteur qui ne crie pas au loup.

Deux ripostes sont possibles sur intrusion confirmée, toutes deux désactivées
par défaut parce qu'elles agissent sur le véhicule :

  - **sentinelle à la demande** : allumer le mode Sentinelle, donc les caméras,
    uniquement quand une intrusion est détectée. On obtient les images sans
    payer la surveillance les 99,9 % du temps où il ne se passe rien.
  - **klaxon** : dissuasion immédiate.
"""

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import select

from . import enums, oauth, prefs
from .db import SessionLocal
from .fleet import FleetClient
from .models import AlertState, Signal, VehicleAlert
from .notify import send

log = logging.getLogger(__name__)

# Fenêtres de corrélation, en secondes. Porte et trappe reprises de SentryGuard.
FENETRE_PORTE = 4.0
FENETRE_TRAPPE = 5.0
DELAI_VERIFICATION = 3.0

# Le signal qui innocente vraiment : un déverrouillage par la clé. Avec la clé
# téléphone, un Model X se déverrouille à l'approche du propriétaire, plusieurs
# secondes avant l'ouverture de la porte — et c'est à ce réveil que l'écran
# central rapporte son état « verrouillé ». Corréler sur la porte seule, à 4 s,
# a fait klaxonner la voiture sur son propriétaire. Un voleur, lui, ne produit
# pas de `Locked: true → false`.
FENETRE_DEVERROUILLAGE = 30.0

# Entre le déverrouillage et la sortie effective de la zone « domicile », il
# peut s'écouler le temps de charger le coffre et de sortir du garage.
FENETRE_DEPART = 15 * 60.0

# Au-delà, un événement mémorisé ne sert plus à rien. Aligné sur la plus
# longue fenêtre de corrélation, celle du départ du domicile.
RETENTION = FENETRE_DEPART + 60.0

_evenements: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
_verifications: set[asyncio.Task] = set()


def _maintenant() -> float:
    return datetime.now(UTC).timestamp()


def noter(vin: str, evenement: str) -> None:
    """Mémorise un événement innocent susceptible d'expliquer un réveil d'écran."""
    t = _maintenant()
    liste = _evenements[vin][evenement]
    liste.append(t)
    liste[:] = [x for x in liste if t - x < RETENTION]


def _evenement_autour(vin: str, evenement: str, instant: float, fenetre: float) -> bool:
    return any(abs(instant - x) <= fenetre for x in _evenements[vin][evenement])


async def ecran_verrouille(vin: str) -> None:
    """L'écran central rapporte l'état « verrouillé ».

    ⚠️ Ce signal ne prouve rien à lui seul, contrairement à ce qu'on a cru au
    départ. `DisplayStateLock` n'est pas « l'écran vient de s'allumer » : c'est
    l'état de repos de l'écran d'une voiture verrouillée. La voiture le rejoue
    donc dans l'instantané qu'elle pousse à **chaque reconnexion**, portes
    closes et siège vide. Le 23/08, ses trois seules apparitions en base
    tombaient à la seconde exacte d'une reconnexion, et ont déclenché une
    fausse intrusion — suivie d'une riposte qui a allumé la sentinelle, soit
    exactement la dépense que ce projet existe pour éviter.
    D'où l'exigence de corroboration dans `_verifier`.

    On ne conclut pas tout de suite : une ouverture de porte légitime arrive
    parfois quelques centaines de millisecondes *après* le réveil de l'écran.
    La vérification est différée, et seule une absence de cause innocente
    déclenche l'alerte.
    """
    # Un événement sur une voiture désarmée n'est jamais une intrusion, même si
    # elle s'arme pendant le délai de vérification : on tranche tout de suite.
    if not await _armee(vin):
        return
    instant = _maintenant()
    tache = asyncio.create_task(_verifier(vin, instant))
    _verifications.add(tache)
    tache.add_done_callback(_verifications.discard)


async def _innocente(vin: str, instant: float, *, sauf: str = "") -> str | None:
    """Motif innocent expliquant un réveil, ou None.

    `sauf` retire un critère : le détecteur de porte forcée ne doit pas se
    faire innocenter par l'ouverture de porte qu'il est en train d'examiner.
    """
    if not await _armee(vin):
        return "véhicule non armé (en route, ou propriétaire à bord)"
    if _evenement_autour(vin, "deverrouillage", instant, FENETRE_DEVERROUILLAGE):
        return "déverrouillage par la clé"
    if sauf != "porte" and _evenement_autour(vin, "porte", instant, FENETRE_PORTE):
        return "ouverture de porte"
    if _evenement_autour(vin, "trappe", instant, FENETRE_TRAPPE):
        return "trappe de charge"
    if not await _verrouillee(vin):
        return "véhicule déverrouillé"
    return None


# --- État d'armement ---------------------------------------------------------
#
# Une alarme de maison ne se déclenche pas quand on marche dans son salon :
# elle n'est active que lorsqu'on est sorti et qu'on l'a armée. Même chose ici.
# « Verrouillée » ne suffit pas — une Tesla verrouille ses portes toute seule
# dès qu'elle roule, et le conducteur qui bouge sur son siège ressemble alors
# à un intrus. La voiture a klaxonné cinq fois sur son propriétaire avant qu'on le
# comprenne.
#
# La voiture est ARMÉE quand elle a été verrouillée à l'arrêt, siège vide :
# le propriétaire est parti. Elle est DÉSARMÉE dès qu'elle est déverrouillée
# par la clé, ou dès qu'elle roule. Aucun détecteur ne conclut à une intrusion
# sur une voiture désarmée.

CLE_ARMEE = "armee"


async def _dernier(session, vin: str, champ: str) -> str | None:
    return await session.scalar(
        select(Signal.value)
        .where(Signal.vin == vin, Signal.name == champ)
        .order_by(Signal.id.desc())
        .limit(1)
    )


def _vrai(brut: str | None) -> bool:
    return brut is not None and brut.strip().lower() == "true"


def _en_stationnement(brut: str | None) -> bool:
    # Levier inconnu = on ne contredit pas l'armement ; seul un rapport
    # explicite de conduite désarme.
    if brut is None:
        return True
    return brut.strip('"').removeprefix("ShiftState").upper() in ("P", "PARK", "UNKNOWN", "INVALID", "SNA", "")


async def armer(vin: str, armee: bool, motif: str) -> None:
    async with SessionLocal() as session:
        row = await session.scalar(
            select(AlertState).where(AlertState.vin == vin, AlertState.key == CLE_ARMEE)
        )
        etat = "1" if armee else "0"
        if row is None:
            session.add(AlertState(vin=vin, key=CLE_ARMEE, state=etat))
        elif row.state != etat:
            row.state = etat
        else:
            return
        await session.commit()
    if armee:
        _evenements[vin]["deverrouillage"].clear()
    log.info("véhicule %s : %s", "ARMÉ" if armee else "désarmé", motif)


async def evaluer_armement(vin: str) -> bool:
    """Décide de l'armement d'après l'état courant, quand aucune transition ne
    l'a fixé — typiquement après un redémarrage du serveur."""
    async with SessionLocal() as session:
        verrou = (await etat_verrou(session, vin))["verrouille"]
        gear = await _dernier(session, vin, "Gear")
        siege = _vrai(await _dernier(session, vin, "DriverSeatOccupied"))
    return verrou and _en_stationnement(gear) and not siege


async def _armee(vin: str) -> bool:
    """État d'armement, avec rattrapage d'un cliquet resté en arrière.

    L'armement est posé par transitions (`Locked`, `Gear`, `VehicleSpeed`).
    Quand la transition n'arrive jamais, le cliquet reste faux indéfiniment :
    le 23/08, la voiture s'est re-verrouillée sans émettre `Locked`, et la
    surveillance est restée désarmée une demi-heure sur une voiture fermée,
    sentinelle armée — aucun détecteur n'aurait conclu.

    D'où le rattrapage : si l'état constaté (verrouillée, à l'arrêt, siège
    vide) dit « armée » alors que le cliquet dit le contraire, l'état constaté
    l'emporte. L'inverse n'est pas vrai — un cliquet armé n'est levé que par
    une vraie transition, sinon un signal manquant désarmerait la voiture.
    """
    async with SessionLocal() as session:
        etat = await session.scalar(
            select(AlertState.state).where(AlertState.vin == vin, AlertState.key == CLE_ARMEE)
        )
    if etat is None:
        return await evaluer_armement(vin)
    if etat != "1" and await evaluer_armement(vin):
        await armer(vin, True, "rattrapage : état constaté armé, cliquet en retard")
        return True
    return etat == "1"


async def armee_pour(vin: str) -> bool:
    """État d'armement, pour l'interface."""
    return await _armee(vin)


async def _dernier_date(session, vin: str, champ: str):
    """Dernière valeur d'un champ, avec sa date. (None, None) si jamais reçue."""
    ligne = await session.execute(
        select(Signal.value, Signal.received_at)
        .where(Signal.vin == vin, Signal.name == champ)
        .order_by(Signal.id.desc())
        .limit(1)
    )
    row = ligne.first()
    return (None, None) if row is None else (row[0], row[1])


# La sentinelle ne s'arme que sur une voiture verrouillée : `Armed`, `Aware` et
# `Panic` impliquent le verrou. `Idle` non — c'est la sentinelle activée mais
# en veille, propriétaire à bord.
_SENTINELLE_VERROUILLEE = ("Armed", "Aware", "Panic")


async def etat_verrou(session, vin: str) -> dict:
    """Verrouillage effectif, et non simple recopie du dernier `Locked`.

    La voiture n'émet pas toujours son re-verrouillage. Constaté le 23/08 :
    plus aucun `Locked` après le 12:38:41 (`false`, le propriétaire descend),
    alors que la sentinelle s'est armée à 12:42:09 — donc sur une voiture
    verrouillée. L'interface annonçait « Déverrouillé » une demi-heure durant,
    et surtout la surveillance restait **désarmée** : plus aucun détecteur
    n'aurait conclu. Un `SentryMode` verrouillant plus récent que le dernier
    `Locked` fait donc foi.
    """
    brut, quand = await _dernier_date(session, vin, "Locked")
    verrouille = brut is not None and brut.strip().lower() == "true"
    deduit = False

    sentinelle, quand_s = await _dernier_date(session, vin, "SentryMode")
    if sentinelle is not None and quand_s is not None:
        etat = sentinelle.strip('"').removeprefix("SentryModeState")
        plus_recent = quand is None or quand_s > quand
        if etat in _SENTINELLE_VERROUILLEE and plus_recent and not verrouille:
            verrouille, deduit, quand = True, True, quand_s

    return {
        "verrouille": verrouille,
        "deduit": deduit,
        "quand": quand.isoformat() if quand is not None else None,
    }


async def _verrouillee(vin: str) -> bool:
    """Verrouillage effectif. En cas de doute, on considère le véhicule
    déverrouillé : une alerte manquée vaut mieux qu'un klaxon sur le
    propriétaire."""
    async with SessionLocal() as session:
        return (await etat_verrou(session, vin))["verrouille"]


async def _corroboration(vin: str) -> str | None:
    """Anomalie physique constatée dans l'habitacle, ou None si tout est en ordre.

    L'état de l'écran ne prouve rien à lui seul (voir `ecran_verrouille`). On
    ne conclut que si quelque chose d'autre ne va pas : un ouvrant, une place
    occupée, un verrou tombé. Un intrus réel produit forcément l'un des trois.

    Les ceintures figurent ici, et **ici seulement** : elles confirment un
    signal, elles n'en déclenchent jamais. Ce n'est pas un critère décisif —
    un intrus ne s'attache pas.
    """
    async with SessionLocal() as session:
        if _vrai(await _dernier(session, vin, "DriverSeatOccupied")):
            return "siège conducteur occupé"
        if enums.ceinture(await _dernier(session, vin, "DriverSeatBelt")):
            return "ceinture conducteur bouclée"
        if enums.ceinture(await _dernier(session, vin, "PassengerSeatBelt")):
            return "ceinture passager bouclée"
        portes = await _dernier(session, vin, "DoorState")
        if portes and "true" in portes.lower():
            return "un ouvrant est ouvert"
        if not (await etat_verrou(session, vin))["verrouille"]:
            return "véhicule déverrouillé"
    return None


async def _verifier(vin: str, instant: float) -> None:
    await asyncio.sleep(DELAI_VERIFICATION)
    motif = await _innocente(vin, instant)
    if motif:
        log.info("réveil d'écran expliqué par : %s — pas d'alerte", motif)
        return
    # L'écran seul ne suffit pas : il faut qu'autre chose cloche réellement.
    corrobore = await _corroboration(vin)
    if corrobore is None:
        log.info("écran en mode verrouillé mais habitacle intact — pas d'alerte")
        return
    await intrusion(vin, f"écran allumé dans un habitacle verrouillé ({corrobore})")


async def siege_occupe(vin: str) -> None:
    """Le siège conducteur vient d'être occupé.

    C'est le signal d'intrusion le plus direct : il n'y a pas d'explication
    innocente à quelqu'un d'assis dans une voiture verrouillée. On garde le
    délai de vérification uniquement pour laisser arriver un déverrouillage
    par la clé qui serait dans le même lot de messages.
    """
    # Un événement sur une voiture désarmée n'est jamais une intrusion, même si
    # elle s'arme pendant le délai de vérification : on tranche tout de suite.
    if not await _armee(vin):
        return
    instant = _maintenant()
    tache = asyncio.create_task(_verifier_siege(vin, instant))
    _verifications.add(tache)
    tache.add_done_callback(_verifications.discard)


async def _verifier_siege(vin: str, instant: float) -> None:
    await asyncio.sleep(DELAI_VERIFICATION)
    motif = await _innocente(vin, instant)
    if motif:
        log.info("siège occupé expliqué par : %s — pas d'alerte", motif)
        return
    await intrusion(vin, "siège conducteur occupé dans un véhicule armé")


async def depart_du_domicile(vin: str) -> None:
    """La voiture vient de quitter le domicile.

    Légitime si elle a été déverrouillée par la clé dans les minutes qui
    précèdent ; sinon c'est un déplacement sans clé — remorquage ou vol. On
    alerte toujours, mais sans riposte : klaxonner une voiture sur une
    dépanneuse n'apporte rien, et la sentinelle ne filme pas en roulant.
    """
    if _evenement_autour(vin, "deverrouillage", _maintenant(), FENETRE_DEPART):
        return
    await send(
        "🚗🚨 La voiture a quitté le domicile sans déverrouillage par la clé.\n"
        "Remorquage, ou vol. Position dans l'application."
    )
    async with SessionLocal() as session:
        session.add(VehicleAlert(vin=vin, name="intrusion", payload="départ du domicile sans clé"))
        await session.commit()


async def porte_ouverte(vin: str, portes: list[str]) -> None:
    """Une porte vient de s'ouvrir. Forcée si la voiture est toujours
    verrouillée une fois le délai écoulé."""
    if not await _armee(vin):
        return
    tache = asyncio.create_task(_verifier_porte(vin, portes))
    _verifications.add(tache)
    tache.add_done_callback(_verifications.discard)


async def _verifier_porte(vin: str, portes: list[str]) -> None:
    instant = _maintenant()
    await asyncio.sleep(DELAI_VERIFICATION)
    motif = await _innocente(vin, instant, sauf="porte")
    if motif:
        log.info("ouverture expliquée par : %s — pas d'alerte", motif)
        return
    await intrusion(vin, f"ouverture forcée — {', '.join(portes)}")


# Une riposte au plus par période. Si un détecteur se trompe encore, le
# propriétaire subit un klaxon, pas cinq.
COOLDOWN_RIPOSTE = 10 * 60.0
_derniere_riposte: dict[str, float] = {}


async def intrusion(vin: str, motif: str, *, exiger_verrou: bool = True) -> None:
    """Intrusion confirmée : journal, notification, puis ripostes selon les réglages.

    Dernier garde-fou avant toute riposte : si le véhicule n'est pas verrouillé
    à cet instant, c'est le propriétaire. On journalise pour comprendre, mais
    on ne klaxonne pas et on n'allume pas les caméras.
    """
    if exiger_verrou and not await _verrouillee(vin):
        log.info("intrusion écartée (%s) : véhicule déverrouillé", motif)
        return

    # Persistée avant tout envoi : si Telegram est injoignable, la trace reste.
    async with SessionLocal() as session:
        session.add(VehicleAlert(vin=vin, name="intrusion", payload=motif))
        await session.commit()

    await send(
        f"🚨 INTRUSION — {motif}.\n"
        "La voiture s'est réveillée sans ouverture légitime."
    )

    depuis = _maintenant() - _derniere_riposte.get(vin, 0.0)
    if depuis < COOLDOWN_RIPOSTE:
        log.info("riposte retenue : la précédente date de %.0f s", depuis)
        return
    _derniere_riposte[vin] = _maintenant()

    if prefs.get("security_auto_sentry"):
        await _commande(vin, "set_sentry_mode", {"on": True},
                        "📹 Sentinelle activée : les caméras enregistrent.")

    if prefs.get("security_honk"):
        # Dernière chance d'éviter un klaxon injustifié : le déverrouillage a pu
        # arriver pendant le délai de vérification.
        if exiger_verrou and not await _verrouillee(vin):
            log.info("klaxon annulé : véhicule déverrouillé entre-temps")
            return
        await _commande(vin, "honk_horn", {}, "📢 Klaxon déclenché.")


async def _commande(vin: str, nom: str, corps: dict, confirmation: str) -> None:
    """Envoie une commande de riposte. Un échec est signalé, jamais fatal."""
    try:
        async with SessionLocal() as session:
            token = await oauth.valid_access_token(session, "default")
        await FleetClient(token).command(vin, nom, corps)
        await send(confirmation)
    except Exception as exc:  # noqa: BLE001
        log.warning("riposte %s en échec : %s", nom, exc)
        await send(f"⚠️ Riposte {nom} refusée : {str(exc)[:120]}")
