"""Loop de coleta em segundo plano.

A cada poll_interval:
  1. lê leases DHCP do gateway (MAC -> ip/hostname)
  2. lê contadores da WAN do gateway -> taxa do roteador
  3. para cada AP, lê station dump -> clientes + taxa por device
  4. grava tudo no InfluxDB
  5. monta um "snapshot" ao vivo em memória (a UI lê daqui, rápido)

Convenção de sentido (perspectiva do device):
  download = AP tx para a estação  -> tx_rate
  upload   = AP rx da estação      -> rx_rate
"""
import os
import asyncio
import time
import logging
from datetime import datetime, timezone

from influxdb_client import Point

from .config import get_settings
from .ssh import pool
from .wifi import (ap_stations, dhcp_leases, wan_device, iface_counters,
                   gateway_neighbors, gateway_traffic, gateway_health,
                   _NEIGH_ONLINE, _NEIGH_CONFIRMADO)
from .devices import store
from .influx import get_influx
from .push import watcher as push_watcher
from .presence import presence
from .steering import enforcer as pref_enforcer, auto_pin_novos, is_iot
from .devices import store as device_store

log = logging.getLogger("wifihub.collector")

# estado anterior p/ taxa dos cabeados (nlbwmon): mac -> (rx,tx,t)
_prev_wired: dict[str, dict] = {}
# tempo maximo segurando a ultima taxa conhecida quando o contador nao mexe.
# Precisa ser maior que o refresh_interval do nlbwmon (padrao 30s), senao a
# taxa zera no meio do intervalo e o grafico volta a serrilhar.
WIRED_HOLD = float(os.getenv("WIRED_RATE_HOLD", "90"))


def _ip_key(ip: str | None):
    """Chave de ordenação estável por IP (octetos numéricos)."""
    try:
        return tuple(int(x) for x in (ip or "").split("."))
    except Exception:
        return (999, 999, 999, 999)

# snapshot ao vivo lido pelas rotas /api/state
snapshot: dict = {"ts": None, "router": {}, "aps": [], "extra": None, "ready": False}

# estado anterior p/ cálculo de taxa: mac -> (rx_bytes, tx_bytes, t)
_prev_client: dict[str, tuple[int, int, float]] = {}
_prev_wan: dict[str, tuple[int, int, float]] = {}


def _rate(prev, key, rx, tx, now):
    """bytes/s de download(tx) e upload(rx) desde a última amostra.
    (perspectiva do AP: 'rx' do AP = upload do device)"""
    p = prev.get(key)
    prev[key] = (rx, tx, now)
    if not p:
        return 0.0, 0.0
    prx, ptx, pt = p
    dt = now - pt
    if dt <= 0:
        return 0.0, 0.0
    # contador pode zerar (reboot/roam) -> ignora negativos
    up = max(0, rx - prx) / dt
    down = max(0, tx - ptx) / dt
    return down, up


def _pair_rate(prev, key, a, b, now):
    """bytes/s de (a, b) direto, sem trocar sentido.
    Para a WAN: a=rx=download (entra), b=tx=upload (sai)."""
    p = prev.get(key)
    prev[key] = (a, b, now)
    if not p:
        return 0.0, 0.0
    pa, pb, pt = p
    dt = now - pt
    if dt <= 0:
        return 0.0, 0.0
    return max(0, a - pa) / dt, max(0, b - pb) / dt


def _counter_rate(prev, key, a, b, now, hold=WIRED_HOLD):
    """Taxa a partir de contador que so avanca de vez em quando.

    O nlbwmon atualiza a contabilidade a cada refresh_interval (30s por
    padrao) enquanto a coleta roda a cada 5s. Dividir o delta pelo intervalo
    de coleta daria 0 na maioria das leituras e um pico enorme quando o
    contador finalmente anda. Aqui a divisao e pelo tempo desde a ultima
    mudanca real, e o valor e mantido entre as atualizacoes.
    """
    st = prev.get(key)
    if st is None:
        prev[key] = {"a": a, "b": b, "changed": now, "ra": 0.0, "rb": 0.0}
        return 0.0, 0.0

    if a < st["a"] or b < st["b"]:
        # contador reiniciou (device reconectou ou nlbwmon zerou a base)
        st.update(a=a, b=b, changed=now, ra=0.0, rb=0.0)
        return 0.0, 0.0

    da, db = a - st["a"], b - st["b"]
    if da or db:
        dt = now - st["changed"]
        if dt > 0:
            st["ra"], st["rb"] = da / dt, db / dt
        st.update(a=a, b=b, changed=now)
    elif now - st["changed"] > hold:
        st["ra"] = st["rb"] = 0.0      # parado ha tempo demais: e zero mesmo

    return st["ra"], st["rb"]


