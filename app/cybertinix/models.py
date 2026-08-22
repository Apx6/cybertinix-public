from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class OAuthToken(Base):
    """Jeton Tesla d'un utilisateur.

    Le refresh token est à usage unique et expire à 3 mois : il doit être
    remplacé en base à chaque échange, sinon l'accès est perdu sans moyen de
    le récupérer autrement qu'en refaisant tout le flow d'autorisation.
    """

    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), unique=True)
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Signal(Base):
    """Un signal de télémétrie, tel que poussé par le véhicule.

    On persiste tout, y compris ce que les notifications n'utilisent pas :
    c'est ce qui permettra de brancher un dashboard plus tard sans avoir à
    réingérer un historique qui n'existerait pas.

    La valeur est stockée en texte brut : le type d'un champ peut changer d'une
    version de firmware à l'autre (12.3 numérique ici, "12.3" chaîne ailleurs).
    La normalisation se fait à la lecture.
    """

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vin: Mapped[str] = mapped_column(String(17))
    name: Mapped[str] = mapped_column(String(128))
    value: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("ix_signals_vin_name_time", "vin", "name", "received_at"),)


class VehicleAlert(Base):
    __tablename__ = "vehicle_alerts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vin: Mapped[str] = mapped_column(String(17))
    name: Mapped[str] = mapped_column(String(128))
    payload: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("ix_alerts_vin_time", "vin", "received_at"),)


class AlertState(Base):
    """Mémoire de ce qui a déjà été annoncé.

    Sans elle, une batterie sous le seuil déclencherait une notification à
    chaque signal reçu. On mémorise le dernier état communiqué pour une clé
    donnée et on n'émet que sur transition.

    Sert aussi à porter un état entre deux signaux — l'odomètre au départ d'un
    trajet, par exemple, pour calculer la distance à l'arrivée.
    """

    __tablename__ = "alert_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    vin: Mapped[str] = mapped_column(String(17))
    key: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("vin", "key", name="uq_alert_state_vin_key"),)


class Preference(Base):
    """Réglage modifié depuis l'interface.

    Le `.env` fournit les valeurs par défaut ; cette table ne contient que les
    écarts. Un réglage jamais touché n'y figure pas, ce qui permet de changer
    un défaut dans le code sans écraser un choix délibéré.
    """

    __tablename__ = "preferences"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ConnectivityEvent(Base):
    """Connexion/déconnexion du véhicule.

    Utile au-delà du suivi : savoir que la voiture est déjà en ligne évite un
    wake_up, qui est de loin l'appel le plus cher de l'API.
    """

    __tablename__ = "connectivity_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vin: Mapped[str] = mapped_column(String(17))
    status: Mapped[str] = mapped_column(String(32))
    connection_id: Mapped[str] = mapped_column(String(128), default="")
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
