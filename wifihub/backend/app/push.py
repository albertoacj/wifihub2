"""Web Push (VAPID) — notificação quando um device novo entra na rede.

Funciona na PWA instalada: desktop Linux (Chrome/Firefox) e iOS 16.4+
(obrigatório estar na tela de início; Safari em aba não recebe push).

As chaves VAPID são geradas sozinhas na primeira execução e guardadas no
volume (/data/vapid.json) — não precisa configurar nada no .env.

Arquivos de estado (volume /data):
  vapid.json       -> par de chaves do servidor
  push_subs.json   -> aparelhos que aceitaram receber notificação
  known_macs.json  -> MACs já vistos (evita alertar a casa toda no 1º boot)
"""
import os
import json
import time
import base64
import asyncio
import logging

log = logging.getLogger("wifihub.push")

DATA_DIR = os.path.dirname(os.getenv("DEVICES_PATH", "/data/devices.json"))
SUBS_PATH = os.path.join(DATA_DIR, "push_subs.json")
KNOWN_PATH = os.path.join(DATA_DIR, "known_macs.json")
VAPID_PATH = os.path.join(DATA_DIR, "vapid.json")

VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@wifihub.local")
if VAPID_SUBJECT.endswith((".local", ".lan", "example.com")):
    log.warning("VAPID_SUBJECT=%s nao serve para iPhone: a Apple valida esse "
                "campo e rejeita dominios inexistentes. Ponha um e-mail real "
                "no .env (VAPID_SUBJECT=mailto:voce@gmail.com).", VAPID_SUBJECT)

# iPhone/iPad trocam de MAC privado; se o IP já é conhecido, não é device novo
IGNORE_KNOWN_IP = os.getenv("PUSH_IGNORE_KNOWN_IP", "1") == "1"


# ------------------------------------------------------------------ storage

def _read(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def _write(path, data, mode=0o644):
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception as exc:
        log.warning("falha ao gravar %s: %s", path, exc)


# ------------------------------------------------------------ chaves VAPID

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _load_or_create_vapid() -> tuple[str, str]:
    """Devolve (public, private) em base64url. Gera na primeira vez."""
    env_pub = os.getenv("VAPID_PUBLIC_KEY", "")
    env_priv = os.getenv("VAPID_PRIVATE_KEY", "")
    if env_pub and env_priv:
        return env_pub, env_priv

    saved = _read(VAPID_PATH)
    if saved and saved.get("public") and saved.get("private"):
        return saved["public"], saved["private"]

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization

    key = ec.generate_private_key(ec.SECP256R1())
    private = _b64(key.private_numbers().private_value.to_bytes(32, "big"))
    public = _b64(key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint))
    _write(VAPID_PATH, {"public": public, "private": private,
                        "created": int(time.time())}, mode=0o600)
    log.info("push: chaves VAPID geradas em %s", VAPID_PATH)
    log.info("push: subject em uso = %s", os.getenv("VAPID_SUBJECT", "(padrao)"))
    return public, private


try:
    VAPID_PUBLIC, VAPID_PRIVATE = _load_or_create_vapid()
    enabled = True
except Exception as exc:          # pragma: no cover
    VAPID_PUBLIC = VAPID_PRIVATE = ""
    enabled = False
    log.warning("push desativado (chaves VAPID): %s", exc)


# ------------------------------------------------------------ assinaturas

class SubStore:
    """Assinaturas push, indexadas pelo endpoint (único por aparelho)."""

    def __init__(self):
        self._subs: dict[str, dict] = _read(SUBS_PATH, {}) or {}
        self._lock = asyncio.Lock()

    def all(self) -> list[dict]:
        return list(self._subs.values())

    def count(self) -> int:
        return len(self._subs)

    async def add(self, sub: dict, label: str = ""):
        ep = sub.get("endpoint")
        if not ep:
            raise ValueError("subscription sem endpoint")
        async with self._lock:
            self._subs[ep] = {"endpoint": ep, "keys": sub.get("keys", {}),
                              "label": label[:80], "added": int(time.time())}
            _write(SUBS_PATH, self._subs, mode=0o600)
        log.info("push: aparelho registrado — total %d", len(self._subs))

    async def remove(self, endpoint: str):
        async with self._lock:
            if self._subs.pop(endpoint, None) is not None:
                _write(SUBS_PATH, self._subs, mode=0o600)
                log.info("push: aparelho removido — total %d", len(self._subs))


subs = SubStore()


# ------------------------------------------------------------------ envio

