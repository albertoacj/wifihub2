"""Regras de redirecionamento (port forward) no gateway, via UCI/fw4.

Lista TODAS as regras DNAT (inclusive as criadas fora do painel, no LuCI),
usando o identificador interno de seção (`uci -X show`) como handle estável
para editar/remover qualquer uma. Regras criadas aqui recebem nome `panel_<id>`.
"""
import re
import uuid
import logging

from .config import get_settings
from .ssh import pool

log = logging.getLogger("wifihub.firewall")

_PREFIX = "panel_"


def _sh(v: str) -> str:
    return "'" + str(v).replace("'", "'\\''") + "'"


async def _sections() -> tuple[dict, list]:
    """Lê todas as seções do firewall: {id: {campos}}, na ordem original."""
    s = get_settings()
    out = await pool.run(s.gateway, "uci -X show firewall 2>/dev/null || true")
    secs: dict[str, dict] = {}
    order: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"firewall\.([^.=]+)=(\w+)", line)
        if m:
            sid, typ = m.group(1), m.group(2)
            if sid not in secs:
                secs[sid] = {"_type": typ}
                order.append(sid)
            continue
        m = re.match(r"firewall\.([^.=]+)\.(\w+)=(.*)", line)
        if m:
            sid, key, val = m.group(1), m.group(2), m.group(3).strip("'")
            secs.setdefault(sid, {})
            secs[sid][key] = val
    return secs, order


async def list_rules() -> list[dict]:
    """Regras de tráfego (seções `rule`): permitem/bloqueiam entre zonas.

    Não são redirecionamentos de porta, mas também abrem caminho na rede —
    por isso aparecem no painel, em lista separada.
    """
    secs, order = await _sections()
    rules = []
    for sid in order:
        r = secs[sid]
        if r.get("_type") != "rule":
            continue
        if r.get("enabled") == "0":
            enabled = False
        else:
            enabled = True
        rules.append({
            "id": sid,
            "name": r.get("name", sid),
            "src": r.get("src", "*"),
            "dest": r.get("dest", "*"),
            "src_ip": r.get("src_ip"),
            "dest_ip": r.get("dest_ip"),
            "proto": r.get("proto", "all"),
            "dest_port": r.get("dest_port"),
            "src_port": r.get("src_port"),
            "target": r.get("target", "ACCEPT"),
            "enabled": enabled,
        })
    return rules


async def list_redirects() -> list[dict]:
    """Redirecionamentos de porta (seções `redirect` com alvo DNAT)."""
    secs, order = await _sections()
    rules = []
    for sid in order:
        r = secs[sid]
        if r.get("_type") != "redirect" or r.get("target", "DNAT") != "DNAT":
            continue
        rules.append({
            "id": sid,
            "name": r.get("name", ""),
            "proto": r.get("proto", "tcp"),
            "src_dport": r.get("src_dport", ""),
            "dest_ip": r.get("dest_ip", ""),
            "dest_port": r.get("dest_port", r.get("src_dport", "")),
            "enabled": r.get("enabled", "1") != "0",
            "managed": sid.startswith(_PREFIX),
        })
    return rules


def _reload() -> str:
    return "fw4 reload 2>/dev/null || /etc/init.d/firewall reload"


async def add_redirect(proto: str, src_dport: str, dest_ip: str,
                       dest_port: str, name: str = "") -> dict:
    s = get_settings()
    proto = proto.lower()
    if proto not in ("tcp", "udp", "tcp udp"):
        raise ValueError("proto deve ser tcp, udp ou 'tcp udp'")
    sid = f"{_PREFIX}{uuid.uuid4().hex[:8]}"
    label = name or f"{proto}-{src_dport}->{dest_ip}:{dest_port}"
    cmds = [
        f"uci set firewall.{sid}=redirect",
        f"uci set firewall.{sid}.name={_sh(label)}",
        f"uci set firewall.{sid}.target='DNAT'",
        f"uci set firewall.{sid}.src='wan'",
        f"uci set firewall.{sid}.dest='lan'",
        f"uci set firewall.{sid}.proto={_sh(proto)}",
        f"uci set firewall.{sid}.src_dport={_sh(src_dport)}",
        f"uci set firewall.{sid}.dest_ip={_sh(dest_ip)}",
        f"uci set firewall.{sid}.dest_port={_sh(dest_port)}",
        "uci commit firewall",
        _reload(),
    ]
    await pool.run(s.gateway, " && ".join(cmds), timeout=25)
    log.info("redirect criado: %s", label)
    return {"id": sid, "name": label, "proto": proto,
            "src_dport": src_dport, "dest_ip": dest_ip, "dest_port": dest_port}


