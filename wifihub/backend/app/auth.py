"""Autenticação do painel.

Sessão por cookie assinado (HMAC-SHA256), sem estado no servidor:
o cookie carrega apenas a validade e é verificado com uma chave secreta
persistida em disco. Comparações de senha são em tempo constante e há
bloqueio progressivo por IP após tentativas erradas.
"""
import os
import hmac
import json
import time
import base64
import hashlib
import logging
import secrets

log = logging.getLogger("wifihub.auth")

COOKIE = "wifihub_session"
# sessão longa: o objetivo é não pedir senha toda hora no celular
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "90"))
MAX_FAILS = 6           # tentativas antes do bloqueio
LOCK_SECONDS = 300      # bloqueio inicial; dobra a cada nova rodada


class Auth:
    def __init__(self, secret_path: str):
        self.secret = _load_or_create_secret(secret_path)
        self._pwd = os.getenv("PANEL_PASSWORD", "")
        self._hash = os.getenv("PANEL_PASSWORD_HASH", "")
        self._fails: dict[str, list] = {}      # ip -> [contagem, liberado_em]

    # ---------------- estado ----------------

    @property
    def configured(self) -> bool:
        return bool(self._pwd or self._hash)

    # ---------------- senha ----------------

    def check_password(self, password: str) -> bool:
        if self._hash:
            return _verify_pbkdf2(password, self._hash)
        if self._pwd:
            return hmac.compare_digest(password.encode(), self._pwd.encode())
        return False

    # ---------------- limitação de tentativas ----------------

    def locked_for(self, ip: str) -> int:
        """Segundos restantes de bloqueio (0 = liberado)."""
        entry = self._fails.get(ip)
        if not entry:
            return 0
        return max(0, int(entry[1] - time.time()))

    def note_failure(self, ip: str):
        entry = self._fails.setdefault(ip, [0, 0.0])
        entry[0] += 1
        if entry[0] >= MAX_FAILS:
            rounds = entry[0] - MAX_FAILS + 1
            entry[1] = time.time() + LOCK_SECONDS * (2 ** (rounds - 1))
            log.warning("bloqueio por tentativas: %s (%ds)", ip,
                        int(entry[1] - time.time()))

    def note_success(self, ip: str):
        self._fails.pop(ip, None)

    # ---------------- sessão ----------------

    def issue(self) -> tuple[str, int]:
        max_age = SESSION_DAYS * 86400
        payload = {"exp": int(time.time()) + max_age, "iat": int(time.time())}
        raw = json.dumps(payload, separators=(",", ":")).encode()
        body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        sig = self._sign(body)
        return f"{body}.{sig}", max_age

    def valid(self, cookie: str | None) -> bool:
        if not cookie or "." not in cookie:
            return False
        body, _, sig = cookie.rpartition(".")
        if not hmac.compare_digest(sig, self._sign(body)):
            return False
        try:
            pad = "=" * (-len(body) % 4)
            payload = json.loads(base64.urlsafe_b64decode(body + pad))
        except Exception:
            return False
        return int(payload.get("exp", 0)) > time.time()

    def _sign(self, body: str) -> str:
        mac = hmac.new(self.secret, body.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def _load_or_create_secret(path: str) -> bytes:
    """Chave de assinatura persistida: sessões sobrevivem a reinícios."""
    try:
        if os.path.exists(path):
            with open(path, "rb") as fh:
                data = fh.read().strip()
            if len(data) >= 32:
                return data
        os.makedirs(os.path.dirname(path), exist_ok=True)
        key = secrets.token_bytes(48)
        with open(path, "wb") as fh:
            fh.write(key)
        os.chmod(path, 0o600)
        log.info("chave de sessão criada")
        return key
    except Exception as exc:
        log.warning("chave de sessão em memória (%s) — sessões caem no restart", exc)
        return secrets.token_bytes(48)


def make_hash(password: str, iterations: int = 260000) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def _verify_pbkdf2(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False