def _health_points(host_id: str, health: dict, stamp) -> list[Point]:
    """Transforma o dict de saude em pontos do Influx (measurement 'health')."""
    if not health:
        return []
    pt = Point("health").tag("host", host_id).time(stamp)
    got = False
    for key in ("cpu", "npu", "temp", "load"):
        val = health.get(key)
        if val is not None:
            pt = pt.field(key, float(val))
            got = True
    if health.get("mem") is not None:
        pt = pt.field("mem", float(health["mem"]))
        got = True
    if health.get("mem_used") is not None:
        pt = pt.field("mem_used", float(health["mem_used"]))
        got = True
    if health.get("uptime") is not None:
        pt = pt.field("uptime", float(health["uptime"]))
        got = True
    return [pt] if got else []


async def _poll_ap(ap, leases, neigh, now, stamp):
    """Coleta um AP. Retorna (entry_dict, pontos_influx)."""
    entry = {"id": ap.id, "name": ap.name, "host": ap.host,
             "online": True, "clients": [], "client_count": 0,
             "rx_rate": 0.0, "tx_rate": 0.0}
    points: list[Point] = []
    try:
        stations = await ap_stations(ap)
    except Exception as exc:
        entry["online"] = False
        log.warning("ap %s offline: %s", ap.id, exc)
        return entry, points

    ap_down = ap_up = 0.0
    for mac, d in stations.items():
        down, up = _rate(_prev_client, mac, d["rx_bytes"], d["tx_bytes"], now)
        ap_down += down
        ap_up += up
        lease = leases.get(mac, {})
        # sem lease (comum em MAC aleatório recém-trocado) -> tenta a tabela ARP
        ip = lease.get("ip") or (neigh.get(mac) or {}).get("ip")
        meta = store.get(mac, ip)
        entry["clients"].append({
            "mac": mac,
            "ip": ip,
            "hostname": lease.get("hostname"),
            "name": meta.get("name") or lease.get("hostname") or mac,
            "icon": meta.get("icon", "generic"),
            "pref_ap": meta.get("pref_ap"),
            "iot": is_iot(lease.get("ip") or d.get("ip")),
            "ap": ap.id,
            "band": d.get("band"),
            "signal": d.get("signal", 0),
            "connected": d.get("connected", 0),
            "inactive": d.get("inactive", 10**9),
            "rx_rate": round(up, 1),      # upload
            "tx_rate": round(down, 1),    # download
        })
        points.append(
            Point("wifi_client").tag("mac", mac).tag("ap", ap.id)
            .field("rx_rate", up).field("tx_rate", down)
            .field("signal", float(d.get("signal", 0)))
            .time(stamp)
        )

    # saude do proprio AP (CPU/memoria/temperatura) — mesma leitura do gateway
    try:
        entry["health"] = await gateway_health(ap)
        points.extend(_health_points(ap.id, entry["health"], stamp))
    except Exception as exc:
        log.warning("health %s: %s", ap.id, exc)

    entry["clients"].sort(key=lambda c: _ip_key(c.get("ip")))
    entry["client_count"] = len(entry["clients"])
    entry["rx_rate"] = round(ap_up, 1)
    entry["tx_rate"] = round(ap_down, 1)
    points.append(
        Point("ap").tag("ap", ap.id)
        .field("clients", entry["client_count"])
        .field("rx_rate", ap_up).field("tx_rate", ap_down)
        .time(stamp)
    )
    return entry, points


