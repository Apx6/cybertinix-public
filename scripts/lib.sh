#!/usr/bin/env bash
# Fonctions partagées par les scripts d'installation.

# Lit une valeur du .env sans l'interpréter.
#
# `source .env` paraît plus simple mais casse dès qu'une valeur contient un
# espace : bash lit `TESLA_SCOPES=openid offline_access …` comme une affectation
# suivie d'une commande `offline_access`. Quoter la valeur dans le .env n'est pas
# une option non plus — Docker Compose lit le même fichier et attend des valeurs
# brutes. On extrait donc au lieu d'exécuter.
env_get() {
  local value
  # Dernière occurrence, comme le ferait `source` : si une clé est renseignée
  # deux fois, c'est la ligne du bas qui compte, pas la ligne vide d'origine.
  value=$(grep -E "^$1=" .env | tail -1) || return 0
  value=${value#*=}
  value=${value%\"}; value=${value#\"}
  value=${value%\'}; value=${value#\'}
  printf '%s' "$value"
}

require_env_file() {
  [[ -f .env ]] || {
    echo "!! .env manquant — pars de .env.example"
    exit 1
  }
}
