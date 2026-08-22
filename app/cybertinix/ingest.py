import asyncio
import json
import logging

import aiomqtt
from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import ConnectivityEvent, Signal, VehicleAlert
from .rules import evaluate

log = logging.getLogger(__name__)

# Structure des topics publiés par fleet-telemetry :
#   <base>/<VIN>/v/<champ>
#   <base>/<VIN>/alerts/<nom>/current | /history
#   <base>/<VIN>/errors/<nom>
#   <base>/<VIN>/connectivity
TOPIC_FILTER = f"{settings.mqtt_topic_base}/#"


async def run() -> None:
    """Boucle d'ingestion, relancée indéfiniment en cas de coupure du broker."""
    while True:
        try:
            await _consume()
        except aiomqtt.MqttError as exc:
            log.warning("MQTT déconnecté (%s), nouvelle tentative dans 5 s", exc)
            await asyncio.sleep(5)


async def _consume() -> None:
    async with aiomqtt.Client(
        hostname=settings.mqtt_broker_host,
        port=settings.mqtt_broker_port,
        identifier="teslaaddict-ingest",
    ) as client:
        await client.subscribe(TOPIC_FILTER, qos=1)
        log.info("abonné à %s", TOPIC_FILTER)
        async for message in client.messages:
            try:
                await _handle(str(message.topic), message.payload)
            except Exception:  # noqa: BLE001 - un message malformé ne doit pas tuer la boucle
                log.exception("message ignoré sur %s", message.topic)


def _started_at(payload: str) -> str | None:
    try:
        return json.loads(payload).get("StartedAt")
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None


async def _alerte_connue(session, vin: str, name: str, debut: str) -> bool:
    """Vrai si une alerte de même nom et même heure de début est déjà en base."""
    existante = await session.scalar(
        select(VehicleAlert.id)
        .where(
            VehicleAlert.vin == vin,
            VehicleAlert.name == name,
            VehicleAlert.payload.contains(f'"StartedAt":"{debut}"'),
        )
        .limit(1)
    )
    return existante is not None


async def _handle(topic: str, raw: bytes) -> None:
    parts = topic.split("/")
    if len(parts) < 3:
        return

    _, vin, kind, *rest = parts
    payload = raw.decode()

    async with SessionLocal() as session:
        if kind == "v" and rest:
            # Chaque champ arrive comme un message distinct, valeur encodée JSON.
            field = rest[0]

            # La valeur précédente se lit AVANT l'insertion : après, on relirait
            # celle qu'on vient d'écrire. Les règles en ont besoin pour ne
            # réagir qu'aux transitions.
            previous = await session.scalar(
                select(Signal.value)
                .where(Signal.vin == vin, Signal.name == field)
                .order_by(Signal.id.desc())
                .limit(1)
            )

            session.add(Signal(vin=vin, name=field, value=payload))
            await session.commit()
            await evaluate(session, vin, field, payload, previous)

        elif kind == "alerts" and rest and rest[-1] == "current":
            # Le véhicule renvoie son historique d'alertes à chaque reconnexion.
            # Une alerte est identifiée par son nom et son heure de début : si
            # on l'a déjà, on ne la réenregistre pas et on ne la réévalue pas —
            # sinon chaque réveil de la voiture rejouerait les mêmes alarmes.
            debut = _started_at(payload)
            if debut and await _alerte_connue(session, vin, rest[0], debut):
                return
            session.add(VehicleAlert(vin=vin, name=rest[0], payload=payload))
            await session.commit()
            await evaluate(session, vin, f"alert:{rest[0]}", payload, None)

        elif kind == "connectivity":
            data = json.loads(payload)
            session.add(
                ConnectivityEvent(
                    vin=vin,
                    status=data.get("Status", "unknown"),
                    connection_id=data.get("ConnectionId", ""),
                )
            )
            await session.commit()

        elif kind == "errors" and rest:
            # Erreurs remontées par le client télémétrie du véhicule : signe
            # d'une configuration que la voiture n'arrive pas à appliquer.
            log.error("erreur télémétrie %s sur %s : %s", rest[0], vin, payload)
