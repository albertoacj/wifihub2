"""Descoberta de clientes por AP, tráfego por estação e "mover para AP X".

Fontes de dado em cada AP (via SSH):
  - `iw dev <radio> station dump`  -> MAC, rx/tx bytes, sinal, tempo conectado
Fonte no gateway:
  - `/tmp/dhcp.leases`             -> MAC -> IP + hostname (nome de fallback)

Steering: para forçar um MAC a migrar para o AP alvo, mandamos os demais APs
desautenticarem + banirem esse MAC por alguns segundos (ubus hostapd del_client
com ban_time). Como só o alvo aceita associação, o cliente migra pra lá.
"""
import json
import re
import logging

from .config import get_settings, Host
from .ssh import pool

log = logging.getLogger("wifihub.wifi")

_MAC_RE = re.compile(r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})")


def _num(s: str) -> int:
    m = re.search(r"-?\d+", s)
    return int(m.group()) if m else 0


def parse_station_dump(text: str) -> dict[str, dict]:
    """Converte a saída de `iw ... station dump` em {mac: {campos}}."""
    stations: dict[str, dict] = {}
    cur = None
    for line in text.splitlines():
        m = re.match(r"Station\s+([0-9a-fA-F:]{17})", line.strip())
        if m:
            cur = m.group(1).lower()
            stations[cur] = {"rx_bytes": 0, "tx_bytes": 0, "signal": 0,
                             "inactive": 10**9,
                             "connected": 0, "tx_rate": 0.0, "rx_rate": 0.0}
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("rx bytes:"):
            stations[cur]["rx_bytes"] = _num(s)
        elif s.startswith("tx bytes:"):
            stations[cur]["tx_bytes"] = _num(s)
        elif s.startswith("signal:") and "avg" not in s:
            stations[cur]["signal"] = _num(s)
        elif s.startswith("inactive time:"):
            stations[cur]["inactive"] = _num(s)
        elif s.startswith("connected time:"):
            stations[cur]["connected"] = _num(s)
        elif s.startswith("tx bitrate:"):
            stations[cur]["tx_rate"] = float(re.search(r"[\d.]+", s).group() or 0)
        elif s.startswith("rx bitrate:"):
            stations[cur]["rx_rate"] = float(re.search(r"[\d.]+", s).group() or 0)
    return stations


async def gateway_neighbors(gateway: Host) -> dict[str, dict]:
    """Tabela de vizinhos (ARP/ND) do gateway: mac -> {ip, dev, state}.

    Pega dispositivos que o RM1800 enxerga em qualquer VLAN (LAN, IoT, etc.),
    inclusive os de IP estático que não aparecem nos leases DHCP.
    """
    out = await pool.run(gateway, "ip neigh show 2>/dev/null || true")
    res: dict[str, dict] = {}
    for line in out.splitlines():
        m = re.match(
            r"(\d+\.\d+\.\d+\.\d+)\s+dev\s+(\S+).*?lladdr\s+([0-9a-fA-F:]{17})\s+(\w+)",
            line.strip(),
        )
        if not m:
            continue
        ip, dev, mac, state = m.group(1), m.group(2), m.group(3).lower(), m.group(4)
        res[mac] = {"ip": ip, "dev": dev, "state": state}
    return res


