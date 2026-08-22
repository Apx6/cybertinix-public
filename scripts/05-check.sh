#!/usr/bin/env bash
# Contrôle de santé de la stack.
#
# Existe parce qu'une boucle de crash est silencieuse : `make up` rend la main
# sans erreur, `docker compose ps` affiche volontiers "Up" pour un conteneur qui
# vient de redémarrer pour la vingtième fois, et une sonde depuis l'extérieur
# peut donner l'illusion qu'un service répond alors que c'est nginx qui ferme
# la connexion à sa place. On regarde donc l'état réel, pas les apparences.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."
source "$SCRIPT_DIR/lib.sh"
require_env_file

DOMAIN=$(env_get DOMAIN)
fail=0

echo "== Conteneurs =="
while read -r name state restarts; do
  [[ -z "$name" ]] && continue
  if [[ "$state" != "running" ]]; then
    printf '  ✗ %-32s %s\n' "$name" "$state"
    fail=1
  elif (( restarts > 3 )); then
    printf '  ✗ %-32s running, mais %s redémarrages : boucle de crash\n' "$name" "$restarts"
    fail=1
  else
    printf '  ✓ %-32s %s\n' "$name" "$state"
  fi
done < <(docker compose ps -q | xargs -r docker inspect \
           --format '{{.Name}} {{.State.Status}} {{.RestartCount}}' | sed 's|^/||')

echo
echo "== Serveur de télémétrie =="
# Le test qui manquait : sans lui, un fleet-telemetry mort passe inaperçu
# jusqu'à ce qu'on se demande pourquoi le véhicule n'envoie rien.
if docker compose exec -T nginx nc -z fleet-telemetry 8443 2>/dev/null; then
  echo "  ✓ fleet-telemetry:8443 accepte les connexions"
else
  echo "  ✗ fleet-telemetry:8443 injoignable — le véhicule ne peut pas se connecter"
  echo "    docker compose logs fleet-telemetry --tail 20"
  fail=1
fi

echo
echo "== Endpoints publics =="
# On réessaie pendant 30 s : appelé juste après `make up`, l'app n'a pas fini
# de démarrer et nginx répond 502 le temps qu'elle soit prête. Un contrôle qui
# crie au loup apprend à ignorer ses propres alertes.
check_url() {
  local label=$1 url=$2 expected=$3
  local code deadline=$((SECONDS + 30))
  while :; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url")
    [[ "$code" == "$expected" ]] && break
    (( SECONDS >= deadline )) && break
    sleep 2
  done
  if [[ "$code" == "$expected" ]]; then
    printf '  ✓ %-32s %s\n' "$label" "$code"
  else
    printf '  ✗ %-32s %s (attendu %s, après 30 s)\n' "$label" "$code" "$expected"
    fail=1
  fi
}
check_url "/healthz" "https://${DOMAIN}/healthz" 200
check_url "clé publique" \
  "https://${DOMAIN}/.well-known/appspecific/com.tesla.3p.public-key.pem" 200

echo
if (( fail )); then
  echo "ÉCHEC — voir les lignes marquées ✗ ci-dessus."
  exit 1
fi
echo "OK  tout est en service."
