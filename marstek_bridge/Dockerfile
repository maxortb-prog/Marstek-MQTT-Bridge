# syntax=docker/dockerfile:1
FROM ghcr.io/home-assistant/base:3.19

ARG BUILD_VERSION

# python3/pip aus Alpine-Paketen (Home-Assistant-Basis-Images sind Alpine-basiert)
RUN apk add --no-cache python3 py3-pip

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

LABEL \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="armhf|armv7|i386|aarch64|amd64" \
      org.opencontainers.image.title="Marstek MQTT Bridge" \
      org.opencontainers.image.description="UDP-zu-MQTT Bruecke fuer Marstek Energiespeicher (lokale UDP-API) und Home Assistant per MQTT-Discovery, inkl. traegem Self-Consumption-Regler fuer den Passive-Mode." \
      org.opencontainers.image.source="https://github.com/maxortb-prog/Marstek-MQTT-Bridge"

CMD [ "/run.sh" ]
