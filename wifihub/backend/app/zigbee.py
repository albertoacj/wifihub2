"""Coexistência Zigbee × Wi-Fi na faixa de 2,4 GHz.

As duas tecnologias dividem a mesma banda. Um canal Zigbee ocupa ~2 MHz; um
canal Wi-Fi 2,4 GHz com HT20 ocupa ~22 MHz e "vaza" alguns MHz além disso.
Quando os dois se sobrepõem, o Wi-Fi — muito mais forte — atropela o Zigbee,
e sensores começam a cair (foi o que já aconteceu nesta rede).

A regra deste módulo: dado o canal Zigbee que o usuário usa, descobrir quais
canais Wi-Fi 2,4 GHz podem ser usados sem invadir aquela faixa, e empurrar os
rádios de 2,4 GHz dos APs para longe.

Frequências centrais (MHz):
  Zigbee (IEEE 802.15.4): canal 11 = 2405, e +5 MHz por canal até 26 = 2480
  Wi-Fi 2,4 GHz:          canal  1 = 2412, +5 MHz por canal até 13 = 2472
"""

ZIGBEE_CENTER = {ch: 2405 + (ch - 11) * 5 for ch in range(11, 27)}   # 11..26
WIFI_CENTER = {ch: 2412 + (ch - 1) * 5 for ch in range(1, 14)}        # 1..13

# Meia-largura ocupada por cada lado, em MHz. Wi-Fi HT20 = 20 MHz de largura
# (±10), mais uma margem de guarda porque a máscara espectral não corta seco.
WIFI_HALF = 11          # ±11 MHz cobre o canal HT20 com folga
ZIGBEE_HALF = 2         # ±2 MHz cobre o canal Zigbee (~3 MHz de largura real)
GUARDA = 2              # margem extra de segurança


def wifi_conflita(wifi_ch: int, zigbee_ch: int) -> bool:
    """True se o canal Wi-Fi invade a faixa do canal Zigbee."""
    wc = WIFI_CENTER.get(wifi_ch)
    zc = ZIGBEE_CENTER.get(zigbee_ch)
    if wc is None or zc is None:
        return False
    distancia = abs(wc - zc)
    return distancia < (WIFI_HALF + ZIGBEE_HALF + GUARDA)


def canais_wifi_seguros(zigbee_ch: int) -> list[int]:
    """Canais Wi-Fi 2,4 GHz que não sobrepõem o Zigbee dado.

    Restringe aos não-sobrepostos 1/6/11 quando possível — usar canais
    intermediários (2,3,4…) espalha interferência para dois vizinhos e é má
    prática de Wi-Fi. Se nenhum dos três sobrar, cai para qualquer canal livre.
    """
    if zigbee_ch not in ZIGBEE_CENTER:
        return [1, 6, 11]
    preferidos = [c for c in (1, 6, 11) if not wifi_conflita(c, zigbee_ch)]
    if preferidos:
        return preferidos
    return [c for c in range(1, 14) if not wifi_conflita(c, zigbee_ch)]


def melhor_canal_wifi(zigbee_ch: int, atual: int | None = None) -> int:
    """Escolhe um canal seguro. Mantém o atual se já for seguro."""
    seguros = canais_wifi_seguros(zigbee_ch)
    if atual in seguros:
        return atual
    return seguros[0] if seguros else 1


def zigbee_conflita_wifi(zigbee_ch: int, wifi_ch: int) -> bool:
    """Atalho semântico para a UI (mesma relação, nome ao contrário)."""
    return wifi_conflita(wifi_ch, zigbee_ch)


def relatorio(zigbee_ch: int) -> dict:
    """Resumo pronto para a interface: seguros, conflitantes e recomendação."""
    seguros = canais_wifi_seguros(zigbee_ch)
    conflitantes = [c for c in range(1, 14) if wifi_conflita(c, zigbee_ch)]
    return {
        "zigbee": zigbee_ch,
        "zigbee_mhz": ZIGBEE_CENTER.get(zigbee_ch),
        "wifi_seguros": seguros,
        "wifi_conflitantes": conflitantes,
        "recomendado": seguros[0] if seguros else None,
    }


# ---------------------------------------------------------------------------
# Estado: o canal Zigbee escolhido pelo usuário (persistido no volume)
# ---------------------------------------------------------------------------
import os
import json
import logging

log = logging.getLogger("wifihub.zigbee")

_PATH = os.path.join(
    os.path.dirname(os.getenv("DEVICES_PATH", "/data/devices.json")),
    "zigbee.json")


def carregar() -> dict:
    """{'channel': int|None}. None = usuário não usa Zigbee (sem restrição)."""
    try:
        with open(_PATH) as fh:
            d = json.load(fh)
        ch = d.get("channel")
        return {"channel": int(ch) if ch in ZIGBEE_CENTER else None}
    except Exception:
        return {"channel": None}


def salvar(channel) -> dict:
    ch = None
    if channel not in (None, "", "off"):
        ch = int(channel)
        if ch not in ZIGBEE_CENTER:
            raise ValueError("canal Zigbee deve estar entre 11 e 26")
    tmp = _PATH + ".tmp"
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    with open(tmp, "w") as fh:
        json.dump({"channel": ch}, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, _PATH)
    log.info("canal Zigbee definido: %s", ch)
    return {"channel": ch}