async def gateway_traffic(gateway: Host) -> dict[str, tuple[int, int]]:
    """Tráfego acumulado por MAC via nlbwmon: mac -> (rx_bytes, tx_bytes).

    Requer `nlbwmon` no gateway. Retorna {} se ausente.
    O `nlbw -c csv` do OpenWrt separa por TAB e envolve strings em aspas,
    então dividimos por tab/vírgula e removemos aspas. rx = download do host.
    """
    out = await pool.run(gateway, "nlbw -c csv 2>/dev/null || true")
    lines = [l for l in out.splitlines() if l.strip()]
    if len(lines) < 2:
        return {}

    def cells(line: str) -> list[str]:
        parts = line.split("\t") if "\t" in line else line.split(",")
        return [p.strip().strip('"').strip() for p in parts]

    header = [h.lower() for h in cells(lines[0])]

    def find(pred):
        for i, h in enumerate(header):
            if pred(h):
                return i
        return -1

    i_mac = find(lambda h: h == "mac") 
    if i_mac < 0:
        i_mac = find(lambda h: "mac" in h)
    i_rx = find(lambda h: ("rx" in h or "down" in h) and "byte" in h)
    i_tx = find(lambda h: ("tx" in h or "up" in h) and "byte" in h)
    if min(i_mac, i_rx, i_tx) < 0:
        return {}

    agg: dict[str, tuple[int, int]] = {}
    for line in lines[1:]:
        cols = cells(line)
        if len(cols) <= max(i_mac, i_rx, i_tx):
            continue
        mac = cols[i_mac].lower()
        if not re.match(r"[0-9a-f:]{17}$", mac):
            continue
        try:
            rx, tx = int(cols[i_rx]), int(cols[i_tx])
        except ValueError:
            continue
        prev = agg.get(mac, (0, 0))
        agg[mac] = (prev[0] + rx, prev[1] + tx)
    return agg


# estados de vizinhança considerados "online" (STALE = conhecido/ativo, só não
# reconfirmado agora; só FAILED/INCOMPLETE indicam de fato inalcançável)
_NEIGH_ONLINE = {"REACHABLE", "DELAY", "PROBE", "PERMANENT", "NOARP", "STALE"}

# Estados que servem de PROVA de que o device está mesmo ali agora.
# STALE fica de fora: é o rastro que sobra depois que um device sai, e um
# aparelho Wi-Fi desconectado continua STALE por minutos. Usar STALE como
# prova rotularia Wi-Fi como cabo — e o rótulo é gravado em disco.
_NEIGH_CONFIRMADO = {"REACHABLE", "DELAY", "PROBE", "PERMANENT", "NOARP"}


async def dhcp_leases(gateway: Host) -> dict[str, dict]:
    """MAC -> {ip, hostname} a partir de /tmp/dhcp.leases do gateway."""
    out = await pool.run(gateway, "cat /tmp/dhcp.leases 2>/dev/null || true")
    leases: dict[str, dict] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            _, mac, ip, host = parts[0], parts[1].lower(), parts[2], parts[3]
            leases[mac] = {"ip": ip, "hostname": None if host == "*" else host}
    return leases


def band_of(radio: str) -> str:
    """Rótulo cosmético de banda a partir do nome da interface."""
    if "phy2" in radio:
        return "6G"
    if "phy1" in radio:
        return "5G"
    return "2.4G"


async def ap_stations(ap: Host) -> dict[str, dict]:
    """Station dump de TODOS os rádios do AP numa única chamada SSH.

    Junta os comandos com um delimitador e separa a saída localmente, evitando
    um round-trip SSH por SSID (relevante quando há vários SSIDs por rádio).
    """
    if not ap.radios:
        return {}
    cmd = " ; ".join(
        f'echo "@@@RADIO {r}"; iw dev {r} station dump 2>/dev/null' for r in ap.radios
    )
    out = await pool.run(ap, cmd, timeout=15)
    merged: dict[str, dict] = {}
    # re.split com grupo capturante -> [pré, radio, bloco, radio, bloco, ...]
    parts = re.split(r"@@@RADIO (\S+)", out)
    it = iter(parts[1:])
    for radio, block in zip(it, it):
        for mac, data in parse_station_dump(block).items():
            data["radio"] = radio
            data["band"] = band_of(radio)
            merged[mac] = data   # um device está em 1 iface só; sem colisão
    return merged


async def wan_device(gateway: Host) -> str:
    """Descobre o device L3 da WAN (ou usa o configurado)."""
    if gateway.wan_iface:
        return gateway.wan_iface
    try:
        out = await pool.run(gateway, "ubus call network.interface.wan status")
        return json.loads(out).get("l3_device", "eth1")
    except Exception:
        return "eth1"


async def iface_counters(host: Host, dev: str) -> tuple[int, int]:
    """(rx_bytes, tx_bytes) de /sys/class/net/<dev>/statistics."""
    out = await pool.run(
        host,
        f"cat /sys/class/net/{dev}/statistics/rx_bytes "
        f"/sys/class/net/{dev}/statistics/tx_bytes 2>/dev/null || echo 0 0",
    )
    nums = [int(x) for x in re.findall(r"\d+", out)] or [0, 0]
    return (nums[0], nums[1] if len(nums) > 1 else 0)


MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def valid_mac(mac: str) -> bool:
    return bool(MAC_RE.match((mac or "").strip().lower()))


async def hostapd_ifaces(ap: Host) -> list[str]:
    """Interfaces hostapd que o AP realmente tem, perguntando ao ubus.

    Confiar na lista `radios` do config.yml é frágil: se o SSID de IoT não
    estiver listado num dos APs, o ban não é aplicado ali e o device escapa
    exatamente para onde não deveria ir. Aqui a lista vem do próprio AP;
    o config vira só fallback.
    """
    try:
        out = await pool.run(ap, "ubus list | grep '^hostapd\\.'", timeout=8)
        ifs = [l.strip().split("hostapd.", 1)[1]
               for l in out.splitlines() if l.strip().startswith("hostapd.")]
        if ifs:
            return ifs
    except Exception as exc:
        log.warning("hostapd_ifaces %s: %s", ap.id, exc)
    return list(ap.radios)


async def steer(mac: str, target_ap_id: str) -> dict:
    """Força o MAC a migrar para target_ap_id banindo-o momentaneamente nos demais."""
    s = get_settings()
    mac = (mac or "").strip().lower()
    # o MAC entra num comando shell: só aceita o formato canônico
    if not valid_mac(mac):
        raise ValueError("MAC inválido")
    if not any(ap.id == target_ap_id for ap in s.aps):
        raise ValueError("AP de destino desconhecido")
    # Dispositivo IoT costuma rescanear devagar: 15s de ban acabam antes de
    # ele procurar outro AP, e ele volta para o mesmo. Por isso o ban de IoT
    # é bem maior — é o que faz a diferença entre a rede comum funcionar e a
    # de IoT não.
    from .steering import is_iot
    ip_conhecido = None
    try:
        from .devices import store
        ip_conhecido = store.get(mac).get("ip")
    except Exception:
        pass
    eh_iot = is_iot(ip_conhecido)
    ban = s.iot_ban_time if eh_iot else s.steer_ban_time

    results = []
    for ap in s.aps:
        if ap.id == target_ap_id:
            continue
        radios = await hostapd_ifaces(ap)
        for radio in radios:
            payload = json.dumps(
                {"addr": mac, "reason": 5, "deauth": True, "ban_time": ban}
            )
            cmd = f"ubus call hostapd.{radio} del_client '{payload}'"
            try:
                await pool.run(ap, cmd)
                results.append({"ap": ap.id, "radio": radio, "ok": True})
            except Exception as exc:
                results.append({"ap": ap.id, "radio": radio, "ok": False, "error": str(exc)})
    falhas = [r for r in results if not r["ok"]]
    return {"mac": mac, "target": target_ap_id, "ban_ms": ban,
            "iot": eh_iot, "actions": results,
            "ok": len(falhas) == 0, "radios": len(results),
            "falhas": len(falhas),
            "erro": falhas[0].get("error") if falhas else None}


# ---------------------------------------------------------------------------
# Canal e potência dos rádios
# ---------------------------------------------------------------------------

