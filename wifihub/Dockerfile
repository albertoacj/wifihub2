# Copia os binários do InfluxDB 2.x da imagem oficial (multi-arch: o Docker
# escolhe aarch64/amd64 conforme o host — sem baixar tarball à mão).
FROM influxdb:2.7 AS influx

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 LANG=C.UTF-8
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=influx /usr/bin/influxd /usr/local/bin/influxd
COPY --from=influx /usr/bin/influx  /usr/local/bin/influx

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY backend/static ./static
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD [ "/run.sh" ]
