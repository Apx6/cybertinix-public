import httpx

from .config import settings


class FleetClient:
    """Client Fleet API.

    Deux bases distinctes, et le choix n'est pas cosmétique :
      - lecture      -> Fleet API directement
      - commandes    -> proxy local, qui signe avec la clé privée

    Une commande non signée est rejetée par le véhicule, sauf sur les Model S/X
    pré-2021 et la plupart des véhicules business. `fleet_status` indique quel
    régime s'applique via `vehicle_command_protocol_required`.
    """

    def __init__(self, access_token: str) -> None:
        self._headers = {"Authorization": f"Bearer {access_token}"}

    async def _get(self, path: str, **params) -> dict:
        async with httpx.AsyncClient(base_url=settings.tesla_audience, timeout=30) as client:
            response = await client.get(path, headers=self._headers, params=params or None)
            response.raise_for_status()
            return response.json()

    async def _post(self, path: str, json: dict | None = None) -> dict:
        async with httpx.AsyncClient(base_url=settings.tesla_audience, timeout=30) as client:
            response = await client.post(path, headers=self._headers, json=json)
            response.raise_for_status()
            return response.json()

    # --- Lecture -------------------------------------------------------------

    async def list_vehicles(self) -> dict:
        return await self._get("/api/1/vehicles")

    async def fleet_status(self, vins: list[str]) -> dict:
        """État du véhicule vis-à-vis de l'application.

        À appeler en premier après l'autorisation : c'est ce qui répond aux deux
        questions bloquantes — le véhicule exige-t-il les commandes signées, et
        est-il éligible à Fleet Telemetry (firmware, matériel, clé appairée).
        """
        return await self._post("/api/1/vehicles/fleet_status", json={"vins": vins})

    async def vehicle_data(self, vin: str, endpoints: str | None = None) -> dict:
        """Appel live au véhicule. Facturé cher et susceptible de le réveiller.

        À réserver au ponctuel : la collecte continue passe par Fleet Telemetry.
        Depuis le firmware 2023.38, la position exige `location_data` explicite,
        ce qui affiche une icône de partage sur l'écran du véhicule.
        """
        return await self._get(f"/api/1/vehicles/{vin}/vehicle_data", endpoints=endpoints)

    # --- Commandes (via le proxy signant) ------------------------------------

    async def command(self, vin: str, name: str, payload: dict | None = None) -> dict:
        async with httpx.AsyncClient(
            base_url=settings.proxy_base_url,
            verify=settings.proxy_ca_bundle,
            timeout=30,
        ) as client:
            response = await client.post(
                f"/api/1/vehicles/{vin}/command/{name}",
                headers=self._headers,
                json=payload or {},
            )
            response.raise_for_status()
            return response.json()

    # --- Fleet Telemetry -----------------------------------------------------

    async def configure_telemetry(self, vins: list[str], config: dict) -> dict:
        """Pousse une configuration de télémétrie vers les véhicules.

        Passe par le proxy, qui signe la configuration avec la clé privée avant
        de la transmettre. C'est l'approche recommandée par Tesla, y compris
        pour les véhicules qui n'exigent pas les commandes signées : la
        signature de configuration est un mécanisme distinct de celle des
        commandes, et le véhicule refuse toute configuration non signée.
        """
        async with httpx.AsyncClient(
            base_url=settings.proxy_base_url,
            verify=settings.proxy_ca_bundle,
            timeout=60,
        ) as client:
            response = await client.post(
                "/api/1/vehicles/fleet_telemetry_config",
                headers=self._headers,
                json={"vins": vins, "config": config},
            )
            response.raise_for_status()
            return response.json()

    async def get_telemetry_config(self, vin: str) -> dict:
        """Configuration active. `synced=false` signifie que le véhicule ne l'a
        pas encore adoptée — il le fera à sa prochaine connexion."""
        return await self._get(f"/api/1/vehicles/{vin}/fleet_telemetry_config")

    async def delete_telemetry_config(self, vin: str) -> dict:
        async with httpx.AsyncClient(base_url=settings.tesla_audience, timeout=30) as client:
            response = await client.delete(
                f"/api/1/vehicles/{vin}/fleet_telemetry_config", headers=self._headers
            )
            response.raise_for_status()
            return response.json()

    async def telemetry_errors(self, vin: str) -> dict:
        """Erreurs remontées par le véhicule après réception de la config.

        Le premier endroit où regarder quand une configuration est acceptée
        mais qu'aucune donnée n'arrive.
        """
        return await self._get(f"/api/1/vehicles/{vin}/fleet_telemetry_errors")

    # --- Planification de charge ---------------------------------------------

    async def add_charge_schedule(self, vin: str, schedule: dict) -> dict:
        """Programme une charge dans le véhicule. Commande signée, donc via le proxy."""
        return await self.command(vin, "add_charge_schedule", schedule)

    async def remove_charge_schedule(self, vin: str, schedule_id: int) -> dict:
        return await self.command(vin, "remove_charge_schedule", {"id": schedule_id})

    async def charge_schedules(self, vin: str) -> dict:
        """Planifications existantes.

        Seule voie disponible : `vehicle_data`, qui est facturé et peut
        réveiller le véhicule. À n'appeler qu'à la demande, jamais en boucle.
        """
        return await self.vehicle_data(vin, endpoints="charge_schedule_data")

    # --- Préconditionnement --------------------------------------------------

    async def add_precondition_schedule(self, vin: str, schedule: dict) -> dict:
        return await self.command(vin, "add_precondition_schedule", schedule)

    async def remove_precondition_schedule(self, vin: str, schedule_id: int) -> dict:
        return await self.command(vin, "remove_precondition_schedule", {"id": schedule_id})

    async def precondition_schedules(self, vin: str) -> dict:
        """Comme pour la charge : seul `vehicle_data` les expose, et il est facturé."""
        return await self.vehicle_data(vin, endpoints="preconditioning_schedule_data")

    # --- Mises à jour logicielles ---------------------------------------------

    async def release_notes(self, vin: str, staged: bool = True) -> dict:
        """Notes de version. `staged=True` donne celles de la mise à jour en
        attente, `False` celles du firmware installé. Non facturé."""
        return await self._get(
            f"/api/1/vehicles/{vin}/release_notes",
            language="fr", staged=str(staged).lower(),
        )

    # --- Divers --------------------------------------------------------------

    async def wake_up(self, vin: str) -> dict:
        """Réveille le véhicule.

        L'appel le plus cher de l'API et limité à 3/min. Vérifier la
        connectivité (via la télémétrie) avant d'y recourir.
        """
        return await self._post(f"/api/1/vehicles/{vin}/wake_up")