async def update_redirect(sid: str, proto: str, src_dport: str,
                          dest_ip: str, dest_port: str, name: str = "") -> dict:
    s = get_settings()
    if not re.fullmatch(r"[\w-]+", sid):
        raise ValueError("id inválido")
    proto = proto.lower()
    if proto not in ("tcp", "udp", "tcp udp"):
        raise ValueError("proto deve ser tcp, udp ou 'tcp udp'")
    cmds = [
        f"uci set firewall.{sid}.proto={_sh(proto)}",
        f"uci set firewall.{sid}.src_dport={_sh(src_dport)}",
        f"uci set firewall.{sid}.dest_ip={_sh(dest_ip)}",
        f"uci set firewall.{sid}.dest_port={_sh(dest_port)}",
    ]
    if name:
        cmds.append(f"uci set firewall.{sid}.name={_sh(name)}")
    cmds += ["uci commit firewall", _reload()]
    await pool.run(s.gateway, " && ".join(cmds), timeout=25)
    log.info("redirect %s atualizado", sid)
    return {"id": sid, "proto": proto, "src_dport": src_dport,
            "dest_ip": dest_ip, "dest_port": dest_port}


async def delete_redirect(sid: str) -> dict:
    s = get_settings()
    if not re.fullmatch(r"[\w-]+", sid):
        raise ValueError("id inválido")
    cmds = [f"uci -q delete firewall.{sid}", "uci commit firewall", _reload()]
    await pool.run(s.gateway, " && ".join(cmds), timeout=25)
    return {"id": sid, "deleted": True}


# ---------------------------------------------------------------------------
# Zonas, encaminhamentos e regras entre VLANs
# ---------------------------------------------------------------------------

_TARGETS = {"ACCEPT", "REJECT", "DROP"}


async def list_zones() -> list[dict]:
    """Zonas do firewall (cada uma corresponde a uma VLAN/rede)."""
    secs, order = await _sections()
    zones = []
    for sid in order:
        z = secs[sid]
        if z.get("_type") != "zone":
            continue
        nets = z.get("network", "")
        zones.append({
            "id": sid,
            "name": z.get("name", sid),
            "networks": [n for n in nets.split() if n] if nets else [],
            "input": z.get("input", "REJECT"),
            "output": z.get("output", "ACCEPT"),
            "forward": z.get("forward", "REJECT"),
        })
    return zones


async def list_forwardings() -> list[dict]:
    """Encaminhamentos zona→zona (libera a VLAN inteira para a outra)."""
    secs, order = await _sections()
    fwds = []
    for sid in order:
        f = secs[sid]
        if f.get("_type") != "forwarding":
            continue
        fwds.append({"id": sid, "src": f.get("src", "?"), "dest": f.get("dest", "?")})
    return fwds


async def add_rule(name: str, src: str, dest: str, target: str = "ACCEPT",
                   src_ip: str = "", dest_ip: str = "", proto: str = "",
                   dest_port: str = "") -> dict:
    """Cria uma regra entre zonas/VLANs. Seções nascem com prefixo panel_."""
    s = get_settings()
    target = (target or "ACCEPT").upper()
    if target not in _TARGETS:
        raise ValueError("alvo deve ser ACCEPT, REJECT ou DROP")
    if not src or not dest:
        raise ValueError("origem e destino são obrigatórios")
    for label, val in (("IP de origem", src_ip), ("IP de destino", dest_ip)):
        if val and not re.fullmatch(r"[\d./]+", val):
            raise ValueError(f"{label} inválido")
    if dest_port and not re.fullmatch(r"[\d,\-]+", dest_port):
        raise ValueError("porta de destino inválida")

    sid = f"{_PREFIX}{uuid.uuid4().hex[:8]}"
    label = name.strip() or f"{src}->{dest}"
    cmds = [
        f"uci set firewall.{sid}=rule",
        f"uci set firewall.{sid}.name={_sh(label)}",
        f"uci set firewall.{sid}.src={_sh(src)}",
        f"uci set firewall.{sid}.dest={_sh(dest)}",
        f"uci set firewall.{sid}.target={_sh(target)}",
        f"uci set firewall.{sid}.family='ipv4'",
    ]
    if src_ip:
        cmds.append(f"uci set firewall.{sid}.src_ip={_sh(src_ip)}")
    if dest_ip:
        cmds.append(f"uci set firewall.{sid}.dest_ip={_sh(dest_ip)}")
    if proto and proto != "all":
        cmds.append(f"uci set firewall.{sid}.proto={_sh(proto)}")
    if dest_port:
        cmds.append(f"uci set firewall.{sid}.dest_port={_sh(dest_port)}")
    cmds += ["uci commit firewall", _reload()]

    await pool.run(s.gateway, " && ".join(cmds), timeout=40)
    log.info("regra criada: %s (%s -> %s / %s)", label, src, dest, target)
    return {"id": sid, "name": label, "src": src, "dest": dest, "target": target}


async def delete_rule(sid: str) -> dict:
    s = get_settings()
    if not re.fullmatch(r"[\w-]+", sid):
        raise ValueError("id inválido")
    cmds = [f"uci -q delete firewall.{sid}", "uci commit firewall", _reload()]
    await pool.run(s.gateway, " && ".join(cmds), timeout=40)
    log.info("regra removida: %s", sid)
    return {"id": sid, "deleted": True}


async def toggle_rule(sid: str, enabled: bool) -> dict:
    s = get_settings()
    if not re.fullmatch(r"[\w-]+", sid):
        raise ValueError("id inválido")
    val = "1" if enabled else "0"
    cmds = [f"uci set firewall.{sid}.enabled='{val}'", "uci commit firewall", _reload()]
    await pool.run(s.gateway, " && ".join(cmds), timeout=40)
    return {"id": sid, "enabled": enabled}
