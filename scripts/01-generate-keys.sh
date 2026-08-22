#!/usr/bin/env bash
# Génère la paire de clés de l'application (clé virtuelle Tesla).
#
# La courbe est imposée : le véhicule n'accepte que prime256v1 (secp256r1).
# La clé privée signe les commandes et les configurations de télémétrie ; elle
# ne doit jamais quitter le serveur ni être versionnée.
set -euo pipefail

cd "$(dirname "$0")/.."
KEYS_DIR="infra/keys"
mkdir -p "$KEYS_DIR"

if [[ -f "$KEYS_DIR/private-key.pem" ]]; then
  echo "!! $KEYS_DIR/private-key.pem existe déjà."
  echo "   La régénérer invaliderait toutes les clés virtuelles déjà appairées"
  echo "   aux véhicules — il faudrait refaire l'appairage à la main."
  echo "   Supprime le fichier explicitement si c'est bien ce que tu veux."
  exit 1
fi

openssl ecparam -name prime256v1 -genkey -noout -out "$KEYS_DIR/private-key.pem"
openssl ec -in "$KEYS_DIR/private-key.pem" -pubout -out "$KEYS_DIR/public-key.pem"
chmod 600 "$KEYS_DIR/private-key.pem"

echo "OK  clé privée : $KEYS_DIR/private-key.pem  (600, jamais commitée)"
echo "OK  clé publique: $KEYS_DIR/public-key.pem"
echo
echo "La clé publique sera servie par nginx sur :"
echo "  https://<domaine>/.well-known/appspecific/com.tesla.3p.public-key.pem"
echo "Elle doit y rester accessible en permanence."
