#!/usr/bin/env bash
# Certificat auto-signé pour le proxy de commandes.
#
# Le proxy n'est jamais exposé publiquement : seul le service `app` lui parle,
# sur le réseau Docker interne. Un auto-signé suffit donc — `app` le vérifie
# explicitement via son CA bundle plutôt que de désactiver la vérification TLS.
set -euo pipefail

cd "$(dirname "$0")/.."
CERTS_DIR="infra/proxy/certs"
mkdir -p "$CERTS_DIR"

openssl req -x509 -nodes -newkey ec \
  -pkeyopt ec_paramgen_curve:prime256v1 \
  -days 3650 \
  -keyout "$CERTS_DIR/proxy-key.pem" \
  -out    "$CERTS_DIR/proxy-cert.pem" \
  -subj "/CN=tesla-http-proxy" \
  -addext "subjectAltName=DNS:tesla-http-proxy,DNS:localhost,IP:127.0.0.1"

chmod 600 "$CERTS_DIR/proxy-key.pem"
echo "OK  $CERTS_DIR/proxy-cert.pem (valable 10 ans, usage interne)"
