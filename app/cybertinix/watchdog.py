"""Chien de garde : détecte ce qui casse sans faire de bruit.

Toutes les pannes rencontrées jusqu'ici étaient silencieuses — un conteneur qui
redémarrait en boucle, un certificat illisible. La plus probable à venir a la
même forme : si la télémétrie décroche, on cesse simplement de recevoir des
notifications, et l'absence de notification ressemble à une voiture à l'arrêt.

Chaque contrôle est verrouillé sur son état : on alerte à la bascule, une seule
fois, et on annonce le rétablissement. Sans ça, un problème persistant enverrait
un message tous les quarts d'heure et on finirait par couper le bot.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


from . import prefs, status
from .db import SessionLocal
from .models import AlertState
from .notify import send

log = logging.getLogger(__name__)

SYSTEM = "_system"


async def _latch(session: AsyncSession, key: str, state: str) -> bool:
    """Vrai uniquement quand l'état change depuis le dernier passage."""
    row = await session.scalar(
        select(AlertState).where(AlertState.vin == SYSTEM, AlertState.key == key)
    )
    if row is None:
        session.add(AlertState(vin=SYSTEM, key=key, state=state))
        await session.commit()
        # Premier passage : on n'alerte que si l'état est déjà mauvais.
        return state != "ok"
    if row.state == state:
        return False
    row.state = state
    await session.commit()
    return True


async def _sustained(session: AsyncSession, key: str, actif: bool, heures: float) -> bool:
    """Vrai quand une condition dure depuis plus de `heures`.

    Certains états sont normalement transitoires. Une configuration non encore
    adoptée pendant que la voiture dort n'est pas une panne — elle le devient
    si elle n'est toujours pas prise le lendemain. On mémorise donc le moment
    où la condition est apparue plutôt que de réagir au premier constat.
    """
    row = await session.scalar(
        select(AlertState).where(AlertState.vin == SYSTEM, AlertState.key == f"depuis:{key}")
    )

    if not actif:
        if row is not None:
            await session.delete(row)
            await session.commit()
        return False

    maintenant = datetime.now(UTC)
    if row is None:
        session.add(AlertState(vin=SYSTEM, key=f"depuis:{key}", state=maintenant.isoformat()))
        await session.commit()
        return False

    try:
        depuis = datetime.fromisoformat(row.state)
    except ValueError:
        return False
    return (maintenant - depuis).total_seconds() > heures * 3600


