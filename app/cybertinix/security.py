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

from . import oauth, prefs
from .db import SessionLocal
from .fleet import FleetClient
from .models import Signal, VehicleAlert
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
    """L'écran central vient de s'allumer en mode verrouillé.

    On ne conclut pas tout de suite : une ouverture de porte légitime arrive
    parfois quelques centaines de millisecondes *après* le réveil de l'écran.
    La vérification est différée, et seule une absence de cause innocente
    déclenche l'alerte.
    """
    instant = _maintenant()
    tache = asyncio.create_task(_verifier(vin, instant))
    _verifications.add(tache)
    tache.add_done_callback(_verifications.discard)


async def _innocente(vin: str, instant: float) -> str | None:
    """Motif innocent expliquant un réveil, ou None."""
    if _evenement_autour(vin, "deverrouillage", instant, FENETRE_DEVERROUILLAGE):
        return "déverrouillage par la clé"
    if _evenement_autour(vin, "porte", instant, FENETRE_PORTE):
        return "ouverture de porte"
    if _evenement_autour(vin, "trappe", instant, FENETRE_TRAPPE):
        return "trappe de charge"
    if not await _verrouillee(vin):
        return "véhicule déverrouillé"
    return None


async def _verrouillee(vin: str) -> bool:
    """Dernier état de verrouillage connu. En cas de doute, on considère le
    véhicule déverrouillé : une alerte manquée vaut mieux qu'un klaxon sur le
    propriétaire."""
    async with SessionLocal() as session:
        brut = await session.scalar(
            select(Signal.value)
            .where(Signal.vin == vin, Signal.name == "Locked")
            .order_by(Signal.id.desc())
            .limit(1)
        )
    return brut is not None and brut.strip().lower() == "true"


async def _verifier(vin: str, instant: float) -> None:
    await asyncio.sleep(DELAI_VERIFICATION)
    motif = await _innocente(vin, instant)
    if motif:
        log.info("réveil d'écran expliqué par : %s — pas d'alerte", motif)
        return
    await intrusion(vin, "écran allumé dans un habitacle verrouillé")


async def siege_occupe(vin: str) -> None:
    """Le siège conducteur vient d'être occupé.

    C'est le signal d'intrusion le plus direct : il n'y a pas d'explication
    innocente à quelqu'un d'assis dans une voiture verrouillée. On garde le
    délai de vérification uniquement pour laisser arriver un déverrouillage
    par la clé qui serait dans le même lot de messages.
    """
    instant = _maintenant()
    tache = asyncio.create_task(_verifier_siege(vin, instant))
    _verifications.add(tache)
    tache.add_done_callback(_verifications.discard)


async def _verifier_siege(vin: str, instant: float) -> None:
    await asyncio.sleep(DELAI_VERIFICATION)
    if _evenement_autour(vin, "deverrouillage", instant, FENETRE_DEVERROUILLAGE):
        return
    if not await _verrouillee(vin):
        return
    await intrusion(vin, "siège conducteur occupé dans un habitacle verrouillé")


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
    tache = asyncio.create_task(_verifier_porte(vin, portes))
    _verifications.add(tache)
    tache.add_done_callback(_verifications.discard)


async def _verifier_porte(vin: str, portes: list[str]) -> None:
    instant = _maintenant()
    await asyncio.sleep(DELAI_VERIFICATION)
    if _evenement_autour(vin, "deverrouillage", instant, FENETRE_DEVERROUILLAGE):
        return  # la clé a ouvert : rien à signaler
    if not await _verrouillee(vin):
        return  # déverrouillée entre-temps : ouverture légitime
    await intrusion(vin, f"ouverture forcée — {', '.join(portes)}")


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
