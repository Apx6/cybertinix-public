import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import (
    actions,
    alerts,
    auth,
    charging,
    commands,
    ingest,
    oauth,
    prefs,
    status as status_module,
    telemetry,
    watchdog,
)
from .config import settings
from .db import SessionLocal, get_session, init_db
from .fleet import FleetClient
from .models import AlertState, VehicleAlert
from .notify import send

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

# États OAuth en attente. En mémoire : suffisant pour un usage mono-utilisateur,
# à déplacer en base le jour où plusieurs comptes se connectent.
_pending_states: set[str] = set()

SUBJECT = "default"


async def _annoncer_version(session: AsyncSession) -> None:
    """Prévient sur Telegram quand l'application redémarre sur une nouvelle
    version. Un simple redémarrage sur la même version reste silencieux."""
    row = await session.scalar(
        select(AlertState).where(AlertState.vin == "_system", AlertState.key == "build_id")
    )
    if row is not None and row.state == settings.build_id:
        return
    premiere = row is None
    if row is None:
        session.add(AlertState(vin="_system", key="build_id", state=settings.build_id))
    else:
        row.state = settings.build_id
    await session.commit()
    if not premiere and settings.build_id != "dev":
        await send(f"🚀 CyberTinix mis à jour — version {settings.build_id}.\n"
                   "Ouvre l'application et touche « Mettre à jour » si elle le propose.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # Les réglages personnalisés remplacent les défauts du .env avant que la
    # moindre règle ou boucle de supervision ne les consulte.
    async with SessionLocal() as session:
        await prefs.load(session)
        await _annoncer_version(session)

    # Quatre boucles indépendantes. Chacune gère ses propres erreurs et se
    # relance : la mort de l'une ne doit pas emporter les autres.
    taches = [
        asyncio.create_task(ingest.run(), name="ingestion"),
        asyncio.create_task(watchdog.run(), name="chien-de-garde"),
        asyncio.create_task(watchdog.digest(), name="resume-quotidien"),
        asyncio.create_task(commands.run(), name="commandes-telegram"),
    ]
    log.info("démarré : %s", ", ".join(t.get_name() for t in taches))

    yield

    for tache in taches:
        tache.cancel()


app = FastAPI(title="CyberTinix", lifespan=lifespan)

# Middleware plutôt que dépendances par route : la protection couvre ainsi
# aussi /docs et /openapi.json, qui décrivent toute la surface de l'API.
app.middleware("http")(auth.middleware)


WEB = Path(__file__).parent / "web"


# Illustrations et autres fichiers statiques de l'interface. Montés après le
# middleware, donc protégés par le jeton comme la page elle-même.
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/", response_class=FileResponse, include_in_schema=False)
async def interface() -> FileResponse:
    """L'interface. Protégée comme le reste par le middleware de jeton.

    `no-cache` : un raccourci sur l'écran d'accueil iPhone rouvre sinon la
    page telle qu'elle était au dernier lancement, parfois des jours après un
    déploiement. Le navigateur revalide donc à chaque ouverture ; combiné au
    bandeau de mise à jour, on ne reste jamais sur une version périmée.
    """
    return FileResponse(
        WEB / "index.html",
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/version")
async def version() -> dict:
    """Identifiant de build. La page le compare au sien pour proposer une
    mise à jour ; un script peut s'en servir pour vérifier un déploiement."""
    return {"version": settings.build_id}


NOM_APP = "CyberTinix"
UTILISATEUR = "cybertinix"


def _page_login(erreur: str = "") -> HTMLResponse:
    html = (WEB / "login.html").read_text(encoding="utf-8")
    html = html.replace("{{NOM}}", NOM_APP).replace("{{UTILISATEUR}}", UTILISATEUR)
    html = html.replace("{{ERREUR}}", f'<div class="err">{erreur}</div>' if erreur else "")
    return HTMLResponse(html, status_code=401 if erreur else 200)


@app.get("/login", include_in_schema=False)
async def login_form(request: Request):
    if auth.session_valide(request.cookies.get(auth.COOKIE)):
        return RedirectResponse("/", status_code=303)
    return _page_login()


@app.post("/login", include_in_schema=False)
async def login_submit(password: str = Form(...)):
    """Vérifie le jeton et pose le cookie de session.

    Soumission classique (pas de fetch) : c'est la navigation qui suit un POST
    réussi qui déclenche la proposition d'enregistrement du mot de passe.
    """
    if not settings.api_token:
        return _page_login("API_TOKEN non configuré sur le serveur.")
    # Un jeton collé depuis un gestionnaire de mots de passe arrive souvent
    # avec un retour à la ligne ou une espace en fin : invisible, et fatal à
    # une comparaison stricte.
    password = password.strip()
    if not auth.jeton_valide(password):
        log.warning(
            "connexion refusée : %d caractères reçus, %d attendus",
            len(password), len(settings.api_token),
        )
        return _page_login(
            f"Jeton incorrect ({len(password)} caractères reçus, "
            f"{len(settings.api_token)} attendus)."
        )
    reponse = RedirectResponse("/", status_code=303)
    auth.poser_cookie(reponse)
    return reponse


@app.get("/logout", include_in_schema=False)
async def logout() -> RedirectResponse:
    reponse = RedirectResponse("/login", status_code=303)
    reponse.delete_cookie(auth.COOKIE, path="/")
    return reponse


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/status")
async def system_status(
    tesla: bool = Query(True, description="Interroger l'API Tesla (gratuit, ne réveille pas)"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """État consolidé : flux, véhicule, certificat, jeton, coût du mois.

    Les appels Tesla utilisés ici ne sont pas facturés. Passer `?tesla=false`
    pour un état purement local et instantané.
    """
    return await status_module.collect(session, avec_tesla=tesla)


@app.get("/status/texte", response_class=PlainTextResponse)
async def system_status_text(session: AsyncSession = Depends(get_session)) -> str:
    """Même chose, mise en forme — la version envoyée sur Telegram."""
    return status_module.render(await status_module.collect(session))


# --- Flow d'autorisation -----------------------------------------------------


@app.get("/auth/login")
async def login() -> RedirectResponse:
    state = secrets.token_urlsafe(24)
    _pending_states.add(state)
    return RedirectResponse(oauth.authorize_url(state))


@app.get("/auth/callback", response_class=HTMLResponse)
async def callback(
    code: str = Query(...),
    state: str = Query(...),
    session: AsyncSession = Depends(get_session),
) -> str:
    if state not in _pending_states:
        raise HTTPException(400, "state inconnu ou déjà consommé")
    _pending_states.discard(state)

    payload = await oauth.exchange_code(code)
    await oauth.store_tokens(session, SUBJECT, payload)

    # L'autorisation seule ne suffit pas : sans clé virtuelle appairée, ni
    # commandes ni télémétrie. On enchaîne donc directement sur l'appairage.
    return f"""
    <h1>Compte Tesla connecté</h1>
    <p>Dernière étape, à faire depuis ton iPhone, à proximité du véhicule :</p>
    <p><a href="{settings.pairing_url}">Ajouter la clé virtuelle au véhicule</a></p>
    <p><code>{settings.pairing_url}</code></p>
    """


# --- Diagnostic --------------------------------------------------------------


@app.get("/vehicles")
async def vehicles(session: AsyncSession = Depends(get_session)) -> dict:
    token = await oauth.valid_access_token(session, SUBJECT)
    return await FleetClient(token).list_vehicles()


@app.get("/vehicles/{vin}/status")
async def status(vin: str, session: AsyncSession = Depends(get_session)) -> dict:
    """État du véhicule vis-à-vis de l'application.

    Le premier appel à faire une fois la clé appairée : il dit si le véhicule
    exige les commandes signées et s'il peut streamer en télémétrie.
    """
    token = await oauth.valid_access_token(session, SUBJECT)
    return await FleetClient(token).fleet_status([vin])


# --- Fleet Telemetry ---------------------------------------------------------


class TelemetryConfigRequest(BaseModel):
    """Surcharges facultatives. Sans corps de requête, les défauts s'appliquent."""

    fields: dict[str, dict] | None = None
    alert_types: list[str] | None = None
    delivery_policy: str | None = None
    exp: int | None = None


@app.post("/vehicles/{vin}/telemetry")
async def configure_telemetry(
    vin: str,
    body: TelemetryConfigRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Configure le véhicule pour qu'il pousse ses données vers notre serveur.

    Prérequis, dans cet ordre : clé virtuelle appairée, firmware 2024.26+, et
    moins de cinq configurations déjà présentes. Un échec sur l'un des trois
    ressort dans `skipped`, pas en erreur HTTP.
    """
    body = body or TelemetryConfigRequest()
    try:
        config = telemetry.build_config(
            fields=body.fields,
            alert_types=body.alert_types,
            delivery_policy=body.delivery_policy,
            exp=body.exp,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(500, str(exc)) from exc

    token = await oauth.valid_access_token(session, SUBJECT)
    payload = await FleetClient(token).configure_telemetry([vin], config)

    result = telemetry.summarize_response(payload)
    if not result["ok"]:
        log.warning("configuration refusée pour %s : %s", vin, result["skipped"])
    return result


@app.get("/vehicles/{vin}/telemetry")
async def telemetry_config(vin: str, session: AsyncSession = Depends(get_session)) -> dict:
    """Configuration vue par Tesla. Guetter `synced: true`."""
    token = await oauth.valid_access_token(session, SUBJECT)
    return await FleetClient(token).get_telemetry_config(vin)


@app.delete("/vehicles/{vin}/telemetry")
async def remove_telemetry_config(vin: str, session: AsyncSession = Depends(get_session)) -> dict:
    token = await oauth.valid_access_token(session, SUBJECT)
    return await FleetClient(token).delete_telemetry_config(vin)


# --- Interface : état, réglages, commandes -----------------------------------


@app.get("/live")
async def live(session: AsyncSession = Depends(get_session)) -> dict:
    """Dernières valeurs connues du véhicule, lues en base.

    Gratuit et instantané. Ces valeurs datent du dernier changement remonté par
    la télémétrie, pas de l'instant présent — une voiture endormie garde donc
    l'état qu'elle avait en s'endormant.
    """
    return await status_module.live(session)


@app.get("/security/events")
async def security_events(
    limit: int = Query(30, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Historique de sécurité : nos intrusions, et les seules alertes du
    véhicule qui relèvent de la sécurité (calculateur VCSEC ou libellé
    explicite). Le journal de diagnostic complet est sur /vehicle/alerts.
    """
    out = []
    for r in await alerts.recentes(session, limite=400):
        if r.name == "intrusion":
            out.append({"quand": r.received_at.isoformat(), "type": "intrusion",
                        "nom": "intrusion", "detail": r.payload})
        elif alerts.est_securite(r.name):
            d = alerts.decoder(r)
            out.append({"quand": d["debut"] or d["recu"], "type": "alerte",
                        "nom": d["nom"], "detail": f"{d['systeme']} · {d['libelle']}"})
        if len(out) >= limit:
            break
    return out


@app.delete("/security/events")
async def clear_security_events(session: AsyncSession = Depends(get_session)) -> dict:
    """Vide l'historique des intrusions. Les alertes du véhicule restent dans
    le journal : ce sont des faits, pas nos conclusions."""
    resultat = await session.execute(delete(VehicleAlert).where(VehicleAlert.name == "intrusion"))
    await session.commit()
    return {"supprimees": resultat.rowcount}


@app.get("/vehicles/{vin}/release-notes")
async def release_notes(
    vin: str,
    staged: bool = Query(True, description="True : mise à jour en attente ; False : firmware installé"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Notes de version en français. Appel non facturé, ne réveille pas."""
    token = await oauth.valid_access_token(session, SUBJECT)
    return await FleetClient(token).release_notes(vin, staged=staged)


@app.get("/vehicle/alerts")
async def vehicle_alerts(
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Journal de diagnostic du véhicule, tel que remonté par la télémétrie.

    Autopilot, charge, capteurs : l'immense majorité n'a rien d'alarmant. Les
    dates sont celles des événements, pas de leur réception — le véhicule
    livre son historique en bloc à chaque reconnexion.
    """
    return [
        alerts.decoder(r)
        for r in await alerts.recentes(session, limite=limit)
        if r.name != "intrusion"
    ]


@app.get("/preferences")
async def get_preferences() -> list[dict]:
    return prefs.describe()


class PreferenceUpdate(BaseModel):
    valeur: Any


@app.put("/preferences/{cle}")
async def set_preference(
    cle: str, body: PreferenceUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    try:
        valeur = await prefs.set(session, cle, body.valeur)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"cle": cle, "valeur": valeur, "personnalise": True}


@app.delete("/preferences/{cle}")
async def reset_preference(cle: str, session: AsyncSession = Depends(get_session)) -> dict:
    """Revient au défaut du .env."""
    if cle not in {r.cle for r in prefs.REGLAGES}:
        raise HTTPException(404, f"réglage inconnu : {cle}")
    valeur = await prefs.reset(session, cle)
    return {"cle": cle, "valeur": valeur, "personnalise": False}


@app.get("/actions")
async def list_actions() -> list[dict]:
    return actions.describe()


class ActionRequest(BaseModel):
    parametre: Any | None = None


@app.post("/vehicles/{vin}/actions/{cle}")
async def run_action(
    vin: str,
    cle: str,
    body: ActionRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Exécute une commande de la liste blanche.

    Les commandes exigent un véhicule éveillé et sont signées par le proxy.
    Une erreur métier (voiture endormie, hors stationnement, câble branché)
    remonte telle quelle : elle est plus utile que sa reformulation.
    """
    action = actions.PAR_CLE.get(cle)
    if action is None:
        raise HTTPException(404, f"commande inconnue : {cle}")

    try:
        corps = actions.corps_pour(action, (body or ActionRequest()).parametre)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    token = await oauth.valid_access_token(session, SUBJECT)
    try:
        reponse = await FleetClient(token).command(vin, action.commande, corps)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            exc.response.status_code,
            f"{action.libelle} refusée : {exc.response.text[:300]}",
        ) from exc

    return {"action": cle, "libelle": action.libelle, "envoye": corps, "reponse": reponse}


# --- Planification de charge -------------------------------------------------


class ChargeScheduleRequest(BaseModel):
    """Fenêtre de charge. Les heures sont locales au véhicule, au format HH:MM."""

    debut: str | None = "22:00"
    fin: str | None = "06:00"
    jours: str = "tous"
    enabled: bool = True
    one_time: bool = False
    schedule_id: int | None = None
    # Sans coordonnées, on reprend la dernière position connue par la
    # télémétrie : si la voiture est garée au domicile, c'est la bonne.
    lat: float | None = None
    lon: float | None = None


@app.post("/vehicles/{vin}/charge-schedule")
async def create_charge_schedule(
    vin: str,
    body: ChargeScheduleRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Programme une charge en heures creuses dans le véhicule.

    La planification vit dans la voiture : une seule commande, pas de coût
    récurrent, et elle continue de s'appliquer même si ce serveur s'arrête.
    Elle est liée à un lieu et ne se déclenche donc qu'à proximité.
    """
    body = body or ChargeScheduleRequest()

    lat, lon = await _position_ou_400(session, vin, body.lat, body.lon)

    try:
        schedule = charging.build_schedule(
            lat=lat,
            lon=lon,
            debut=body.debut,
            fin=body.fin,
            jours=body.jours,
            enabled=body.enabled,
            one_time=body.one_time,
            schedule_id=body.schedule_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    token = await oauth.valid_access_token(session, SUBJECT)
    reponse = await FleetClient(token).add_charge_schedule(vin, schedule)

    return {
        "envoye": schedule,
        "resume": charging.decrire(schedule),
        "reponse": reponse,
    }


@app.get("/vehicles/{vin}/charge-schedule")
async def list_charge_schedules(vin: str, session: AsyncSession = Depends(get_session)) -> dict:
    """Planifications enregistrées dans le véhicule.

    ⚠️ Passe par `vehicle_data`, qui est facturé et peut réveiller la voiture.
    À utiliser ponctuellement pour vérifier, pas en surveillance.
    """
    token = await oauth.valid_access_token(session, SUBJECT)
    return await FleetClient(token).charge_schedules(vin)


@app.delete("/vehicles/{vin}/charge-schedule/{schedule_id}")
async def delete_charge_schedule(
    vin: str, schedule_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    token = await oauth.valid_access_token(session, SUBJECT)
    return await FleetClient(token).remove_charge_schedule(vin, schedule_id)


# --- Préconditionnement ------------------------------------------------------


async def _position_ou_400(
    session: AsyncSession, vin: str, lat: float | None, lon: float | None
) -> tuple[float, float]:
    if lat is not None and lon is not None:
        return lat, lon
    position = await charging.derniere_position(session, vin)
    if position is None:
        raise HTTPException(
            400,
            "Aucune position connue : fournir lat et lon, ou attendre que la "
            "télémétrie ait remonté une position depuis le domicile.",
        )
    return position


class PreconditionRequest(BaseModel):
    """`heure` est l'instant où l'habitacle doit être prêt, pas le départ du cycle."""

    heure: str = "08:00"
    jours: str = "semaine"
    enabled: bool = True
    one_time: bool = False
    schedule_id: int | None = None
    lat: float | None = None
    lon: float | None = None


@app.post("/vehicles/{vin}/precondition-schedule")
async def create_precondition_schedule(
    vin: str,
    body: PreconditionRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Programme le préconditionnement de l'habitacle.

    La voiture calcule seule quand démarrer selon la température extérieure.
    Branchée, elle puise sur le réseau plutôt que sur la batterie — d'où
    l'intérêt de faire coïncider l'heure cible avec la fin des heures creuses.
    """
    body = body or PreconditionRequest()
    lat, lon = await _position_ou_400(session, vin, body.lat, body.lon)

    try:
        schedule = charging.build_precondition(
            lat=lat,
            lon=lon,
            heure=body.heure,
            jours=body.jours,
            enabled=body.enabled,
            one_time=body.one_time,
            schedule_id=body.schedule_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    token = await oauth.valid_access_token(session, SUBJECT)
    try:
        reponse = await FleetClient(token).add_precondition_schedule(vin, schedule)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            exc.response.status_code, f"Préconditionnement refusé : {exc.response.text[:300]}"
        ) from exc

    return {
        "envoye": schedule,
        "resume": charging.decrire_precondition(schedule),
        "reponse": reponse,
    }


@app.get("/vehicles/{vin}/precondition-schedule")
async def list_precondition_schedules(
    vin: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """⚠️ Passe par `vehicle_data` : facturé, et peut réveiller la voiture."""
    token = await oauth.valid_access_token(session, SUBJECT)
    return await FleetClient(token).precondition_schedules(vin)


@app.delete("/vehicles/{vin}/precondition-schedule/{schedule_id}")
async def delete_precondition_schedule(
    vin: str, schedule_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    token = await oauth.valid_access_token(session, SUBJECT)
    return await FleetClient(token).remove_precondition_schedule(vin, schedule_id)


@app.get("/vehicles/{vin}/telemetry/errors")
async def telemetry_errors(vin: str, session: AsyncSession = Depends(get_session)) -> dict:
    """À consulter quand la config est acceptée mais que rien n'arrive."""
    token = await oauth.valid_access_token(session, SUBJECT)
    return await FleetClient(token).telemetry_errors(vin)
