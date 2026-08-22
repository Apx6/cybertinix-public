#!/usr/bin/env bash
# Met en place les certificats TLS.
#
# Deux problèmes à résoudre :
#  1. Poule/œuf — nginx refuse de démarrer sans certificat, mais le challenge
#     HTTP-01 de Let's Encrypt a besoin de nginx. On amorce donc avec un
#     auto-signé jetable, puis on le remplace.
#  2. Les configs (nginx, fleet-telemetry) référencent un chemin fixe
#     /etc/letsencrypt/live/teslaaddict/ alors que certbot nomme son dossier
#     d'après le domaine. On pose un lien symbolique, ce qui garde les configs
#     indépendantes du nom de domaine choisi.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."
source "$SCRIPT_DIR/lib.sh"
require_env_file

# docker compose lit .env tout seul pour ses interpolations ; ici on n'extrait
# que ce dont le script a besoin.
DOMAIN=$(env_get DOMAIN)
LETSENCRYPT_EMAIL=$(env_get LETSENCRYPT_EMAIL)

: "${DOMAIN:?DOMAIN manquant dans .env}"
: "${LETSENCRYPT_EMAIL:?LETSENCRYPT_EMAIL manquant dans .env}"

VOL="teslaaddict_certbot-conf"

# Exécute une commande shell dans le volume des certificats.
in_vol() {
  docker run --rm -v "${VOL}:/etc/letsencrypt" alpine:3.20 sh -c "$1"
}

echo "== Certificat auto-signé d'amorçage =="
docker volume create "$VOL" >/dev/null
in_vol "mkdir -p /etc/letsencrypt/live/teslaaddict"
in_vol "apk add --no-cache openssl >/dev/null && \
        openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
          -keyout /etc/letsencrypt/live/teslaaddict/privkey.pem \
          -out    /etc/letsencrypt/live/teslaaddict/fullchain.pem \
          -subj '/CN=${DOMAIN}' 2>/dev/null"

echo "== Démarrage de nginx pour le challenge ACME =="
docker compose up -d nginx

echo "== Émission du certificat Let's Encrypt =="
# Un seul certificat couvre les deux noms : le domaine applicatif et le
# sous-domaine de télémétrie vers lequel le véhicule se connecte.
docker compose run --rm --entrypoint certbot certbot \
  certonly --webroot -w /var/www/certbot \
  -d "${DOMAIN}" -d "telemetry.${DOMAIN}" \
  --email "${LETSENCRYPT_EMAIL}" --agree-tos --no-eff-email \
  --non-interactive

echo "== Remplacement de l'auto-signé par le vrai certificat =="
in_vol "rm -rf /etc/letsencrypt/live/teslaaddict && \
        ln -s /etc/letsencrypt/live/${DOMAIN} /etc/letsencrypt/live/teslaaddict"

docker compose restart nginx

echo
echo "OK  certificat en place pour ${DOMAIN} et telemetry.${DOMAIN}"
echo "Vérifie que la chaîne convient au véhicule avec check_server_cert.sh"
echo "du dépôt fleet-telemetry avant de configurer la télémétrie."
