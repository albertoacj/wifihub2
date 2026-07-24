"""Presença: quando cada MAC foi visto pela última vez.

Fica fora do InfluxDB de propósito — é um valor por device que só é
sobrescrito, não uma série temporal. Vai para um JSON no volume.

A gravação é preguiçosa (a cada FLUSH_EVERY segundos): a coleta roda a cada
5s e reescrever o arquivo inteiro nesse ritmo seria desperdício de I/O.
"""
import os
import json
import time
import logging

log = logging.getLogger("wifihub.presence")

DATA_DIR = os.path.dirname(os.getenv("DEVICES_PATH", "/data/devices.json"))
PRESENCE_PATH = os.path.join(DATA_DIR, "presence.json")
FLUSH_EVERY = 60.0          # segundos entre gravações em disco


class Presence:
    def __init__(self, path: str = PRESENCE_PATH):
        self.path = path
        self._seen: dict[str, int] = {}
        self._dirty = False
        self._last_flush = 0.0
        self._load()

    def _load(self):
        try:
            with open(self.path) as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._seen = {k: int(v) for k, v in data.items()}
        except Exception:
            self._seen = {}

    def touch(self, macs):
        """Marca todos os MACs informados como vistos agora."""
        now = int(time.time())
        for mac in macs:
            if mac:
                self._seen[mac.lower()] = now
        self._dirty = True

    def last_seen(self, mac: str) -> int | None:
        return self._seen.get((mac or "").lower())

    def maybe_flush(self, force: bool = False):
        now = time.monotonic()
        if not self._dirty:
            return
        if not force and (now - self._last_flush) < FLUSH_EVERY:
            return
        self._last_flush = now
        self._dirty = False
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w") as fh:
                json.dump(self._seen, fh)
            os.replace(tmp, self.path)
        except Exception as exc:
            log.warning("falha ao gravar presença: %s", exc)


presence = Presence()
