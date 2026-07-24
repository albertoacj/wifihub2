"""Onboarding SSH do wizard (estilo ssh-copy-id).

Fluxo pensado para quem nunca configurou: o wizard GERA um par de chaves,
o usuário informa a SENHA do root de cada dispositivo, e o wizard usa essa
senha para COPIAR a chave pública no aparelho — depois reconecta com a chave
para confirmar que ficou sem senha.

OpenWrt usa Dropbear por padrão (authorized_keys em /etc/dropbear/), com
fallback para ~/.ssh/authorized_keys (OpenSSH). A senha é usada só na hora,
nunca é gravada — o que persiste é apenas a chave privada em /data/ssh.
"""
import os
import asyncio
import logging
import asyncssh

from . import provisioning

log = logging.getLogger("wifihub.wizard")

_CONNECT_TIMEOUT = 12


def gen_keypair() -> str:
    """Gera o par ed25519 em /data/ssh (se ainda não existir) e devolve a
    chave pública. Idempotente: se já houver chave, só devolve a pública."""
    if provisioning.has_ssh_key():
        return provisioning.public_key()

    os.makedirs(provisioning.SSH_DIR, exist_ok=True)
    key = asyncssh.generate_private_key("ssh-ed25519", comment="wifihub")
    priv = key.export_private_key()          # OpenSSH PEM
    pub = key.export_public_key()            # "ssh-ed25519 AAAA... wifihub"

    with open(provisioning.SSH_KEY_PATH, "wb") as fh:
        fh.write(priv)
    os.chmod(provisioning.SSH_KEY_PATH, 0o600)
    with open(provisioning.SSH_PUB_PATH, "wb") as fh:
        fh.write(pub if pub.endswith(b"\n") else pub + b"\n")
    os.chmod(provisioning.SSH_PUB_PATH, 0o644)
    log.info("par de chaves SSH gerado em %s", provisioning.SSH_KEY_PATH)
    return provisioning.public_key()


def _install_cmd(pubkey: str) -> str:
    """Comando shell idempotente que instala a chave no Dropbear e no OpenSSH."""
    # pubkey é a nossa própria chave gerada (sem injeção de terceiros)
    return (
        'K=' + _shq(pubkey) + '; '
        'mkdir -p /etc/dropbear 2>/dev/null; '
        'touch /etc/dropbear/authorized_keys 2>/dev/null; '
        'grep -qF "$K" /etc/dropbear/authorized_keys 2>/dev/null || '
        'echo "$K" >> /etc/dropbear/authorized_keys; '
        'chmod 600 /etc/dropbear/authorized_keys 2>/dev/null; '
        'mkdir -p "$HOME/.ssh" 2>/dev/null; '
        'touch "$HOME/.ssh/authorized_keys" 2>/dev/null; '
        'grep -qF "$K" "$HOME/.ssh/authorized_keys" 2>/dev/null || '
        'echo "$K" >> "$HOME/.ssh/authorized_keys"; '
        'chmod 700 "$HOME/.ssh" 2>/dev/null; '
        'chmod 600 "$HOME/.ssh/authorized_keys" 2>/dev/null; '
        'echo WIFIHUB_INSTALLED'
    )


def _shq(s: str) -> str:
    """Aspas simples seguras para shell."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


async def install_key(host: str, user: str, password: str, port: int = 22) -> None:
    """Conecta com senha e instala a chave pública. Levanta em caso de falha."""
    pub = provisioning.public_key()
    if not pub:
        raise RuntimeError("chave pública ainda não gerada")
    async with asyncssh.connect(
        host, port=port, username=user, password=password,
        known_hosts=None, connect_timeout=_CONNECT_TIMEOUT,
    ) as conn:
        res = await asyncio.wait_for(
            conn.run(_install_cmd(pub), check=False), timeout=20)
        if "WIFIHUB_INSTALLED" not in (res.stdout or ""):
            raise RuntimeError(
                "não foi possível gravar a chave no aparelho "
                f"(saída: {(res.stderr or res.stdout or '').strip()[:200]})")


async def verify_key(host: str, user: str, port: int = 22) -> bool:
    """Confirma que já dá para entrar SÓ com a chave (sem senha)."""
    async with asyncssh.connect(
        host, port=port, username=user,
        client_keys=[provisioning.SSH_KEY_PATH],
        known_hosts=None, connect_timeout=_CONNECT_TIMEOUT,
    ) as conn:
        res = await asyncio.wait_for(conn.run("echo wifihub-ok", check=False), 15)
        return "wifihub-ok" in (res.stdout or "")


async def test_and_install(host: str, user: str, password: str,
                           port: int = 22) -> dict:
    """Instala a chave via senha e verifica. Devolve {ok, detail}."""
    try:
        # Se a chave já funciona, nem precisa da senha.
        try:
            if await verify_key(host, user, port):
                return {"ok": True, "detail": "chave já ativa"}
        except Exception:
            pass  # ainda não tem a chave — segue para instalar

        await install_key(host, user, password, port)
        if await verify_key(host, user, port):
            return {"ok": True, "detail": "chave instalada"}
        return {"ok": False, "detail": "chave instalada, mas a verificação falhou"}
    except asyncssh.PermissionDenied:
        return {"ok": False, "detail": "senha incorreta"}
    except (asyncssh.Error, OSError, asyncio.TimeoutError) as exc:
        return {"ok": False, "detail": f"não conectou: {exc}"}