def _provider(endpoint: str) -> str:
    if "apple.com" in endpoint:
        return "Apple (iPhone/iPad)"
    if "mozilla.com" in endpoint or "mozaws" in endpoint:
        return "Mozilla (Firefox)"
    if "google.com" in endpoint or "googleapis" in endpoint:
        return "Google (Chrome/Brave)"
    return endpoint.split("/")[2] if "/" in endpoint else "?"


def _send_sync(sub: dict, payload: dict) -> dict:
    """Envia um push. Devolve {ok, status, provider, error, dead}."""
    from pywebpush import webpush, WebPushException
    who = _provider(sub.get("endpoint", ""))
    try:
        webpush(
            subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]},
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE,
            vapid_claims={"sub": VAPID_SUBJECT},
            ttl=600,
            timeout=10,
        )
        return {"ok": True, "provider": who, "status": 201, "dead": False}
    except WebPushException as exc:
        code = getattr(exc.response, "status_code", 0)
        body = ""
        try:
            body = (exc.response.text or "")[:180]
        except Exception:
            pass
        dead = code in (404, 410)
        dica = ""
        if code == 403:
            razao = ""
            try:
                razao = json.loads(body).get("reason", "")
            except Exception:
                pass
            if "Expired" in razao or "Timestamp" in razao:
                dica = ("relogio do servidor fora de hora — o JWT nasce "
                        "expirado; sincronize com NTP")
            elif "Jwt" in razao or "Token" in razao:
                dica = (f"JWT recusado ({razao}) — confira VAPID_SUBJECT "
                        f"(atual: {VAPID_SUBJECT})")
            else:
                dica = f"recusado pelo provedor ({razao or 'sem motivo'})"
        elif dead:
            dica = "assinatura expirada; reative a notificacao nesse aparelho"
        log.warning("push %s falhou (%s): %s %s", who, code, body, dica)
        return {"ok": False, "provider": who, "status": code,
                "error": body or str(exc)[:180], "hint": dica, "dead": dead}
    except Exception as exc:
        log.warning("push %s erro: %s", who, exc)
        return {"ok": False, "provider": who, "status": 0,
                "error": str(exc)[:180], "hint": "", "dead": False}


async def broadcast(title: str, body: str, tag: str = "wifihub",
                    url: str = "/") -> list[dict]:
    """Dispara para todos os inscritos, limpa os mortos e devolve o relatorio."""
    if not enabled:
        return []
    targets = subs.all()
    if not targets:
        return []
    payload = {"title": title, "body": body, "tag": tag, "url": url,
               "ts": int(time.time())}
    results = await asyncio.gather(
        *[asyncio.to_thread(_send_sync, s, payload) for s in targets],
        return_exceptions=True,
    )
    report = []
    for sub, res in zip(targets, results):
        if isinstance(res, Exception):
            res = {"ok": False, "provider": _provider(sub["endpoint"]),
                   "status": 0, "error": str(res)[:180], "dead": False}
        report.append(res)
        if res.get("dead"):
            await subs.remove(sub["endpoint"])
    return report


# ------------------------------------------------------ detecção de novidade

class NewDeviceWatcher:
    """Compara os MACs de cada coleta com os já conhecidos."""

    def __init__(self):
        state = _read(KNOWN_PATH)
        self.seeded = state is not None
        state = state or {}
        self.macs: set[str] = set(state.get("macs", []))
        self.ips: set[str] = set(state.get("ips", []))

    def _persist(self):
        _write(KNOWN_PATH, {"macs": sorted(self.macs), "ips": sorted(self.ips)})

    async def check(self, devices: list[dict]):
        """devices: [{mac, ip, name, ap}] de todos os APs + cabeados."""
        new = []
        for d in devices:
            mac = (d.get("mac") or "").lower()
            if not mac or mac in self.macs:
                continue
            ip = d.get("ip")
            self.macs.add(mac)
            if ip:
                known_ip = ip in self.ips
                self.ips.add(ip)
                if IGNORE_KNOWN_IP and known_ip:
                    continue        # MAC rotativo do mesmo aparelho
            new.append(d)

        if not new:
            return
        self._persist()

        if not self.seeded:
            # primeira execução: só memoriza, não alerta a casa inteira
            self.seeded = True
            log.info("push: %d devices memorizados na primeira coleta", len(new))
            return

        for d in new:
            nome = d.get("name") or d.get("hostname") or d.get("mac")
            onde = d.get("ap") or "cabo"
            ip = d.get("ip") or "sem IP"
            log.info("push: device novo %s (%s)", nome, d.get("mac"))
            await broadcast(
                title="Novo device na rede",
                body=f"{nome} · {ip} · {onde}",
                tag=f"new-{d.get('mac')}",
            )


watcher = NewDeviceWatcher()
