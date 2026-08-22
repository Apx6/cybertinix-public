#!/usr/bin/env bash
# Enregistrement de l'application auprès de Tesla (étape 4 de l'onboarding).
#
# À faire une seule fois par région d'opération, après que la clé publique soit
# effectivement servie sur le domaine. Tesla va la relire pendant cet appel :
# si elle n'est pas joignable, l'enregistrement échoue.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."
source "$SCRIPT_DIR/lib.sh"
require_env_file

DOMAIN=$(env_get DOMAIN)
TESLA_CLIENT_ID=$(env_get TESLA_CLIENT_ID)
TESLA_CLIENT_SECRET=$(env_get TESLA_CLIENT_SECRET)
TESLA_AUDIENCE=$(env_get TESLA_AUDIENCE)

: "${DOMAIN:?DOMAIN manquant dans .env}"
: "${TESLA_CLIENT_ID:?TESLA_CLIENT_ID manquant dans .env}"
: "${TESLA_CLIENT_SECRET:?TESLA_CLIENT_SECRET manquant dans .env}"
: "${TESLA_AUDIENCE:?TESLA_AUDIENCE manquant dans .env}"

echo "== Vérification préalable de la clé publique =="
KEY_URL="https://${DOMAIN}/.well-known/appspecific/com.tesla.3p.public-key.pem"
if ! curl -fsS "$KEY_URL" | grep -q "BEGIN PUBLIC KEY"; then
  echo "!! La clé publique n'est pas servie sur $KEY_URL"
  echo "   Tesla la relit à chaque appairage : corrige ça avant de continuer."
  exit 1
fi
echo "OK  clé publique joignable"

echo "== Génération du partner token =="
PARTNER_TOKEN=$(curl -fsS --request POST \
  --header 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'grant_type=client_credentials' \
  --data-urlencode "client_id=${TESLA_CLIENT_ID}" \
  --data-urlencode "client_secret=${TESLA_CLIENT_SECRET}" \
  --data-urlencode 'scope=openid vehicle_device_data vehicle_cmds vehicle_charging_cmds vehicle_location' \
  --data-urlencode "audience=${TESLA_AUDIENCE}" \
  'https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

echo "== Enregistrement du compte partenaire =="
curl -fsS --request POST \
  --header "Authorization: Bearer ${PARTNER_TOKEN}" \
  --header 'Content-Type: application/json' \
  --data "{\"domain\": \"${DOMAIN}\"}" \
  "${TESLA_AUDIENCE}/api/1/partner_accounts" | python3 -m json.tool

echo
echo "== Vérification : clé publique enregistrée côté Tesla =="
curl -fsS --header "Authorization: Bearer ${PARTNER_TOKEN}" \
  "${TESLA_AUDIENCE}/api/1/partner_accounts/public_key?domain=${DOMAIN}" | python3 -m json.tool

echo
echo "OK  application enregistrée sur ${TESLA_AUDIENCE}"
echo "Rappel : à refaire pour chaque région où tu opères."
