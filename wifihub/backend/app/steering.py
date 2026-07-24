"""Mantém cada device no AP escolhido como preferido.

A cada coleta, quem estiver fora do AP preferido leva um steer (deauth curto
+ ban momentâneo nos demais APs, a mesma coisa que o botão "Mover" faz).

Os freios existem porque insistir cegamente é pior que não fazer nada:

- **cooldown**: no máximo um steer por MAC a cada COOLDOWN segundos. Sem isso
  um device seria deauthenticado a cada 5 segundos.
- **desistência**: depois de MAX_TENTATIVAS sem sucesso o device fica em paz
  por BACKOFF segundos. Se ele não consegue se associar ao AP preferido —
  longe demais, banda incompatível — insistir só o mantém fora da rede.
- **AP preferido precisa estar online**: mandar um device para um AP que caiu
  o tiraria da rede inteira.
"""
import time
import logging

from .devices import store
from .config import get_settings

log = logging.getLogger("wifihub.steering")

COOLDOWN = 60.0          # segundos entre tentativas para o mesmo MAC
MAX_TENTATIVAS = 3       # depois disso, desiste por um tempo
BACKOFF = 900.0          # 15 min de trégua após desistir
PAUSA_MANUAL = 600.0     # 10 min sem interferir após um "Mover" feito à mão


class PrefEnforcer:
    def __init__(self):
        self._st: dict[str, dict] = {}

    def status(self, mac: str) -> dict:
        return self._st.get((mac or "").lower(), {})

    def pausar(self, mac: str, segundos: float = PAUSA_MANUAL):
        """Suspende o enforcement para um MAC.

        Chamado quando você move um device pela mão. Sem isto o enforcement
        veria a divergência no ciclo seguinte (5s depois) e puxaria o device
        de volta — na prática, o botão "Mover" pararia de funcionar para
        qualquer device fixado. O ajuste manual vence por um tempo; se a
        mudança for para valer, use "Fixar sempre neste AP".
        """
        mac = (mac or "").lower()
        if not mac:
            return
        st = self._st.setdefault(mac, {"tentativas": 0, "proxima": 0.0})
        st["proxima"] = time.time() + segundos
        st["tentativas"] = 0
        st["manual"] = True
        log.info("steering: %s movido à mão; enforcement pausado por %d min",
                 mac, int(segundos / 60))

    async def enforce(self, aps_out: list[dict]) -> list[dict]:
        from . import wifi          # import tardio: evita ciclo

        online = {a["id"] for a in aps_out if a.get("online")}
        agora = time.time()
        acoes = []

        for ap in aps_out:
            if not ap.get("online"):
                continue
            for c in ap.get("clients", []):
                mac = (c.get("mac") or "").lower()
                if not mac:
                    continue
                pref = store.get(mac, c.get("ip")).get("pref_ap")

                if not pref or pref == ap["id"]:
                    self._st.pop(mac, None)      # está onde deveria
                    continue
                if pref not in online:
                    continue                     # destino fora do ar

                st = self._st.setdefault(mac, {"tentativas": 0, "proxima": 0.0})
                if agora < st["proxima"]:
                    continue

                st["tentativas"] += 1
                if st["tentativas"] > MAX_TENTATIVAS:
                    st["tentativas"] = 0
                    st["proxima"] = agora + BACKOFF
                    log.info("steering: %s não fica em %s; trégua de %d min",
                             mac, pref, int(BACKOFF / 60))
                    continue

                st["proxima"] = agora + COOLDOWN
                try:
                    await wifi.steer(mac, pref)
                    log.info("steering: %s de %s para %s (tentativa %d)",
                             mac, ap["id"], pref, st["tentativas"])
                    acoes.append({"mac": mac, "de": ap["id"], "para": pref,
                                  "tentativa": st["tentativas"]})
                except Exception as exc:
                    log.warning("steering %s: %s", mac, exc)

        return acoes


enforcer = PrefEnforcer()


# ---------------------------------------------------------------------------
# IoT: fixar em lote
# ---------------------------------------------------------------------------

def is_iot(ip: str | None) -> bool:
    """IoT é reconhecido pelo prefixo de IP da VLAN, não pelo rádio.

    O rádio diria o SSID, mas só serve para Wi-Fi. O prefixo de IP vale
    também para o que estiver na VLAN por cabo, e é o mesmo critério que
    você usa mentalmente para dizer "isso é IoT".
    """
    if not ip:
        return False
    return any(ip.startswith(pref) for pref in get_settings().iot_subnets)


async def pin_iot(aps_out: list[dict]) -> dict:
    """Fixa cada IoT online no AP em que está agora.

    É uma foto do momento, de propósito. Fixar automaticamente "onde apareceu"
    seria perigoso logo depois de uma queda de energia: cimentaria justamente
    o espalhamento ruim. Assim você arruma a rede primeiro e só então congela.
    """
    fixados, ja_ok = [], 0
    for ap in aps_out:
        if not ap.get("online"):
            continue
        for c in ap.get("clients", []):
            if not is_iot(c.get("ip")):
                continue
            mac = (c.get("mac") or "").lower()
            atual = store.get(mac, c.get("ip")).get("pref_ap")
            if atual == ap["id"]:
                ja_ok += 1
                continue
            await store.upsert(mac, ip=c.get("ip"), pref_ap=ap["id"])
            fixados.append({"mac": mac, "nome": c.get("name"), "ap": ap["id"]})
    return {"fixados": fixados, "ja_estavam": ja_ok, "total": len(fixados) + ja_ok}


async def unpin_iot() -> int:
    """Solta todos os IoT."""
    n = 0
    for mac, e in list(store.all().items()):
        if e.get("pref_ap") and is_iot(e.get("ip")):
            await store.upsert(mac, pref_ap="")
            n += 1
    return n


async def auto_pin_novos(aps_out: list[dict]):
    """Fixa IoT ainda sem preferência no AP onde apareceu (config iot.auto_pin)."""
    if not get_settings().iot_auto_pin:
        return
    for ap in aps_out:
        if not ap.get("online"):
            continue
        for c in ap.get("clients", []):
            if not is_iot(c.get("ip")):
                continue
            mac = (c.get("mac") or "").lower()
            if store.get(mac, c.get("ip")).get("pref_ap"):
                continue
            await store.upsert(mac, ip=c.get("ip"), pref_ap=ap["id"])
            log.info("auto-pin: %s fixado em %s", mac, ap["id"])
