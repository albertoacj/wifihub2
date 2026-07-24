"""Wrapper do InfluxDB 2.x: escrita de métricas e leitura de histórico (Flux).

Measurements:
  wifi_client  tags: mac, ap          fields: rx_rate, tx_rate, rx_bytes, tx_bytes, signal
  ap           tag:  ap               fields: clients, rx_rate, tx_rate
  router       (sem tag)              fields: wan_rx_rate, wan_tx_rate
  health       tag:  host             fields: cpu, npu, mem, temp, load, uptime
"""
import logging
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from .config import get_settings

log = logging.getLogger("wifihub.influx")


class Influx:
    def __init__(self):
        s = get_settings()
        self.bucket = s.influx_bucket
        self.org = s.influx_org
        self._client = InfluxDBClient(url=s.influx_url, token=s.influx_token,
                                      org=s.influx_org)
        self._write = self._client.write_api(write_options=SYNCHRONOUS)
        self._query = self._client.query_api()

    def write_points(self, points: list[Point]):
        if not points:
            return
        try:
            self._write.write(bucket=self.bucket, org=self.org, record=points)
        except Exception as exc:
            log.warning("write influx: %s", exc)

    def history(self, measurement: str, field: str, tag_key: str | None,
                tag_val: str | None, window: str = "-1h",
                every: str = "30s") -> list[dict]:
        """Retorna [{t, v}] agregado por janela para sparklines/charts."""
        filt = f'|> filter(fn: (r) => r._measurement == "{measurement}")'
        filt += f'\n|> filter(fn: (r) => r._field == "{field}")'
        if tag_key and tag_val:
            filt += f'\n|> filter(fn: (r) => r.{tag_key} == "{tag_val}")'
        flux = f'''
from(bucket: "{self.bucket}")
  |> range(start: {window})
  {filt}
  |> aggregateWindow(every: {every}, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_value"])
  |> sort(columns: ["_time"])
'''
        out = []
        try:
            tables = self._query.query(flux, org=self.org)
            for table in tables:
                for rec in table.records:
                    out.append({"t": rec.get_time().isoformat(),
                                "v": rec.get_value()})
        except Exception as exc:
            log.warning("query influx: %s", exc)
        return out

    def usage_totals(self, window: str, every: str, bucket: str | None = None) -> dict:
        """Consumo acumulado por MAC no período.

        As taxas são gravadas em bytes/s a cada coleta; a média de cada janela
        multiplicada pela duração da janela dá os bytes trafegados nela.
        Retorna {mac: {"down": bytes, "up": bytes, "series": [...]}}.
        """
        secs = _dur_seconds(every)
        flux = f'''
from(bucket: "{bucket or self.bucket}")
  |> range(start: -{window})
  |> filter(fn: (r) => r._measurement == "wifi_client" or r._measurement == "wired_client")
  |> filter(fn: (r) => r._field == "rx_rate" or r._field == "tx_rate")
  |> aggregateWindow(every: {every}, fn: mean, createEmpty: true)
  |> map(fn: (r) => ({{ r with _value: if exists r._value then r._value * {secs}.0 else 0.0 }}))
  |> group(columns: ["mac", "_field", "_time"])
  |> sum()
  |> group(columns: ["mac", "_field"])
  |> sort(columns: ["_time"])
'''
        out: dict = {}
        try:
            tables = self._query.query(flux, org=self.org)
        except Exception as exc:
            log.warning("usage_totals: %s", exc)
            return {}
        for table in tables:
            for rec in table.records:
                mac = rec.values.get("mac")
                if not mac:
                    continue
                field = rec.get_field()
                val = rec.get_value() or 0.0
                slot = out.setdefault(mac, {"down": 0.0, "up": 0.0,
                                            "dseries": [], "useries": []})
                # tx_rate = download do device, rx_rate = upload
                if field == "tx_rate":
                    slot["down"] += val
                    slot["dseries"].append(val)
                else:
                    slot["up"] += val
                    slot["useries"].append(val)
        return out


    # ---------------- histórico permanente (downsampling) ----------------

    HIST_SUFFIX = "_history"

    @property
    def hist_bucket(self) -> str:
        return f"{self.bucket}{self.HIST_SUFFIX}"

    def ensure_downsampling(self):
        """Cria o bucket histórico (retenção infinita) e a tarefa que agrega
        as taxas em médias horárias. Idempotente: roda a cada boot sem duplicar."""
        try:
            buckets = self._client.buckets_api()
            existing = buckets.find_bucket_by_name(self.hist_bucket)
            if existing is None:
                orgs = self._client.organizations_api().find_organizations(org=self.org)
                org_id = orgs[0].id
                buckets.create_bucket(bucket_name=self.hist_bucket,
                                      org_id=org_id, retention_rules=[])  # [] = para sempre
                log.info("bucket histórico criado: %s", self.hist_bucket)
        except Exception as exc:
            log.warning("bucket histórico: %s", exc)
            return

        name = "wifihub_downsample_1h"
        flux = f'''option task = {{name: "{name}", every: 1h}}

from(bucket: "{self.bucket}")
  |> range(start: -task.every)
  |> filter(fn: (r) => r._measurement == "wifi_client" or r._measurement == "wired_client"
                    or r._measurement == "ap" or r._measurement == "router"
                    or r._measurement == "health")
  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
  |> to(bucket: "{self.hist_bucket}", org: "{self.org}")
'''
        try:
            tasks = self._client.tasks_api()
            for t in tasks.find_tasks():
                if t.name == name:
                    return                      # já existe
            orgs = self._client.organizations_api().find_organizations(org=self.org)
            tasks.create_task_every(name=name, flux=flux, every="1h",
                                    organization=orgs[0])
            log.info("tarefa de downsampling criada (médias horárias, retenção infinita)")
        except Exception as exc:
            log.warning("tarefa de downsampling: %s", exc)

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass


_influx: Influx | None = None


def get_influx() -> Influx:
    global _influx
    if _influx is None:
        _influx = Influx()
    return _influx


def _dur_seconds(every: str) -> int:
    import re as _re
    m = _re.fullmatch(r"(\d+)([smhdw])", every)
    if not m:
        return 3600
    n, unit = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
