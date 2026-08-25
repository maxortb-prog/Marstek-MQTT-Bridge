# syntax=docker/dockerfile:1
ARG BUILD_FROM
FROM $BUILD_FROM

# python3/pip aus Alpine-Paketen (Home-Assistant-Basis-Images sind Alpine-basiert)
RUN apk add --no-cache python3 py3-pip

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
