"""Estado de provisionamento (modo híbrido).

A topologia editável em runtime — idioma, nome da rede, gateway, APs,
intervalos, subnets IoT, hash da senha do painel e a referência da chave
SSH — vive em ``/data/setup.json`` (volume persistente). Isso permite
configurar tudo pela página e recarregar ao vivo, sem recriar o container.

Os *segredos de encanamento* (credenciais do InfluxDB) continuam no
ambiente, gerados uma única vez pelo bootstrap. O token do DuckDNS
(usado pelo Caddy) também fica no ``.env`` — trocá-lo pede recriar só o
container do Caddy. Nada de segredo é gravado aqui em texto puro: da
senha do painel guardamos apenas o hash PBKDF2.
"""
import os
import json
import logging
import tempfile
from typing import Any

log = logging.getLogger("wifihub.provisioning")

# Mesmo diretório do devices.json (volume wifihub-data montado em /data)
DATA_DIR = os.path.dirname(os.getenv("DEVICES_PATH", "/data/devices.json"))
SETUP_PATH = os.path.join(DATA_DIR, "setup.json")

# Chave SSH gerada pelo wizard vive no volume gravável (não em keys/:ro)
SSH_DIR = os.path.join(DATA_DIR, "ssh")
SSH_KEY_PATH = os.path.join(SSH_DIR, "id_ed25519")
SSH_PUB_PATH = SSH_KEY_PATH + ".pub"

SCHEMA_VERSION = 1


def _atomic_write(path: str, data: str, mode: int = 0o600) -> None:
    """Grava de forma atômica (tmp + rename) pra nunca deixar arquivo pela metade."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".setup-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_setup() -> dict[str, Any] | None:
    """Devolve o dict de setup, ou None se ainda não houver arquivo válido."""
    try:
        with open(SETUP_PATH) as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
        log.warning("setup.json não é um objeto — ignorando")
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("setup.json ilegível (%s) — tratando como não configurado", exc)
    return None


def save_setup(data: dict[str, Any]) -> None:
    """Persiste o dict de setup (marca provisioned=True e a versão do schema)."""
    data = dict(data)
    data["provisioned"] = True
    data.setdefault("schema", SCHEMA_VERSION)
    _atomic_write(SETUP_PATH, json.dumps(data, ensure_ascii=False, indent=2))
    log.info("setup.json gravado (%d APs)", len(data.get("aps", [])))


def is_provisioned() -> bool:
    """True quando o wizard já concluiu ao menos uma vez."""
    data = load_setup()
    return bool(data and data.get("provisioned"))


def has_ssh_key() -> bool:
    return os.path.exists(SSH_KEY_PATH)


def public_key() -> str:
    """Conteúdo da chave pública gerada (string vazia se ainda não existe)."""
    try:
        with open(SSH_PUB_PATH) as fh:
            return fh.read().strip()
    except OSError:
        return ""
