FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY cybertinix ./cybertinix
# Empreinte du code réellement embarqué, indépendante de BUILD_ID : si un
# déploiement reconstruit l'ancien code (rsync non passé), BUILD_ID change
# mais pas l'empreinte. `make empreinte` donne la même valeur en local.
RUN find cybertinix -type f ! -name '*.pyc' ! -name '.DS_Store' | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -c1-8 > /srv/EMPREINTE
# Le dossier web/ contient l'interface ; il part avec le paquet.

# Identifiant de build, fourni par le Makefile à chaque déploiement. C'est lui
# que la page compare au serveur pour savoir qu'une nouvelle version est là.
ARG BUILD_ID=dev
ENV BUILD_ID=$BUILD_ID

EXPOSE 8000
CMD ["uvicorn", "cybertinix.main:app", "--host", "0.0.0.0", "--port", "8000"]