async def radio_info(ap: Host) -> list[dict]:
    """Rádios do AP com canal/potência atuais e opções disponíveis.

    Usa `ubus call network.wireless status` (já traz channel, txpower, band,
    htmode e country) e o `iw` para listar as frequências permitidas.
    Não depende do iwinfo, que pode não estar instalado.
    """
    out = await pool.run(ap, "ubus call network.wireless status", timeout=15)
    try:
        status = json.loads(out)
    except Exception as exc:
        log.warning("wireless status %s: %s", ap.id, exc)
        return []

    radios = []
    for rname, rdata in sorted(status.items()):
        ifaces = [i.get("ifname") for i in rdata.get("interfaces", []) if i.get("ifname")]
        cfg = rdata.get("config", {})
        band_raw = str(cfg.get("band", "")).lower()
        band = {"2g": "2.4G", "5g": "5G", "6g": "6G"}.get(band_raw, "")
        ch_cfg = str(cfg.get("channel", "auto"))
        # phy vem do nome da interface (phy0-ap0 -> phy0); senão, do path
        phy = ""
        if ifaces:
            m = re.match(r"(phy\d+)", ifaces[0])
            if m:
                phy = m.group(1)
        if not phy:
            m = re.search(r"\d+", rname)
            phy = f"phy{m.group()}" if m else ""
        radios.append({
            "radio": rname,
            "iface": ifaces[0] if ifaces else "",
            "ifaces": ifaces,
            "phy": phy,
            "up": bool(rdata.get("up", True)),
            "band": band,
            "htmode": cfg.get("htmode"),
            "country": cfg.get("country"),
            "auto": ch_cfg.lower() == "auto",
            "channel": None if ch_cfg.lower() == "auto" else int(ch_cfg)
                       if ch_cfg.isdigit() else None,
            "txpower": cfg.get("txpower"),
        })

    if not radios:
        return []

    # frequências permitidas por phy, numa única chamada
    parts = []
    for r in radios:
        if not r["phy"]:
            continue
        parts.append(f'echo "@@@R {r["radio"]}"')
        parts.append(f'iw phy {r["phy"]} info 2>/dev/null | grep -E "MHz \\[" || true')
    if parts:
        blob = await pool.run(ap, " ; ".join(parts), timeout=25)
        chunks = re.split(r"@@@R (\S+)", blob)
        it = iter(chunks[1:])
        per = {}
        for rname, body in zip(it, it):
            channels, maxdbm = [], 0
            for line in body.splitlines():
                low = line.lower()
                if "disabled" in low or "no ir" in low:
                    continue
                m = re.search(r"\[(\d+)\]", line)
                if not m:
                    continue
                ch = int(m.group(1))
                if ch not in channels:
                    channels.append(ch)
                mp = re.search(r"\(([\d.]+) dBm\)", line)
                if mp:
                    maxdbm = max(maxdbm, int(float(mp.group(1))))
            per[rname] = (sorted(channels), maxdbm)

        for r in radios:
            chans, maxdbm = per.get(r["radio"], ([], 0))
            r["channels"] = chans
            cap = maxdbm or int(r.get("txpower") or 20)
            ladder = [1, 3, 5, 7, 10, 12, 14, 17, 20, 23, 26, 30]
            powers = [p for p in ladder if p <= cap]
            cur = r.get("txpower")
            if cur and cur not in powers:
                powers.append(int(cur))
            r["powers"] = sorted(set(powers))
            r["max_txpower"] = cap
    else:
        for r in radios:
            r["channels"] = []
            r["powers"] = []

    # canal real quando está em auto
    if any(r["auto"] and r["iface"] for r in radios):
        parts = []
        for r in radios:
            if r["auto"] and r["iface"]:
                parts.append(f'echo "@@@C {r["radio"]}"')
                parts.append(f'iw dev {r["iface"]} info 2>/dev/null | grep -i channel || true')
        blob = await pool.run(ap, " ; ".join(parts), timeout=20)
        chunks = re.split(r"@@@C (\S+)", blob)
        it = iter(chunks[1:])
        for rname, body in zip(it, it):
            m = re.search(r"channel\s+(\d+)", body, re.I)
            if m:
                for r in radios:
                    if r["radio"] == rname:
                        r["channel"] = int(m.group(1))
    return radios


async def set_radio(ap: Host, radio: str, channel=None, txpower=None) -> dict:
    """Aplica canal e/ou potência num rádio (UCI + wifi reload).

    Atenção: o reload derruba brevemente os clientes daquele rádio.
    """
    if not re.fullmatch(r"radio\d+", radio):
        raise ValueError("rádio inválido")
    cmds = []
    if channel is not None:
        ch = str(channel).lower()
        if ch != "auto" and not re.fullmatch(r"\d+", ch):
            raise ValueError("canal inválido")
        cmds.append(f"uci set wireless.{radio}.channel='{ch}'")
    if txpower is not None:
        if not re.fullmatch(r"\d+", str(txpower)):
            raise ValueError("potência inválida")
        cmds.append(f"uci set wireless.{radio}.txpower='{txpower}'")
    if not cmds:
        raise ValueError("nada para alterar")
    cmds += ["uci commit wireless", "wifi reload"]
    await pool.run(ap, " && ".join(cmds), timeout=40)
    log.info("%s %s: canal=%s potencia=%s", ap.id, radio, channel, txpower)
    return {"ap": ap.id, "radio": radio, "channel": channel, "txpower": txpower}


