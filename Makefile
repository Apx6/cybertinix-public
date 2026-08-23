.PHONY: help keys certs proxy-cert register up check down logs ps rebuild publish

help:
	@echo "Mise en route, dans l'ordre :"
	@echo "  make keys        génère la paire de clés de l'application"
	@echo "  make proxy-cert  certificat interne du proxy de commandes"
	@echo "  make certs       certificats Let's Encrypt (DNS déjà pointé)"
	@echo "  make register    enregistre l'application auprès de Tesla"
	@echo "  make up          démarre la stack"
	@echo "  make check       vérifie que tout tourne vraiment"
	@echo
	@echo "Exploitation :"
	@echo "  make logs / ps / down / rebuild"
	@echo "  make publish     pousse le code vers le dépôt public, après crible des identifiants"

keys:
	./scripts/01-generate-keys.sh

certs:
	./scripts/02-bootstrap-certs.sh

proxy-cert:
	./scripts/03-proxy-cert.sh

register:
	./scripts/04-register-partner.sh

up:
# Sans ce garde-fou, Docker crée un dossier à la place du fichier de clé
# publique manquant, et nginx sert un 404 sur le .well-known sans erreur
# visible — l'appairage échoue alors sans raison apparente.
	@test -f infra/keys/public-key.pem || { \
		echo "!! infra/keys/public-key.pem manquant — lance 'make keys' d'abord."; \
		exit 1; }
	@test -f infra/proxy/certs/proxy-cert.pem || { \
		echo "!! certificat du proxy manquant — lance 'make proxy-cert' d'abord."; \
		exit 1; }
	BUILD_ID=$$(date +%Y%m%d-%H%M) docker compose up -d --build
	@echo
	@$(MAKE) --no-print-directory check

check:
	./scripts/05-check.sh

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

rebuild:
	BUILD_ID=$$(date +%Y%m%d-%H%M) docker compose up -d --build app

publish:
	./scripts/06-publish.sh

# Empreinte du code local, à comparer avec celle affichée dans Système.
empreinte:
	@cd app && find cybertinix -type f ! -name '*.pyc' ! -name '.DS_Store' | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256 | cut -c1-8