async def _collect_once():
    s = get_settings()
    influx = get_influx()
    now = time.monotonic()
    stamp = datetime.now(timezone.utc)
    points: list[Point] = []

    leases = {}
    try:
        leases = await dhcp_leases(s.gateway)
    except Exception as exc:
        log.warning("leases: %s", exc)

    # tabela ARP do gateway: usada como fonte alternativa de IP (MAC rotativo)
    # e para listar os dispositivos de cabo mais adiante
    try:
        neigh = await gateway_neighbors(s.gateway)
    except Exception as exc:
        neigh = {}
        log.warning("neigh: %s", exc)

    # --- Roteador (WAN) ---
    router = {"id": s.gateway.id, "name": s.gateway.name,
              "wan_rx_rate": 0.0, "wan_tx_rate": 0.0, "online": True}
    try:
        dev = await wan_device(s.gateway)
        rx, tx = await iface_counters(s.gateway, dev)
        # interface WAN: rx = download (entra da internet), tx = upload (sai)
        dl, ul = _pair_rate(_prev_wan, "wan", rx, tx, now)
        router["wan_rx_rate"] = dl   # download
        router["wan_tx_rate"] = ul   # upload
        points.append(Point("router")
                      .field("wan_rx_rate", dl).field("wan_tx_rate", ul)
                      .time(stamp))
    except Exception as exc:
        router["online"] = False
        log.warning("wan counters: %s", exc)

    # --- APs (em paralelo) ---
    results = await asyncio.gather(
        *[_poll_ap(ap, leases, neigh, now, stamp) for ap in s.aps],
        return_exceptions=True,
    )
    aps_out = []
    total_clients = 0
    for ap, res in zip(s.aps, results):
        if isinstance(res, Exception):
            log.warning("ap %s erro: %s", ap.id, res)
            aps_out.append({"id": ap.id, "name": ap.name, "host": ap.host,
                            "online": False, "clients": [], "client_count": 0,
                            "rx_rate": 0.0, "tx_rate": 0.0})
            continue
        entry, pts = res
        points.extend(pts)
        total_clients += entry["client_count"]
        aps_out.append(entry)

    # Fast roaming: por alguns segundos o mesmo cliente aparece em 2 APs (a
    # associação antiga ainda não expirou). Mantém cada MAC só no AP onde está
    # mais ATIVO — menor "inactive time"; desempate por melhor sinal — e remove
    # a associação fantasma do AP antigo (que some do card e da contagem).
    _best = {}
    for _ai, _a in enumerate(aps_out):
        for _c in _a.get("clients", []):
            _key = (_c.get("inactive", 10**9), -(_c.get("signal") or -999))
            if _c["mac"] not in _best or _key < _best[_c["mac"]][0]:
                _best[_c["mac"]] = (_key, _ai)
    for _ai, _a in enumerate(aps_out):
        _a["clients"] = [c for c in _a.get("clients", [])
                         if _best.get(c["mac"], (None, _ai))[1] == _ai]
        _a["client_count"] = len(_a["clients"])
    total_clients = sum(a.get("client_count", 0) for a in aps_out)

    router["clients_total"] = total_clients

    # --- Dispositivos por cabo / estáticos (conhecidos do gateway) ---
    wifi_macs = {c["mac"] for a in aps_out for c in a["clients"]}
    infra_ips = {s.gateway.host} | {ap.host for ap in s.aps}
    wired = []
    try:
        router["health"] = await gateway_health(s.gateway)
        points.extend(_health_points(s.gateway.id, router["health"], stamp))
    except Exception as exc:
        log.warning("health: %s", exc)

    try:
        traffic = await gateway_traffic(s.gateway)   # {} se nlbwmon ausente
    except Exception as exc:
        traffic = {}
        log.warning("nlbw: %s", exc)

    def _add(mac, ip, state):
        meta = store.get(mac, ip)
        # Um device Wi-Fi que desconecta deixa a entrada ARP em STALE por
        # minutos. Sem esta guarda ele apareceria como cabeado no Switch.
        # Se ja sabemos que ele e Wi-Fi e nao esta em nenhum AP agora, ele
        # esta offline — vai para o bloco Offline, nao para o Switch.
        if meta.get("link") == "wifi" and mac not in wifi_macs:
            return
        # STALE sozinho nao prova nada: tanto um cabeado ocioso quanto um
        # Wi-Fi que ja saiu ficam STALE. Entao: entra no Switch quem esta
        # confirmado agora, ou quem ja foi confirmado como cabo alguma vez.
        # Um STALE sem rotulo e quase sempre rastro de Wi-Fi desligado.
        if state not in _NEIGH_CONFIRMADO and meta.get("link") != "cabo":
            return
        lease = leases.get(mac, {})
        dev = {
            "mac": mac,
            "ip": ip,
            "hostname": lease.get("hostname"),
            "name": meta.get("name") or lease.get("hostname") or mac,
            "icon": meta.get("icon", "generic"),
            "pref_ap": meta.get("pref_ap"),
            "state": state,
            "link": meta.get("link") or "cabo",
            "online": state in _NEIGH_ONLINE,
            "rx_rate": 0.0, "tx_rate": 0.0, "metered": False,
        }
        t = traffic.get(mac)
        if t:
            dl, ul = _counter_rate(_prev_wired, mac, t[0], t[1], now)
            dev["tx_rate"] = round(dl, 1)   # download
            dev["rx_rate"] = round(ul, 1)   # upload
            dev["metered"] = True
            points.append(Point("wired_client").tag("mac", mac)
                          .field("rx_rate", ul).field("tx_rate", dl).time(stamp))
        wired.append(dev)

    seen = set()
    for mac, n in neigh.items():
        if mac in wifi_macs or n["ip"] in infra_ips:
            continue
        _add(mac, n["ip"], n["state"])
        seen.add(mac)
    # leases sem vizinho ativo (device offline mas com reserva)
    for mac, l in leases.items():
        if mac in wifi_macs or mac in seen or l.get("ip") in infra_ips:
            continue
        _add(mac, l.get("ip"), "LEASE")

    # mantem cada device no AP preferido (com cooldown e desistencia)
    try:
        await auto_pin_novos(aps_out)
        await pref_enforcer.enforce(aps_out)
    except Exception as exc:
        log.warning("steering: %s", exc)

    # aprende como cada MAC se conecta (so grava quando muda)
    try:
        vistos = {m: "wifi" for m in wifi_macs}
        # so marca cabo com prova de presenca (o filtro acima ja garante isso)
        vistos.update({c["mac"]: "cabo" for c in wired
                       if c["mac"] not in wifi_macs and c.get("online")})
        await store.note_link(vistos)
    except Exception as exc:
        log.warning("note_link: %s", exc)

    # No Switch entram só os cabeados de fato online; os offline (lease sem
    # vizinho ativo, online=False) saem daqui e caem no bloco Offline.
    wired_on = [c for c in wired if c.get("online")]
    wired_on.sort(key=lambda c: _ip_key(c.get("ip")))
    router["clients_total"] = total_clients + len(wired_on)
    # soma o tráfego dos cabeados (disponível quando o nlbwmon está ativo)
    w_down = sum(c.get("tx_rate", 0.0) for c in wired_on)
    w_up = sum(c.get("rx_rate", 0.0) for c in wired_on)
    extra = {"id": "wired", "name": "Switch", "kind": "wired",
             "online": True, "clients": wired_on, "client_count": len(wired_on),
             "metered": any(c.get("metered") for c in wired_on),
             "rx_rate": round(w_up, 1), "tx_rate": round(w_down, 1)}

    # ---- quem esta na rede agora, e quem sumiu ----
    online_now = {(c.get("mac") or "").lower()
                  for a in aps_out for c in a["clients"]}
    online_now |= {(c.get("mac") or "").lower() for c in wired_on}
    online_now.discard("")
    presence.touch(online_now)
    presence.maybe_flush()

    # So entram na lista os devices que voce nomeou ou deu icone: sem esse
    # filtro, todo MAC rotativo de visitante viraria uma linha de "offline".
    gone = []
    OFFLINE_WINDOW = 7 * 86400   # 7 dias
    now_ts = int(time.time())
    for mac, meta in device_store.all().items():
        mac = mac.lower()
        if mac in online_now:
            continue
        seen = presence.last_seen(mac)
        known = meta.get("name") or meta.get("icon") or is_iot(meta.get("ip"))
        recent = seen is not None and (now_ts - seen) <= OFFLINE_WINDOW
        # nomeados/IoT sempre; e qualquer um visto nos últimos 7 dias, mesmo
        # sem nome (a janela evita acumular MAC de visitante para sempre)
        if not (known or recent):
            continue
        gone.append({"mac": mac,
                     "name": meta.get("name") or mac,
                     "icon": meta.get("icon") or "generic",
                     "ip": meta.get("ip"),
                     "link": meta.get("link") or "",
                     "last_seen": seen})
    gone.sort(key=lambda d: d["last_seen"] or 0, reverse=True)
    offline = {"id": "offline", "name": "Offline", "kind": "offline",
               "online": True, "clients": gone, "client_count": len(gone)}

    influx.write_points(points)

    snapshot["ts"] = stamp.isoformat()
    snapshot["router"] = router
    snapshot["aps"] = aps_out
    snapshot["extra"] = extra
    snapshot["offline"] = offline
    snapshot["ready"] = True

    # --- alguem novo entrou na rede? ---
    try:
        todos = [c for a in aps_out for c in a["clients"]] + wired
        await push_watcher.check(todos)
    except Exception as exc:
        log.warning("push: %s", exc)


async def run_collector():
    s = get_settings()
    log.info("coletor iniciado (intervalo %ss)", s.poll_interval)
    while True:
        try:
            await _collect_once()
        except Exception as exc:
            log.exception("coleta falhou: %s", exc)
        await asyncio.sleep(s.poll_interval)
