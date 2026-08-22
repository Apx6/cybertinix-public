import logging
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings
from .models import Base

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """Crée le schéma s'il manque, puis applique les réparations connues.

    Suffisant tant que le modèle bouge peu. Passer à Alembic dès qu'il faudra
    migrer des données déjà collectées.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _dedoublonner_alertes(conn)


async def _dedoublonner_alertes(conn) -> None:
    """Supprime les copies d'une même alerte véhicule.

    Avant que l'ingestion ne filtre, chaque reconnexion du véhicule rejouait
    son historique et en insérait une copie. Une alerte est identifiée par
    son nom et son heure de début ; on garde la première occurrence.
    Idempotent : sans doublon, ne touche à rien.
    """
    from sqlalchemy import text

    resultat = await conn.execute(text("""
        DELETE FROM vehicle_alerts a
        USING vehicle_alerts b
        WHERE a.id > b.id
          AND a.vin = b.vin
          AND a.name = b.name
          AND a.name <> 'intrusion'
          AND substring(a.payload from '"StartedAt":"([^"]+)"')
            = substring(b.payload from '"StartedAt":"([^"]+)"')
    """))
    if resultat.rowcount:
        logging.getLogger(__name__).info(
            "alertes véhicule dédoublonnées : %d copies supprimées", resultat.rowcount
        )

    # Fausses intrusions issues d'avertissements de pneus, classés à tort en
    # alarme quand le filtre retenait tout le calculateur VCSEC.
    faux = await conn.execute(text("""
        DELETE FROM vehicle_alerts
        WHERE name = 'intrusion' AND payload ~* 'tpms|tire|tyre|pressure'
    """))
    if faux.rowcount:
        logging.getLogger(__name__).info(
            "fausses intrusions (pneus) retirées de l'historique : %d", faux.rowcount
        )


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
