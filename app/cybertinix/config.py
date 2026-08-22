from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    domain: str = ""

    # Posé par le Dockerfile à la construction. « dev » hors déploiement.
    build_id: str = "dev"

    # Jeton partagé exigé sur toutes les routes sauf /healthz et le callback
    # OAuth. Vide, l'application refuse de servir : le domaine figure en clair
    # dans les journaux de transparence des certificats, il ne protège rien.
    api_token: str = ""

    tesla_client_id: str = ""
    tesla_client_secret: str = ""
    tesla_audience: str = "https://fleet-api.prd.eu.vn.cloud.tesla.com"
    tesla_scopes: str = (
        "openid offline_access user_data vehicle_device_data "
        "vehicle_location vehicle_cmds vehicle_charging_cmds"
    )

    # Le flow d'autorisation passe par auth.tesla.com, mais l'échange de code
    # et le refresh doivent viser fleet-auth : ce sont deux domaines distincts,
    # avec des limites de débit différentes. Les confondre casse le refresh.
    tesla_authorize_url: str = "https://auth.tesla.com/oauth2/v3/authorize"
    tesla_token_url: str = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"

    database_url: str = "postgresql+asyncpg://teslaaddict:teslaaddict@postgres:5432/teslaaddict"

    mqtt_broker_host: str = "mosquitto"
    mqtt_broker_port: int = 1883
    mqtt_topic_base: str = "telemetry"

    proxy_base_url: str = "https://tesla-http-proxy:4443"
    proxy_ca_bundle: str = "/certs/proxy-cert.pem"

    # Serveur de télémétrie tel que le véhicule le joindra : port 443 public,
    # nginx aiguillant ensuite sur fleet-telemetry d'après le nom SNI.
    telemetry_port: int = 443
    telemetry_ca_path: str = "/etc/letsencrypt/live/teslaaddict/chain.pem"

    # Vide = `telemetry.<domain>`. À ne renseigner que pour héberger la
    # télémétrie sur un autre nom — auquel cas Tesla impose qu'il partage le
    # domaine racine de l'application, ce que build_config vérifie.
    telemetry_hostname_override: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Familles de notifications ---
    # Les deux premières racontent les gestes ordinaires du propriétaire —
    # ouvrir sa porte, partir — et deviennent vite du bruit. Désactivées par
    # défaut ; la sécurité couvre les cas qui comptent.
    notif_acces: bool = False
    notif_trajets: bool = False
    notif_charge: bool = True
    notif_seuils: bool = True

    # --- Seuils d'alerte ---
    battery_low_percent: float = 20.0

    # Sert uniquement à libeller les notifications, aucune conversion n'est
    # faite. Tesla a historiquement renvoyé des miles sur ses API ; comparer la
    # distance du premier trajet annoncé avec l'écran du véhicule, et basculer
    # sur "mi" si l'écart est d'un facteur 1,6.
    distance_unit: str = "km"

    # --- Ripostes sur intrusion, désactivées par défaut : elles agissent sur
    # le véhicule. La sentinelle à la demande allume les caméras uniquement
    # quand une intrusion est détectée, donc sans coût le reste du temps.
    security_auto_sentry: bool = False
    security_honk: bool = False

    # Nom de rue via Nominatim. Seul appel sortant vers un tiers autre que
    # Tesla ou Telegram : il transmet la position du véhicule.
    reverse_geocode: bool = True

    # --- Supervision ---
    timezone: str = "Europe/Paris"

    watchdog_enabled: bool = True
    watchdog_interval_minutes: int = 15

    # Une Tesla en sommeil n'émet rien : l'absence de données n'est pas une
    # panne en soi. Le seuil est donc long, et le message formulé comme une
    # vérification à faire, pas comme une alarme.
    watchdog_no_data_hours: int = 48

    # Délai avant de s'inquiéter d'une configuration non adoptée : la voiture
    # ne peut rien adopter tant qu'elle dort.
    sync_grace_hours: float = 24.0

    cert_expiry_warning_days: int = 15
    # Le jeton de rafraîchissement expire à 3 mois. Prévenir avant, sinon il
    # faut refaire toute l'autorisation.
    refresh_token_max_age_days: int = 90
    refresh_token_warning_days: int = 14

    # Écoute des messages entrants pour répondre à /etat.
    telegram_commands_enabled: bool = True

    digest_enabled: bool = True
    digest_hour: int = 8

    telemetry_cert_path: str = "/etc/letsencrypt/live/teslaaddict/fullchain.pem"

    # En bars, confirmé sur le véhicule — Tesla ne documente pas l'unité des
    # champs TPMS. Une Model X tourne autour de 2.9-3.1 ; 2.5 alerte avant que
    # la voiture ne le fasse d'elle-même, ce qui laisse le temps de regonfler.
    tpms_min_pressure: float = 2.5

    @property
    def redirect_uri(self) -> str:
        return f"https://{self.domain}/auth/callback"

    @property
    def pairing_url(self) -> str:
        """Deep link d'appairage de la clé virtuelle, à ouvrir sur le téléphone."""
        return f"https://tesla.com/_ak/{self.domain}"

    @property
    def telemetry_hostname(self) -> str:
        """Nom que le véhicule contactera pour pousser ses données.

        Doit commencer par `telemetry.` pour que le routage SNI de nginx
        l'aiguille vers fleet-telemetry, et partager le domaine racine de
        l'application enregistrée chez Tesla.
        """
        return self.telemetry_hostname_override or f"telemetry.{self.domain}"


settings = Settings()
