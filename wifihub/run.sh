#!/usr/bin/env bash
# Ponto de entrada do add-on: resolve o InfluxDB (externo se informado nas
# opções, senão sobe o embutido e faz o setup no 1º boot) e inicia o backend.
set -e
OPT=/data/options.json
DATA=/data
mkdir -p "$DATA"

opt() { [ -f "$OPT" ] && python3 -c "import json;print(json.load(open('$OPT')).get('$1','') or '')" || echo ""; }
rnd() { head -c "${1:-32}" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

EXT_URL="$(opt influxdb_url)"
if [ -n "$EXT_URL" ]; then
  export INFLUX_URL="$EXT_URL"
  export INFLUX_TOKEN="$(opt influxdb_token)"
  export INFLUX_ORG="$(opt influxdb_org)";   [ -z "$INFLUX_ORG" ]   && export INFLUX_ORG=homelab
  export INFLUX_BUCKET="$(opt influxdb_bucket)"; [ -z "$INFLUX_BUCKET" ] && export INFLUX_BUCKET=wifihub
  echo "[wifihub] InfluxDB EXTERNO: $INFLUX_URL"
else
  export INFLUX_URL="http://127.0.0.1:8086"
  export INFLUX_ORG=homelab
  export INFLUX_BUCKET=wifihub
  SECRETS="$DATA/influx.token"
  mkdir -p "$DATA/influxdb/engine"
  influxd --bolt-path "$DATA/influxdb/influxd.bolt" \
          --engine-path "$DATA/influxdb/engine" \
          --http-bind-address 127.0.0.1:8086 &
  echo "[wifihub] aguardando InfluxDB embutido subir..."
  for i in $(seq 1 60); do
    influx ping --host http://127.0.0.1:8086 >/dev/null 2>&1 && break
    sleep 1
  done
  if [ ! -f "$SECRETS" ]; then
    TOKEN="$(rnd 32)"
    influx setup --force --host http://127.0.0.1:8086 \
      --org "$INFLUX_ORG" --bucket "$INFLUX_BUCKET" \
      --username admin --password "$(rnd 16)" \
      --token "$TOKEN" --retention 30d
    echo "$TOKEN" > "$SECRETS"; chmod 600 "$SECRETS"
    echo "[wifihub] InfluxDB embutido inicializado"
  fi
  export INFLUX_TOKEN="$(cat "$SECRETS")"
fi

export DEVICES_PATH="$DATA/devices.json"
cd /app
echo "[wifihub] iniciando backend na porta 8099 (Ingress)"
exec uvicorn app.main:app --host 0.0.0.0 --port 8099
