"""Ícones enviados pelo usuário.

Guarda em /data/icons. Imagens raster são normalizadas: recorta a borda
transparente, redimensiona para caber num quadrado padrão e centraliza,
para que todos fiquem visualmente do mesmo tamanho no painel.
SVG é guardado como veio (é vetorial, escala sozinho).
"""
import io
import os
import re
import json
import uuid
import asyncio
import logging

log = logging.getLogger("wifihub.icons")

SIZE = 128          # lado do quadrado normalizado (px)
MAX_BYTES = 2_000_000
ALLOWED = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml"}


class IconStore:
    def __init__(self, base_dir: str):
        self.dir = base_dir
        self.index_path = os.path.join(base_dir, "index.json")
        self._lock = asyncio.Lock()
        os.makedirs(self.dir, exist_ok=True)
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.index_path):
            try:
                with open(self.index_path) as fh:
                    self._data = json.load(fh)
            except Exception as exc:
                log.warning("índice de ícones ilegível: %s", exc)
                self._data = {}

    async def _save(self):
        tmp = self.index_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self._data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.index_path)

    def all(self) -> list[dict]:
        return [{"id": k, **v} for k, v in sorted(
            self._data.items(), key=lambda kv: kv[1].get("name", ""))]

    def path_of(self, icon_id: str) -> tuple[str, str] | None:
        entry = self._data.get(icon_id)
        if not entry:
            return None
        p = os.path.join(self.dir, entry["file"])
        if not os.path.exists(p):
            return None
        return p, entry.get("mime", "image/png")

    async def add(self, data: bytes, filename: str, mime: str,
                  name: str = "", strip_bg: bool = True) -> dict:
        if len(data) > MAX_BYTES:
            raise ValueError("arquivo muito grande (máx. 2 MB)")
        if mime not in ALLOWED:
            raise ValueError(f"formato não suportado: {mime}")

        icon_id = uuid.uuid4().hex[:10]
        label = (name or os.path.splitext(filename)[0])[:40].strip() or "ícone"

        if mime == "image/svg+xml":
            # vetorial: guarda como veio, escala sozinho no <img>
            fname = f"{icon_id}.svg"
            with open(os.path.join(self.dir, fname), "wb") as fh:
                fh.write(data)
            out_mime = "image/svg+xml"
        else:
            png = _normalize(data, strip_bg=strip_bg)
            fname = f"{icon_id}.png"
            with open(os.path.join(self.dir, fname), "wb") as fh:
                fh.write(png)
            out_mime = "image/png"

        async with self._lock:
            self._data[icon_id] = {"name": label, "file": fname, "mime": out_mime}
            await self._save()
        log.info("ícone adicionado: %s (%s)", label, icon_id)
        return {"id": icon_id, "name": label, "mime": out_mime}

    async def rename(self, icon_id: str, name: str) -> dict:
        async with self._lock:
            entry = self._data.get(icon_id)
            if not entry:
                raise KeyError(icon_id)
            entry["name"] = name[:40].strip() or entry["name"]
            await self._save()
            return {"id": icon_id, **entry}

    async def delete(self, icon_id: str) -> dict:
        async with self._lock:
            entry = self._data.pop(icon_id, None)
            if not entry:
                raise KeyError(icon_id)
            try:
                os.remove(os.path.join(self.dir, entry["file"]))
            except OSError:
                pass
            await self._save()
        return {"id": icon_id, "deleted": True}


def _normalize(data: bytes, strip_bg: bool = True) -> bytes:
    """Recorta borda vazia, redimensiona e centraliza num quadrado SIZE×SIZE."""
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    img = img.convert("RGBA")

    if strip_bg and _is_opaque(img):
        img = _drop_bg(img)

    bbox = img.getbbox()          # descarta a borda transparente
    if bbox:
        img = img.crop(bbox)

    img.thumbnail((SIZE, SIZE), Image.LANCZOS)
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas.paste(img, ((SIZE - img.width) // 2, (SIZE - img.height) // 2), img)

    buf = io.BytesIO()
    canvas.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _is_opaque(img) -> bool:
    alpha = img.getchannel("A")
    return alpha.getextrema()[0] == 255


def _drop_bg(img, tol: int = 26):
    """Deixa transparente o fundo liso e claro (branco/quase-branco) das bordas."""
    px = img.load()
    w, h = img.size
    corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
    r = sum(c[0] for c in corners) // 4
    g = sum(c[1] for c in corners) // 4
    b = sum(c[2] for c in corners) // 4
    if min(r, g, b) < 200:        # fundo escuro/colorido: não mexe
        return img
    if max(abs(c[0] - r) + abs(c[1] - g) + abs(c[2] - b) for c in corners) > 40:
        return img                # cantos diferentes entre si: não é fundo liso

    out = img.copy()
    opx = out.load()
    for y in range(h):
        for x in range(w):
            cr, cg, cb, ca = opx[x, y]
            if abs(cr - r) <= tol and abs(cg - g) <= tol and abs(cb - b) <= tol:
                opx[x, y] = (cr, cg, cb, 0)
    return out
