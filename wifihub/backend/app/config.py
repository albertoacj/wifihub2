"""Carrega a configuração efetiva do painel (modo híbrido).

Precedência (o de baixo vence):
  1. defaults embutidos
  2. config.yml            (legado / usuários avançados, opcional)
  3. /data/setup.json      (gravado pelo wizard — autoritativo em runtime)

Segredos do InfluxDB e porta SSH vêm do ambiente. O caminho da chave SSH
vem do setup.json quando o wizard gerou uma; senão cai no env/legado.

get_settings() é cacheado; após salvar o wizard, chame reload_settings()
para aplicar ao vivo sem reiniciar o container.
"""
import os
import yaml
import logging
from dataclasses import dataclass, field
from functools import lru_cache

from . import provisioning

log = logging.getLogger("wifihub.config")


@dataclass
class Host:
    id: str
    name: str
    host: str
    user: str = "root"
    radios: list = field(default_factory=list)
    wan_iface: str = ""


@dataclass
class Settings:
    poll_interval: int
    steer_ban_time: int
    gateway: Host | None
    aps: list  # list[Host]

    link_down_mbps: int
    link_up_mbps: int

    iot_subnets: list
    iot_auto_pin: bool
    iot_ban_time: int

    # InfluxDB (via env — auto-gerado no bootstrap)
    influx_url: str
    influx_token: str
    influx_org: str
    influx_bucket: str

    # SSH
    ssh_key_path: str
    ssh_port: int

    # preferências do wizard (setup.json)
    language: str
    network_name: str
    duckdns_domain: str
    panel_password_hash: str


_DEFAULTS = {
    "poll_interval": 5,
    "steer_ban_time": 15000,
    "link_down_mbps": 600,
    "link_up_mbps": 300,
    "iot": {"subnets": ["192.168.10."], "auto_pin": False, "ban_time": 60000},
    "language": "en",
    "network_name": "",
    "gateway": None,
    "aps": [],
}


def _load_yaml() -> dict:
    """config.yml legado, se existir (opcional)."""
    path = os.environ.get("WIFIHUB_CONFIG", "/config/config.yml")
    try:
        with open(path) as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}
    except (yaml.YAMLError, OSError) as exc:
        log.warning("config.yml ilegível (%s) — ignorando", exc)
        return {}


def _merge(base: dict, over: dict) -> dict:
    """Merge raso com um nível de profundidade para os blocos aninhados."""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        elif v is not None:
            out[k] = v
    return out


def _host(raw: dict, is_gateway: bool) -> Host:
    return Host(
        id=raw["id"], name=raw.get("name", raw["id"]), host=raw["host"],
        user=raw.get("user", "root"),
        radios=raw.get("radios", []),
        wan_iface=raw.get("wan_iface", "") if is_gateway else "",
    )


@lru_cache
def get_settings() -> Settings:
    raw = _merge(_DEFAULTS, _load_yaml())
    setup = provisioning.load_setup()
    if setup:
        raw = _merge(raw, setup)

    gw_raw = raw.get("gateway")
    gateway = _host(gw_raw, True) if gw_raw else None
    aps = [_host(a, False) for a in (raw.get("aps") or [])]

    iot = raw.get("iot") or {}
    ssh_block = (setup or {}).get("ssh") or {}
    ssh_key_path = (
        ssh_block.get("key_path")
        or os.environ.get("SSH_KEY_PATH")
        or "/keys/id_ed25519"
    )

    return Settings(
        poll_interval=int(raw.get("poll_interval", 5)),
        steer_ban_time=int(raw.get("steer_ban_time", 15000)),
        gateway=gateway,
        aps=aps,
        link_down_mbps=int(raw.get("link_down_mbps", 600)),
        link_up_mbps=int(raw.get("link_up_mbps", 300)),
        iot_subnets=[str(x) for x in iot.get("subnets", ["192.168.10."])],
        iot_auto_pin=bool(iot.get("auto_pin", False)),
        iot_ban_time=int(iot.get("ban_time", 60000)),
        influx_url=os.environ.get("INFLUX_URL", "http://influxdb:8086"),
        influx_token=os.environ.get("INFLUX_TOKEN", ""),
        influx_org=os.environ.get("INFLUX_ORG", "homelab"),
        influx_bucket=os.environ.get("INFLUX_BUCKET", "wifihub"),
        ssh_key_path=ssh_key_path,
        ssh_port=int(os.environ.get("SSH_PORT", "22")),
        language=str(raw.get("language", "en")),
        network_name=str(raw.get("network_name", "")),
        duckdns_domain=str((setup or {}).get("duckdns", {}).get("domain", "")),
        panel_password_hash=str((setup or {}).get("panel_password_hash", "")),
    )


def reload_settings() -> Settings:
    """Limpa o cache e recarrega — chamado após o wizard salvar."""
    get_settings.cache_clear()
    return get_settings()
