"""Protection des routes par jeton partagé.

Le domaine n'est pas un secret : il apparaît en clair dans les journaux de
transparence des certificats, publics et indexés. Sans authentification,
n'importe qui pourrait lire l'état du véhicule, et surtout supprimer sa
configuration de télémétrie.

Deux voies d'accès :

  - **Cookie de session**, posé par la page `/login`. C'est un vrai formulaire
    HTML avec un champ mot de passe : iOS et les gestionnaires de mots de passe
    proposent de l'enregistrer, ce que l'invite HTTP Basic du navigateur ne
    permet pas. Le cookie ne contient pas le jeton mais un dérivé HMAC, donc
    un cookie qui fuite ne livre pas le secret.
  - **`Authorization: Bearer <jeton>`**, pour curl et les scripts.

Restent ouvertes `/healthz` (sondé par `make check` et la surveillance externe),
`/auth/callback` (où Tesla redirige sans pouvoir porter notre jeton — protégé
par le `state` que seul `/auth/login` sait émettre) et `/login` elle-même.

`/auth/login` est protégé délibérément : sans cela, un tiers pourrait dérouler
le flux OAuth avec son propre compte Tesla et écraser le jeton enregistré.
"""

import hashlib
import hmac
import logging
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from .config import settings

log = logging.getLogger(__name__)

ROUTES_OUVERTES = {"/healthz", "/auth/callback", "/login", "/logout"}
COOKIE = "session"
# Un an : l'appareil reste connecté, le gestionnaire de mots de passe sert
# surtout à retrouver le jeton sur un nouvel appareil.
DUREE = 365 * 24 * 3600


def valeur_session() -> str:
    """Dérivé du jeton, stable tant que le jeton ne change pas.

    Changer `API_TOKEN` invalide donc toutes les sessions d'un coup.
    """
    return hmac.new(settings.api_token.encode(), b"session-v1", hashlib.sha256).hexdigest()


def jeton_valide(candidat: str | None) -> bool:
    return bool(candidat) and secrets.compare_digest(candidat, settings.api_token)


def session_valide(candidat: str | None) -> bool:
    return bool(candidat) and secrets.compare_digest(candidat, valeur_session())


def poser_cookie(reponse: RedirectResponse) -> None:
    reponse.set_cookie(
        COOKIE,
        valeur_session(),
        max_age=DUREE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _bearer(request: Request) -> str | None:
    schema, _, valeur = request.headers.get("authorization", "").partition(" ")
    return valeur.strip() if schema.lower() == "bearer" and valeur else None


def _veut_du_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


async def middleware(request: Request, call_next):
    if request.url.path in ROUTES_OUVERTES:
        return await call_next(request)

    if not settings.api_token:
        # Échec fermé : servir ouvertement serait pire que de ne pas servir.
        log.error("API_TOKEN absent — routes protégées indisponibles")
        return JSONResponse(
            status_code=503,
            content={
                "detail": "API_TOKEN non configuré dans .env. "
                "Générer un jeton avec `openssl rand -hex 32`, le renseigner, "
                "puis redémarrer le conteneur app."
            },
        )

    if session_valide(request.cookies.get(COOKIE)) or jeton_valide(_bearer(request)):
        return await call_next(request)

    # Un navigateur est envoyé vers le formulaire ; un script reçoit un 401.
    if _veut_du_html(request) and request.method == "GET":
        return RedirectResponse("/login", status_code=303)
    return JSONResponse(status_code=401, content={"detail": "Jeton absent ou invalide."})