async def gateway_health(gateway: Host) -> dict:
    """CPU, memória, carga, uptime e temperatura do gateway.

    Lê tudo do /proc numa única chamada SSH. O percentual de CPU é calculado
    pelo delta entre coletas (o /proc/stat traz contadores acumulados).
    """
    cmd = ("echo '@@@STAT'; head -1 /proc/stat; "
           "echo '@@@MEM'; head -3 /proc/meminfo; "
           "echo '@@@LOAD'; cat /proc/loadavg; "
           "echo '@@@UP'; cat /proc/uptime; "
           "echo '@@@TEMP'; cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | head -1; "
           # aceleração de rede (NSS/NPU): mesmo script que o LuCI usa
           "echo '@@@NPU'; [ -x /usr/libexec/npu_usage.sh ] && /usr/libexec/npu_usage.sh 2>/dev/null; "
           "echo '@@@WANIP'; ubus call network.interface.wan status 2>/dev/null "
           "| grep -o '\"address\":[^,]*' | head -1")
    out = await pool.run(gateway, cmd, timeout=15)

    def chunk(tag: str) -> str:
        m = re.search(rf"@@@{tag}\n(.*?)(?=@@@|\Z)", out, re.S)
        return m.group(1).strip() if m else ""

    health: dict = {}

    # CPU: contadores acumulados -> percentual entre amostras
    stat = chunk("STAT").split()
    if len(stat) >= 8 and stat[0] == "cpu":
        vals = [int(v) for v in stat[1:8]]
        idle = vals[3] + vals[4]              # idle + iowait
        total = sum(vals)
        prev = _prev_cpu.get(gateway.id)
        _prev_cpu[gateway.id] = (idle, total)
        if prev:
            d_idle, d_total = idle - prev[0], total - prev[1]
            if d_total > 0:
                health["cpu"] = round(100.0 * (1 - d_idle / d_total), 1)

    # memória
    mem = {}
    for line in chunk("MEM").splitlines():
        parts = line.split(":")
        if len(parts) == 2:
            m = re.search(r"(\d+)", parts[1])
            if m:
                mem[parts[0].strip()] = int(m.group(1))    # kB
    total_kb = mem.get("MemTotal")
    avail_kb = mem.get("MemAvailable", mem.get("MemFree"))
    if total_kb and avail_kb is not None:
        used = total_kb - avail_kb
        health["mem_total"] = total_kb * 1024
        health["mem_used"] = used * 1024
        health["mem"] = round(100.0 * used / total_kb, 1)

    load = chunk("LOAD").split()
    if load:
        try:
            health["load"] = float(load[0])
        except ValueError:
            pass

    up = chunk("UP").split()
    if up:
        try:
            health["uptime"] = int(float(up[0]))
        except ValueError:
            pass

    npu = chunk("NPU")
    m = re.search(r"[\d.]+", npu)
    if m:
        try:
            health["npu"] = round(float(m.group()), 1)
        except ValueError:
            pass

    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", chunk("WANIP"))
    if m:
        health["wan_ip"] = m.group(1)

    t = chunk("TEMP")
    if t.isdigit():
        val = int(t)
        health["temp"] = round(val / 1000.0, 1) if val > 1000 else float(val)

    return health


_prev_cpu: dict[str, tuple[int, int]] = {}


async def reboot_host(host: Host) -> None:
    """Reinicia um equipamento OpenWrt.

    O comando sai destacado (nohup + &) e com 1s de atraso: assim o SSH
    devolve o controle antes do aparelho cair, em vez de estourar timeout.
    """
    await pool.run(host, "(sleep 1; reboot) >/dev/null 2>&1 &", timeout=8)