async def _controles(session: AsyncSession) -> list[str]:
    alertes: list[str] = []
    etat = await status.collect(session)

    # --- Flux de données ---
    flux = etat["flux"]
    age = flux.get("age_heures")
    seuil = prefs.get("watchdog_no_data_hours")
    if age is not None:
        muet = age > seuil
        if await _latch(session, "flux", "muet" if muet else "ok"):
            alertes.append(
                f"🔇 Aucune donnée depuis {flux['age']}. Si la voiture a roulé "
                "entre-temps, la télémétrie a décroché."
                if muet
                else "🔊 Les données sont de nouveau reçues."
            )

    # --- Véhicule ---
    vehicule = etat.get("vehicule", {})
    if vehicule and not vehicule.get("erreur"):
        if vehicule.get("cle_appairee") is False:
            if await _latch(session, "cle", "absente"):
                alertes.append(
                    "🔑 La clé virtuelle n'est plus appairée. Ni commandes ni "
                    "télémétrie tant qu'elle n'est pas rajoutée depuis l'app mobile."
                )
        elif vehicule.get("cle_appairee"):
            if await _latch(session, "cle", "ok"):
                alertes.append("🔑 Clé virtuelle de nouveau appairée.")

        telemetrie = vehicule.get("telemetrie") or {}
        synced = telemetrie.get("synced")
        if synced is not None:
            # Une configuration fraîchement poussée reste non adoptée tant que
            # la voiture dort. On laisse une journée avant de s'en inquiéter.
            grace = prefs.get("sync_grace_hours")
            desync_durable = await _sustained(
                session, "synced", actif=not synced, heures=grace
            )
            if await _latch(session, "synced", "desync" if desync_durable else "ok"):
                alertes.append(
                    "📡 Télémétrie de nouveau synchronisée."
                    if not desync_durable
                    else "📡 Configuration de télémétrie non adoptée depuis plus de "
                    f"{grace} h. Elle est supprimée automatiquement "
                    "si l'accès est révoqué ou si la limite de facturation a été dépassée."
                )

        # Seules les erreurs récentes comptent : Tesla conserve un historique,
        # et une panne déjà corrigée y reste visible longtemps.
        #
        # Et parmi les récentes, seules celles qui demandent quelque chose.
        # La plus fréquente — le véhicule sans réseau au réveil — se résout
        # d'elle-même en quelques secondes : alerter dessus, c'était crier au
        # loup et apprendre à ignorer le chien de garde. On alerte donc sur
        # les erreurs graves, ou sur un volume anormal d'erreurs bénignes.
        detail = vehicule.get("erreurs_detail") or []
        graves = [e for e in detail if e.get("gravite") == "action"]
        benignes = [e for e in detail if e.get("gravite") != "action"]
        trop = len(benignes) >= status.SEUIL_ERREURS_BENIGNES

        etat_err = "graves" if graves else ("volume" if trop else "ok")
        if await _latch(session, "erreurs_telemetrie", etat_err):
            if graves:
                e = graves[0]
                message = f"⚠️ Télémétrie — {e['libelle']}. {e['conseil']}"
                if len(graves) > 1:
                    message += f" ({len(graves)} au total sur 24 h)"
                # Une signature non répertoriée ne dit rien par son libellé :
                # on joint le texte brut, seul moyen de la classer sans aller
                # fouiller le serveur.
                if "non répertoriée" in e["libelle"]:
                    message += f"\n\n{e['brut'][:200]}"
                alertes.append(message)
            elif trop:
                alertes.append(
                    f"⚠️ Télémétrie — {len(benignes)} erreurs de connexion en 24 h, "
                    "au-delà de l'ordinaire. Chacune se résout seule, mais le volume "
                    "est inhabituel : vérifier make check et la couverture réseau."
                )

    # --- Certificat ---
    cert = etat["certificat"]
    jours = cert.get("jours_restants")
    if jours is not None:
        proche = jours <= prefs.get("cert_expiry_warning_days")
        if await _latch(session, "certificat", "proche" if proche else "ok"):
            if proche:
                alertes.append(
                    f"📜 Certificat TLS expirant dans {jours} jours. Le "
                    "renouvellement est automatique — vérifier certbot s'il approche de zéro."
                )
    elif cert.get("erreur"):
        if await _latch(session, "certificat", "illisible"):
            alertes.append(f"📜 Certificat illisible : {cert['erreur']}")

    # --- Jeton ---
    jeton = etat["jeton"]
    if jeton.get("present"):
        reste = jeton["refresh_jours_restants_estimes"]
        proche = reste <= prefs.get("refresh_token_warning_days")
        if await _latch(session, "jeton", "proche" if proche else "ok"):
            if proche:
                alertes.append(
                    f"🔐 Le jeton de rafraîchissement expire dans ~{reste} jours. "
                    "Repasser par /auth/login avant, sinon l'accès est perdu."
                )
    else:
        if await _latch(session, "jeton", "absent"):
            alertes.append("🔐 Aucune autorisation Tesla enregistrée.")

    return alertes


async def run() -> None:
    # Laisser l'application finir de démarrer avant le premier passage.
    await asyncio.sleep(60)

    while True:
        # Réglages relus à chaque tour : activer, désactiver ou changer la
        # fréquence depuis l'interface prend effet sans redémarrer.
        if prefs.get("watchdog_enabled"):
            try:
                async with SessionLocal() as session:
                    for alerte in await _controles(session):
                        await send(alerte)
            except Exception:  # noqa: BLE001 — le chien de garde ne doit jamais mourir
                log.exception("passage du chien de garde en échec")

        await asyncio.sleep(prefs.get("watchdog_interval_minutes") * 60)


def _prochain_resume() -> datetime:
    """Prochaine occurrence de l'heure configurée, en heure locale.

    Le décalage se recalcule à partir de l'heure locale à chaque tour plutôt
    que par un sommeil de 24 h : un sommeil fixe dériverait aux changements
    d'heure d'été.
    """
    maintenant = status.local_now()
    cible = maintenant.replace(
        hour=prefs.get("digest_hour"), minute=0, second=0, microsecond=0
    )
    if cible <= maintenant:
        cible += timedelta(days=1)
    return cible


async def digest() -> None:
    """Résumé quotidien, à l'heure locale configurée."""
    while True:
        attente = (_prochain_resume() - status.local_now()).total_seconds()
        await asyncio.sleep(max(attente, 60))

        # Testé au réveil, pas au démarrage : désactiver puis réactiver depuis
        # l'interface fonctionne sans redémarrer le conteneur.
        if not prefs.get("digest_enabled"):
            continue

        try:
            async with SessionLocal() as session:
                etat = await status.collect(session)
                await send(status.render(etat))
        except Exception:  # noqa: BLE001
            log.exception("résumé quotidien en échec")
