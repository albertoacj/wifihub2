"""Pool de conexões SSH persistentes (asyncssh) para gateway + APs.

Mantém uma conexão viva por host e reconecta em caso de falha, evitando
handshake a cada coleta (7 hosts a cada 5s seria pesado).
"""
import asyncio
import asyncssh
import logging

from .config import get_settings, Host

log = logging.getLogger("wifihub.ssh")


class SSHPool:
    def __init__(self):
        self._conns: dict[str, asyncssh.SSHClientConnection] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def _connect(self, host: Host) -> asyncssh.SSHClientConnection:
        s = get_settings()
        # known_hosts=None: rede doméstica confiável. Para endurecer, aponte
        # para um arquivo known_hosts e remova esta linha.
        return await asyncssh.connect(
            host.host,
            port=s.ssh_port,
            username=host.user,
            client_keys=[s.ssh_key_path],
            known_hosts=None,
            connect_timeout=8,
            keepalive_interval=20,
        )

    async def conn(self, host: Host) -> asyncssh.SSHClientConnection:
        async with self._lock(host.id):
            c = self._conns.get(host.id)
            if c is not None and not c.is_closed():
                return c
            c = await self._connect(host)
            self._conns[host.id] = c
            return c

    async def run(self, host: Host, command: str, timeout: float = 12.0) -> str:
        """Executa um comando e devolve stdout. Reconecta uma vez se cair."""
        for attempt in (1, 2):
            try:
                c = await self.conn(host)
                res = await asyncio.wait_for(c.run(command, check=False), timeout)
                return res.stdout or ""
            except (asyncssh.Error, OSError, asyncio.TimeoutError) as exc:
                log.warning("SSH %s falhou (tentativa %d): %s", host.id, attempt, exc)
                # invalida conexão e tenta de novo
                old = self._conns.pop(host.id, None)
                if old is not None:
                    try:
                        old.close()
                    except Exception:
                        pass
                if attempt == 2:
                    raise
        return ""

    async def close(self):
        for c in self._conns.values():
            try:
                c.close()
            except Exception:
                pass
        self._conns.clear()


pool = SSHPool()
