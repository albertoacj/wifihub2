"""Metadados de dispositivos: nome, ícone, tipo de link e AP preferido, por MAC.

Metadados NÃO vão para o InfluxDB de propósito: renomear um device criaria
séries novas (churn) num banco de série temporal. Aqui fica um JSON pequeno,
persistido em volume, e o front junta nome/ícone às métricas na hora de exibir.

Durabilidade: gravar com os.replace não basta. Sem fsync, uma queda de energia
pode registrar o rename antes dos dados chegarem ao disco — o arquivo volta
vazio e todos os nomes somem. Aqui há fsync do arquivo e do diretório, uma
cópia .bak da versão anterior e um espelho legível em devices.txt.
"""
import json
import asyncio
import os
import time
import logging

log = logging.getLogger("wifihub.devices")

# chaves de ícone que o front sabe desenhar
ICONS = [
    "generic", "tv", "settop", "ac", "thermo", "solar", "ev_charger", "car",
    "computer", "phone_iphone", "ipad", "macbook", "laptop", "speaker",
    "energy_meter", "water_meter", "boiler", "rainbird", "smart_home",
    "zigbee", "router", "ap", "console", "camera", "printer", "server",
]


class DeviceStore:
    def __init__(self, path: str):
        self.path = path
        self.bak = path + ".bak"
        self.txt = os.path.join(os.path.dirname(path), "devices.txt")
        self._lock = asyncio.Lock()
        self._data: dict[str, dict] = {}
        self._load()
        self._migra_link()

    # ------------------------------------------------------------ leitura

    def _read(self, path: str) -> dict | None:
        if not os.path.exists(path):
            return None
        try:
            with open(path) as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            log.warning("arquivo ilegível %s: %s", path, exc)
            return None

    def _load(self):
        data = self._read(self.path)
        if data:
            self._data = data
            return
        # principal vazio ou corrompido: tenta o backup antes de desistir
        data = self._read(self.bak)
        if data:
            self._data = data
            log.warning("devices.json estava ilegível — %d devices "
                        "recuperados do backup", len(data))
            return
        if os.path.exists(self.path):
            log.error("devices.json e o backup estão ilegíveis; começando "
                      "vazio. Confira %s", self.txt)
        self._data = {}

    def _migra_link(self):
        """Descarta rótulos wifi/cabo aprendidos pela versão anterior.

        A primeira versão marcava como "cabo" qualquer MAC que aparecesse na
        lista de cabeados, inclusive um Wi-Fi desligado que só deixou rastro
        ARP em STALE. O rótulo errado ficava gravado para sempre e o device
        nunca mais era tratado como Wi-Fi.

        Nomes, ícones, IPs e AP preferido não são tocados — só o rótulo, que
        é reaprendido em segundos: Wi-Fi na primeira coleta em que o device
        aparecer num AP, cabo na primeira vez que for confirmado presente.
        """
        marca = os.path.join(os.path.dirname(self.path) or ".", ".link_v2")
        if os.path.exists(marca):
            return
        n = 0
        for e in self._data.values():
            if e.pop("link", None) is not None:
                n += 1
        try:
            if n:
                self._save_sync()
                log.info("migração: %d rótulos wifi/cabo descartados para "
                         "serem reaprendidos", n)
            with open(marca, "w") as fh:
                fh.write("ok\n")
        except Exception as exc:
            log.warning("falha na migração de rótulos: %s", exc)

    # ------------------------------------------------------------ escrita

    def _save_sync(self):
        """Gravação durável: fsync do conteúdo antes do rename e do diretório
        depois, senão uma queda de energia pode deixar o arquivo vazio."""
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)

        # guarda a versão anterior antes de sobrescrever
        if os.path.exists(self.path):
            try:
                with open(self.path, "rb") as src, open(self.bak + ".tmp", "wb") as dst:
                    dst.write(src.read())
                    dst.flush()
                    os.fsync(dst.fileno())
                os.replace(self.bak + ".tmp", self.bak)
            except Exception as exc:
                log.warning("falha ao gerar backup: %s", exc)

        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self._data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

        try:
            fd = os.open(d, os.O_DIRECTORY)
            os.fsync(fd)
            os.close(fd)
        except Exception:
            pass          # nem todo sistema de arquivos permite

        self._write_txt()

    def _write_txt(self):
        """Espelho legível: dá para abrir com cat e reconstruir na mão."""
        try:
            linhas = ["# WifiHub — nomes dos dispositivos",
                      "# gerado em " + time.strftime("%Y-%m-%d %H:%M:%S"),
                      "# MAC | nome | ícone | link | AP preferido | IP", ""]
            for mac in sorted(self._data):
                e = self._data[mac]
                linhas.append(" | ".join([
                    mac,
                    e.get("name", ""),
                    e.get("icon", ""),
                    e.get("link", ""),
                    e.get("pref_ap", ""),
                    e.get("ip", ""),
                ]))
            tmp = self.txt + ".tmp"
            with open(tmp, "w") as fh:
                fh.write("\n".join(linhas) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.txt)
        except Exception as exc:
            log.warning("falha ao gravar devices.txt: %s", exc)

    async def _save(self):
        await asyncio.to_thread(self._save_sync)

    # ------------------------------------------------------------ consulta

    def all(self) -> dict[str, dict]:
        return self._data

    def get(self, mac: str, ip: str | None = None) -> dict:
        """Metadados do device. Procura pelo MAC; se não achar (aparelhos com
        MAC aleatório rotativo, como iPhone/iPad), cai para o IP, que é estável
        graças às reservas de DHCP."""
        entry = self._data.get((mac or "").lower())
        if entry:
            return entry
        if ip:
            for e in self._data.values():
                if e.get("ip") == ip and (e.get("name") or e.get("icon")):
                    return e
        return {}

    # ------------------------------------------------------------ escrita

    async def upsert(self, mac: str, name: str | None = None,
                     icon: str | None = None, ip: str | None = None,
                     link: str | None = None,
                     pref_ap: str | None = None) -> dict:
        mac = mac.lower()
        async with self._lock:
            entry = self._data.get(mac, {})
            if name is not None:
                entry["name"] = name.strip()
            if icon is not None:
                # "custom:<id>" referencia um ícone enviado pelo usuário
                if icon.startswith("custom:") or icon in ICONS:
                    entry["icon"] = icon
                else:
                    entry["icon"] = "generic"
            if ip:
                entry["ip"] = ip      # âncora estável p/ MAC rotativo
            if link in ("wifi", "cabo"):
                entry["link"] = link
            if pref_ap is not None:
                if pref_ap:
                    entry["pref_ap"] = pref_ap
                else:
                    entry.pop("pref_ap", None)   # string vazia = soltar
            self._data[mac] = entry
            await self._save()
            return entry

    async def note_link(self, vistos: dict[str, str]):
        """Registra como cada MAC apareceu (wifi/cabo). Só grava em disco
        quando algo realmente muda — isto roda a cada coleta."""
        mudou = False
        for mac, tipo in vistos.items():
            mac = (mac or "").lower()
            if not mac or tipo not in ("wifi", "cabo"):
                continue
            entry = self._data.setdefault(mac, {})
            if entry.get("link") != tipo:
                entry["link"] = tipo
                mudou = True
        if mudou:
            async with self._lock:
                await self._save()


store = DeviceStore(os.environ.get("DEVICES_PATH", "/data/devices.json"))
