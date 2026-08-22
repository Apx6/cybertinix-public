# Proxy de commandes signées de Tesla, construit depuis les sources.
#
# Pas d'image officielle publiée. `go install ...@latest` ne marche pas non
# plus : le go.mod du dépôt contient des directives `replace`, que Go refuse
# hors module principal. On clone donc et on build sur place.
FROM golang:1.23-alpine AS build

RUN apk add --no-cache git

ARG VEHICLE_COMMAND_VERSION=v0.4.1
RUN git clone --depth 1 --branch ${VEHICLE_COMMAND_VERSION} \
      https://github.com/teslamotors/vehicle-command.git /src

WORKDIR /src
RUN CGO_ENABLED=0 go build -o /out/tesla-http-proxy ./cmd/tesla-http-proxy

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /out/tesla-http-proxy /usr/local/bin/tesla-http-proxy
EXPOSE 4443
ENTRYPOINT ["/usr/local/bin/tesla-http-proxy"]
