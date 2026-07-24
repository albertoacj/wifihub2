"""Corta o acesso dos APs à internet, ligável pela interface.

Ideia de segurança: os APs (VLAN 20, 192.168.20.0/24) não precisam de saída
para a internet no dia a dia. Sem rota de saída, um AP comprometido não baixa
payload nem exfiltra nada. Você libera só quando vai atualizar pacote (apk)
ou instalar algo, e volta a bloquear depois.

Onde a regra vive: NO GATEWAY (RM1800), no firewall. É o único lugar por onde
o tráfego dos APs para a WAN passa. Bloquear de dentro do AP seria um tiro no
pé — trancaria o próprio acesso a ele.

O que a regra NÃO afeta: o acesso da sua LAN aos APs. Gerenciar um AP é
tráfego interno LAN→VLAN20, não forward VLAN20→WAN. Por isso você continua
com SSH e com o painel funcionando mesmo com a internet dos APs cortada.

A regra é um único `rule` do fw3/fw4:
    src = <zona da VLAN 20>, dest = wan, target = REJECT
Ligar/desligar = alternar o campo `enabled`, sem recriar nada.
"""
import re
import logging

from .config import get_settings
from .ssh import pool
from .firewall import _sections, _reload, _sh

log = logging.getLogger("wifihub.apwan")

RULE_NAME = "wifihub-block-ap-wan"
AP_SUBNET = "192.168.20.0/24"


async def _find_rule(secs: dict) -> str | None:
    for sid, sec in secs.items():
        if sec.get("_type") == "rule" and sec.get("name") == RULE_NAME:
            return sid
    return None


async def _ap_zone(secs: dict) -> str | None:
    """Descobre o nome da zona que contém a rede 192.168.20.x.

    Preferimos a zona (mais robusto a mudanças de sub-rede) mas caímos para o
    src_ip literal se não der para identificar a zona.
    """
    # zona cujo network inclua a interface da VLAN 20 — heurística por nome
    for sid, sec in secs.items():
        if sec.get("_type") != "zone":
            continue
        nome = sec.get("name", "")
        if nome in ("aps", "ap", "mgmt", "vlan20"):
            return nome
    return None


async def status() -> dict:
    """{'blocked': bool, 'exists': bool}."""
    secs, _ = await _sections()
    sid = await _find_rule(secs)
    if not sid:
        return {"blocked": False, "exists": False}
    enabled = secs[sid].get("enabled", "1")
    return {"blocked": enabled not in ("0", "false"), "exists": True}


async def _ensure_rule() -> str:
    """Cria a regra (desabilitada) se ainda não existir. Devolve o sid."""
    secs, _ = await _sections()
    sid = await _find_rule(secs)
    if sid:
        return sid

    sid = "wifihub_apwan"
    zona = await _ap_zone(secs)
    cmds = [
        f"uci set firewall.{sid}=rule",
        f"uci set firewall.{sid}.name={_sh(RULE_NAME)}",
        f"uci set firewall.{sid}.dest='wan'",
        f"uci set firewall.{sid}.proto='all'",
        f"uci set firewall.{sid}.target='REJECT'",
        f"uci set firewall.{sid}.enabled='0'",
    ]
    if zona:
        cmds.insert(3, f"uci set firewall.{sid}.src={_sh(zona)}")
    else:
        # sem zona identificável: bloqueia pela sub-rede, ainda na zona lan
        cmds.insert(3, f"uci set firewall.{sid}.src='lan'")
        cmds.insert(4, f"uci set firewall.{sid}.src_ip={_sh(AP_SUBNET)}")
    cmds.append("uci commit firewall")
    await pool.run(get_settings().gateway, " && ".join(cmds), timeout=20)
    log.info("regra de bloqueio AP→WAN criada (desabilitada)")
    return sid


async def set_blocked(blocked: bool) -> dict:
    """Liga (blocked=True) ou desliga o corte de internet dos APs."""
    sid = await _ensure_rule()
    val = "1" if blocked else "0"
    await pool.run(get_settings().gateway,
                   f"uci set firewall.{sid}.enabled='{val}' && "
                   f"uci commit firewall && ({_reload()})",
                   timeout=30)
    log.info("internet dos APs %s", "BLOQUEADA" if blocked else "liberada")
    return {"blocked": blocked, "exists": True}
