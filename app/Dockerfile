FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY cybertinix ./cybertinix
# Le dossier web/ contient l'interface ; il part avec le paquet.

# Identifiant de build, fourni par le Makefile à chaque déploiement. C'est lui
# que la page compare au serveur pour savoir qu'une nouvelle version est là.
ARG BUILD_ID=dev
ENV BUILD_ID=$BUILD_ID

EXPOSE 8000
CMD ["uvicorn", "cybertinix.main:app", "--host", "0.0.0.0", "--port", "8000"]
