import asyncio
import logging
from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request, Response
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               HTMLResponse)
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .collector import snapshot, run_collector
from .devices import store, ICONS
from .icons import IconStore
from .auth import Auth, COOKIE
from .influx import get_influx
from .ssh import pool
from . import wifi, firewall, push, provisioning, wizard
from .config import reload_settings
from .models import (DeviceUpdate, SteerRequest, RedirectCreate, RadioUpdate,
                     IconRename, RuleCreate, RuleToggle)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wifihub")

STATIC_DIR = Path(__file__).parent.parent / "static"
ICON_DIR = os.path.join(
    os.path.dirname(os.getenv("DEVICES_PATH", "/data/devices.json")), "icons")
icon_store = IconStore(ICON_DIR)
DATA_DIR = os.path.dirname(os.getenv("DEVICES_PATH", "/data/devices.json"))
auth = Auth(os.path.join(DATA_DIR, "session.key"))


_bg: dict = {"task": None, "running": False}


async def start_services():
    """Inicia Influx + coletor. Idempotente — chamado no boot (se já
    provisionado) ou logo após o wizard concluir, sem reiniciar o container."""
    if _bg["running"]:
        return
    reload_settings()
    await asyncio.to_thread(get_influx().ensure_downsampling)
    _bg["task"] = asyncio.create_task(run_collector())
    _bg["running"] = True
    log.info("serviços iniciados — coletor ativo")


async def stop_services():
    task = _bg.get("task")
    if task:
        task.cancel()
        _bg["task"] = None
    if _bg["running"]:
        await pool.close()
        try:
            get_influx().close()
        except Exception:
            pass
    _bg["running"] = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    if provisioning.is_provisioned():
        await start_services()
    else:
        log.warning("MODO SETUP: painel ainda não configurado — "
                    "coletor parado, servindo o assistente em /setup")
    yield
    await stop_services()


app = FastAPI(title="Rede Casa Cabral", lifespan=lifespan)

# caminhos acessíveis sem sessão
_OPEN = {"/login", "/api/login", "/manifest.webmanifest", "/sw.js", "/favicon.ico"}


def _harden(resp: Response) -> Response:
    """Cabeçalhos de segurança — aplicados também às respostas de bloqueio."""
    resp.headers["X-Content-Type-Options"] = "nosniff"
    # SAMEORIGIN (não DENY): o HA embute o add-on num iframe via Ingress.
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers.setdefault("Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline' https://esm.sh; "
        # frame-ancestors 'self' permite o iframe do HA; base-uri 'self' permite
        # o <base href> que injetamos para o caminho do Ingress.
        "connect-src 'self' https://esm.sh; frame-ancestors 'self'; base-uri 'self'")
    return resp


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    open_path = (path in _OPEN or path.startswith("/static/"))
    setup_path = (path == "/setup" or path.startswith("/api/setup"))

    # Antes de provisionar: tudo é direcionado ao assistente de setup.
    if not provisioning.is_provisioned():
        if setup_path or open_path:
            return _harden(await call_next(request))
        if path.startswith("/api/"):
            return _harden(JSONResponse(
                {"detail": "não configurado", "setup": True}, status_code=503))
        return _harden(RedirectResponse(
            _ingress_base(request) + "/setup", status_code=302))

    # Já provisionado: a página de setup vira a aba Config (exige sessão).
    if not open_path and auth.configured and not auth.valid(request.cookies.get(COOKIE)):
        if path.startswith("/api/"):
            return _harden(JSONResponse({"detail": "não autenticado"}, status_code=401))
        return _harden(RedirectResponse(
            _ingress_base(request) + "/login", status_code=302))

    return _harden(await call_next(request))


@app.get("/login")
async def login_page():
    if not auth.configured:
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/api/login")
async def api_login(request: Request):
    ip = request.client.host if request.client else "?"
    wait = auth.locked_for(ip)
    if wait:
        raise HTTPException(429, f"muitas tentativas — tente em {wait}s")
    body = await request.json()
    if not auth.check_password(str(body.get("password", ""))):
        auth.note_failure(ip)
        raise HTTPException(401, "senha incorreta")
    auth.note_success(ip)
    value, max_age = auth.issue()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, value, max_age=max_age, httponly=True,
                    samesite="lax", secure=True, path="/")
    return resp


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/api/state")
async def api_state():
    return snapshot


@app.get("/api/meta")
async def api_meta():
    s = get_settings()
    return {
        "icons": ICONS,
        "custom_icons": icon_store.all(),
        "aps": [{"id": a.id, "name": a.name} for a in s.aps],
        "gateway": ({"id": s.gateway.id, "name": s.gateway.name}
                    if s.gateway else None),
        "network_name": s.network_name,
        "language": s.language,
        "poll_interval": s.poll_interval,
        "link_down_mbps": s.link_down_mbps,
        "link_up_mbps": s.link_up_mbps,
    }


@app.get("/api/history")
async def api_history(entity: str, id: str = "", metric: str = "tx_rate",
                      window: str = "-1h", every: str = "30s"):
    """entity: router | ap | client(mac em id). metric: rx_rate|tx_rate|signal|clients"""
    influx = get_influx()
    if entity == "router":
        field = "wan_rx_rate" if metric == "rx_rate" else "wan_tx_rate"
        return influx.history("router", field, None, None, window, every)
    if entity == "ap":
        return influx.history("ap", metric, "ap", id, window, every)
    if entity == "client":
        return influx.history("wifi_client", metric, "mac", id.lower(), window, every)
    if entity == "health":
        # metric: cpu | npu | mem | temp | load ; id: id do roteador ou do AP
        return influx.history("health", metric, "host", id, window, every)
    raise HTTPException(400, "entity inválida")


def _mac_ip(mac: str) -> str | None:
    """IP atual do MAC, a partir do snapshot ao vivo."""
    mac = mac.lower()
    for a in snapshot.get("aps", []):
        for c in a.get("clients", []):
            if c.get("mac") == mac:
                return c.get("ip")
    extra = snapshot.get("extra")
    if extra:
        for c in extra.get("clients", []):
            if c.get("mac") == mac:
                return c.get("ip")
    return None


@app.post("/api/devices/{mac}")
async def api_device_update(mac: str, body: DeviceUpdate):
    if not wifi.valid_mac(mac):
        raise HTTPException(400, "MAC inválido")
    if body.name is not None and len(body.name) > 64:
        raise HTTPException(400, "nome muito longo")
    if body.pref_ap:
        s_cfg = get_settings()
        if body.pref_ap not in {a.id for a in s_cfg.aps}:
            raise HTTPException(400, "AP desconhecido")
    entry = await store.upsert(mac, name=body.name, icon=body.icon,
                               ip=_mac_ip(mac), pref_ap=body.pref_ap)
    return {"mac": mac.lower(), **entry}


@app.post("/api/steer")
async def api_steer(body: SteerRequest):
    s = get_settings()
    if body.target_ap not in {a.id for a in s.aps}:
        raise HTTPException(400, "AP alvo desconhecido")
    try:
        from .steering import enforcer
        r = await wifi.steer(body.mac, body.target_ap)
        # ajuste manual vence o enforcement por um tempo
        if store.get(body.mac).get("pref_ap") not in (None, "", body.target_ap):
            enforcer.pausar(body.mac)
        return r
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


def _ip_name_map() -> dict:
    """IP -> nome do device, a partir do snapshot ao vivo (wifi + cabo)."""
    m = {}
    for a in snapshot.get("aps", []):
        for c in a.get("clients", []):
            if c.get("ip"):
                m[c["ip"]] = c.get("name")
    extra = snapshot.get("extra")
    if extra:
        for c in extra.get("clients", []):
            if c.get("ip"):
                m[c["ip"]] = c.get("name")
    return m


@app.get("/api/redirects")
async def api_redirects_list():
    try:
        rules = await firewall.list_redirects()
    except Exception as exc:
        raise HTTPException(500, str(exc))
    names = _ip_name_map()
    for r in rules:
        r["dest_name"] = names.get(r["dest_ip"])
    return rules


@app.get("/api/firewall")
async def api_firewall():
    """Panorama do firewall: zonas (VLANs), encaminhamentos e regras."""
    try:
        zones = await firewall.list_zones()
        fwds = await firewall.list_forwardings()
        rules = await firewall.list_rules()
    except Exception as exc:
        raise HTTPException(500, str(exc))
    names = _ip_name_map()
    for r in rules:
        r["dest_name"] = names.get(r.get("dest_ip"))
        r["src_name"] = names.get(r.get("src_ip"))
    return {"zones": zones, "forwardings": fwds, "rules": rules}


@app.get("/api/rules")
async def api_rules_list():
    try:
        rules = await firewall.list_rules()
    except Exception as exc:
        raise HTTPException(500, str(exc))
    names = _ip_name_map()
    for r in rules:
        r["dest_name"] = names.get(r.get("dest_ip"))
        r["src_name"] = names.get(r.get("src_ip"))
    return rules


@app.post("/api/rules")
async def api_rules_add(body: RuleCreate):
    try:
        return await firewall.add_rule(body.name, body.src, body.dest, body.target,
                                       body.src_ip, body.dest_ip, body.proto,
                                       body.dest_port)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/rules/{rid}/toggle")
async def api_rules_toggle(rid: str, body: RuleToggle):
    try:
        return await firewall.toggle_rule(rid, body.enabled)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.delete("/api/rules/{rid}")
async def api_rules_delete(rid: str):
    try:
        return await firewall.delete_rule(rid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/redirects")
async def api_redirects_add(body: RedirectCreate):
    try:
        return await firewall.add_redirect(
            body.proto, body.src_dport, body.dest_ip, body.dest_port, body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.put("/api/redirects/{rid}")
async def api_redirects_update(rid: str, body: RedirectCreate):
    try:
        return await firewall.update_redirect(
            rid, body.proto, body.src_dport, body.dest_ip, body.dest_port, body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.delete("/api/redirects/{rid}")
async def api_redirects_delete(rid: str):
    try:
        return await firewall.delete_redirect(rid)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/aps/{ap_id}/radios")
async def api_radios(ap_id: str):
    s = get_settings()
    ap = next((a for a in s.aps if a.id == ap_id), None)
    if ap is None:
        raise HTTPException(404, "AP desconhecido")
    try:
        return await wifi.radio_info(ap)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/aps/{ap_id}/radios/{radio}")
async def api_radio_set(ap_id: str, radio: str, body: RadioUpdate):
    s = get_settings()
    ap = next((a for a in s.aps if a.id == ap_id), None)
    if ap is None:
        raise HTTPException(404, "AP desconhecido")
    try:
        return await wifi.set_radio(ap, radio, body.channel, body.txpower)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/zigbee")
async def api_zigbee_get():
    """Canal Zigbee atual + qual seria o efeito nos APs 2.4GHz."""
    from . import zigbee
    estado = zigbee.carregar()
    ch = estado["channel"]
    out = {"channel": ch, "relatorio": zigbee.relatorio(ch) if ch else None,
           "aps": []}
    if ch is None:
        return out
    # o que cada rádio 2.4GHz está usando agora vs. o que precisaria mudar
    s_cfg = get_settings()
    for ap in s_cfg.aps:
        try:
            radios = await wifi.radio_info(ap)
        except Exception:
            continue
        for r in radios:
            if r.get("band") != "2.4G":
                continue
            atual = r.get("channel")
            seguro = not atual or not zigbee.wifi_conflita(atual, ch)
            out["aps"].append({
                "ap": ap.id, "ap_name": ap.name, "radio": r["radio"],
                "canal_atual": atual, "seguro": seguro,
                "sugerido": zigbee.melhor_canal_wifi(ch, atual),
            })
    return out


@app.post("/api/zigbee")
async def api_zigbee_set(payload: dict):
    """Define o canal Zigbee e ajusta os APs 2.4GHz que estiverem em conflito.

    payload: {"channel": 11..26 | null, "apply": bool}
    - channel null desliga a restrição (não mexe em nada).
    - apply=false só salva e devolve o plano, sem tocar nos rádios.
    """
    from . import zigbee
    ch_in = (payload or {}).get("channel")
    aplicar = bool((payload or {}).get("apply", True))

    estado = zigbee.salvar(ch_in)
    ch = estado["channel"]
    resultado = {"channel": ch, "mudancas": [], "erros": []}
    if ch is None or not aplicar:
        return resultado

    s_cfg = get_settings()
    for ap in s_cfg.aps:
        try:
            radios = await wifi.radio_info(ap)
        except Exception as exc:
            resultado["erros"].append(f"{ap.name}: leitura falhou ({exc})")
            continue
        for r in radios:
            if r.get("band") != "2.4G":
                continue
            atual = r.get("channel")
            if atual and not zigbee.wifi_conflita(atual, ch):
                continue                      # já está seguro
            novo = zigbee.melhor_canal_wifi(ch, atual)
            if novo == atual:
                continue
            try:
                await wifi.set_radio(ap, r["radio"], channel=novo)
                resultado["mudancas"].append({
                    "ap": ap.id, "ap_name": ap.name, "radio": r["radio"],
                    "de": atual, "para": novo})
            except Exception as exc:
                resultado["erros"].append(f"{ap.name}/{r['radio']}: {exc}")

    log.info("zigbee ch%s: %d rádios ajustados", ch, len(resultado["mudancas"]))
    return resultado


@app.get("/api/usage")
async def api_usage(period: str = "day"):
    """Consumo por dispositivo: period = day | week | month."""
    # (janela, agregação, usa_histórico)
    cfg = {
        "day":   ("24h",  "1h", False),   # detalhe fino, bucket ao vivo
        "week":  ("7d",   "1d", True),
        "month": ("30d",  "1d", True),
        "year":  ("365d", "30d", True),   # só existe graças ao histórico permanente
    }
    if period not in cfg:
        raise HTTPException(400, "período inválido (day, week, month ou year)")
    window, every, use_hist = cfg[period]
    inf = get_influx()
    totals = inf.usage_totals(window, every,
                              bucket=inf.hist_bucket if use_hist else None)

    # nome/ícone/AP a partir do snapshot ao vivo, com fallback nos metadados
    live: dict[str, dict] = {}
    for a in snapshot.get("aps", []):
        for c in a.get("clients", []):
            live[c["mac"]] = {"name": c.get("name"), "icon": c.get("icon"),
                              "ip": c.get("ip"), "where": a.get("name"),
                              "online": True, "link": "wifi"}
    extra = snapshot.get("extra")
    if extra:
        for c in extra.get("clients", []):
            live[c["mac"]] = {"name": c.get("name"), "icon": c.get("icon"),
                              "ip": c.get("ip"), "where": "Switch",
                              "online": c.get("online", False), "link": "cabo"}

    rows = []
    for mac, t in totals.items():
        meta = store.get(mac)
        info = live.get(mac, {})
        rows.append({
            "mac": mac,
            "name": info.get("name") or meta.get("name") or mac,
            "icon": info.get("icon") or meta.get("icon", "generic"),
            "ip": info.get("ip"),
            "where": info.get("where"),
            "link": info.get("link"),
            "online": info.get("online", False),
            "down": round(t["down"]),
            "up": round(t["up"]),
            "total": round(t["down"] + t["up"]),
            "dseries": [round(v) for v in t["dseries"]],
            "useries": [round(v) for v in t["useries"]],
        })
    rows.sort(key=lambda r: r["total"], reverse=True)
    return {"period": period, "window": window, "every": every, "devices": rows}


@app.get("/api/icons")
async def api_icons_list():
    return icon_store.all()


@app.post("/api/icons")
async def api_icons_add(file: UploadFile = File(...), name: str = Form(""),
                        strip_bg: bool = Form(True)):
    data = await file.read()
    try:
        return await icon_store.add(data, file.filename or "icone",
                                    file.content_type or "", name, strip_bg)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"não foi possível processar a imagem: {exc}")


@app.post("/api/icons/{icon_id}/rename")
async def api_icons_rename(icon_id: str, body: IconRename):
    try:
        return await icon_store.rename(icon_id, body.name)
    except KeyError:
        raise HTTPException(404, "ícone não encontrado")


@app.delete("/api/icons/{icon_id}")
async def api_icons_delete(icon_id: str):
    try:
        return await icon_store.delete(icon_id)
    except KeyError:
        raise HTTPException(404, "ícone não encontrado")


@app.get("/icons/{icon_id}")
async def api_icon_file(icon_id: str):
    found = icon_store.path_of(icon_id)
    if not found:
        raise HTTPException(404, "ícone não encontrado")
    path, mime = found
    return FileResponse(path, media_type=mime, headers={
        "Cache-Control": "private, max-age=86400",
        # SVG enviado pelo usuário não pode executar nada
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        "X-Content-Type-Options": "nosniff",
    })


def _ingress_base(request: Request) -> str:
    """Prefixo do Ingress que o HA envia (sem barra final). Vazio = standalone."""
    return request.headers.get("X-Ingress-Path", "").rstrip("/")


def _inject_ingress(html: str, base: str) -> str:
    """Faz a UI (URLs absolutas) funcionar sob o caminho do Ingress, sem mexer
    no app: injeta <base href> + um shim que prefixa fetch e desliga o SW."""
    if not base:
        return html
    shim = (
        f'<base href="{base}/">'
        f'<script>window.__INGRESS__="{base}";'
        '(function(){var b=window.__INGRESS__,f=window.fetch;'
        'window.fetch=function(u,o){if(typeof u==="string"&&u[0]==="/"&&u[1]!=="/")u=b+u;return f(u,o);};'
        'try{if(navigator.serviceWorker)navigator.serviceWorker.register=function(){return Promise.reject();};}catch(e){}'
        '})();</script>'
    )
    html = html.replace("<head>", "<head>" + shim, 1)
    # refs absolutas do <head> viram relativas (o <base> resolve pelo Ingress)
    return (html
            .replace('href="/manifest.webmanifest"', 'href="manifest.webmanifest"')
            .replace('href="/static/', 'href="static/')
            .replace('src="/static/', 'src="static/'))


def _serve_html(name: str, request: Request):
    html = (STATIC_DIR / name).read_text(encoding="utf-8")
    return HTMLResponse(_inject_ingress(html, _ingress_base(request)))


@app.get("/api/setup/state")
async def api_setup_state():
    """Estado do provisionamento — consumido pelo assistente e pela aba Config."""
    return {
        "provisioned": provisioning.is_provisioned(),
        "has_ssh_key": provisioning.has_ssh_key(),
        "public_key": provisioning.public_key(),
        "running": _bg["running"],
    }


@app.get("/api/setup/config")
async def api_setup_config():
    """Config atual (pré-preenche a aba Config); nunca devolve segredos."""
    s = provisioning.load_setup() or {}
    s.pop("panel_password_hash", None)
    return s


@app.post("/api/setup/genkey")
async def api_setup_genkey():
    """Gera (ou devolve) o par de chaves SSH e retorna a pública."""
    pub = await asyncio.to_thread(wizard.gen_keypair)
    return {"public_key": pub}


@app.post("/api/setup/test-host")
async def api_setup_test_host(payload: dict):
    """Instala a chave num host via senha e verifica o acesso sem senha."""
    host = str(payload.get("host", "")).strip()
    if not host:
        raise HTTPException(400, "informe o IP/host")
    user = (str(payload.get("user", "root")).strip() or "root")
    password = str(payload.get("password", ""))
    port = int(payload.get("port", 22) or 22)
    return await wizard.test_and_install(host, user, password, port)


@app.post("/api/setup/save")
async def api_setup_save(payload: dict):
    """Grava o setup.json, recarrega a config e liga o coletor ao vivo."""
    gw = payload.get("gateway") or {}
    aps = payload.get("aps") or []
    port = int(payload.get("port", 22) or 22)
    setup = {
        "language": str(payload.get("language", "en")),
        "network_name": str(payload.get("network_name", "")),
        "gateway": {
            "id": gw.get("id") or "gateway",
            "name": gw.get("name") or "Gateway",
            "host": str(gw.get("host", "")).strip(),
            "user": gw.get("user") or "root",
        },
        "aps": [
            {"id": a.get("id") or f"ap{i+1}",
             "name": a.get("name") or f"AP {i+1}",
             "host": str(a.get("host", "")).strip(),
             "user": a.get("user") or "root",
             "radios": a.get("radios") or []}
            for i, a in enumerate(aps)
        ],
        "ssh": {"key_path": provisioning.SSH_KEY_PATH, "port": port,
                "public_key": provisioning.public_key()},
    }
    if not setup["gateway"]["host"]:
        raise HTTPException(400, "informe o IP do gateway")
    provisioning.save_setup(setup)
    reload_settings()
    await start_services()
    return {"ok": True}


@app.get("/setup")
async def setup_page(request: Request):
    if (STATIC_DIR / "setup.html").exists():
        return _serve_html("setup.html", request)
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<title>WifiHub — Setup</title>"
        "<body style='font-family:system-ui;max-width:34rem;margin:4rem auto;"
        "padding:0 1rem;line-height:1.5'>"
        "<h1>WifiHub — modo setup</h1>"
        "<p>O painel ainda não foi configurado. O assistente de configuração "
        "aparecerá aqui (Fase 3).</p>"
        "<p><b>Fase 1 OK:</b> o app subiu sem <code>config.yml</code>, o coletor "
        "está parado e o roteamento para <code>/setup</code> funciona.</p>"
        "<p style='color:#666'>Uso interno apenas — não exponha à internet.</p>"
        "</body>")


@app.get("/")
async def index(request: Request):
    return _serve_html("index.html", request)


@app.get("/sw.js")
async def service_worker():
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest",
                        media_type="application/manifest+json")


# ---------------- Web Push (notificação de device novo) ----------------

@app.get("/api/push/key")
async def push_key():
    return {"key": push.VAPID_PUBLIC, "enabled": push.enabled,
            "subs": push.subs.count()}


@app.post("/api/push/subscribe")
async def push_subscribe(payload: dict):
    sub = payload.get("subscription") or {}
    if not sub.get("endpoint") or not sub.get("keys"):
        raise HTTPException(400, "subscription invalida")
    await push.subs.add(sub, payload.get("label", ""))
    return {"ok": True, "subs": push.subs.count()}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(payload: dict):
    ep = payload.get("endpoint")
    if ep:
        await push.subs.remove(ep)
    return {"ok": True, "subs": push.subs.count()}


@app.post("/api/push/test")
async def push_test():
    if not push.enabled:
        raise HTTPException(400, "push nao configurado")
    report = await push.broadcast("WifiHub", "Teste de notificacao OK", tag="test")
    return {"ok": all(r.get("ok") for r in report) if report else False,
            "subject": push.VAPID_SUBJECT,
            "subs": push.subs.count(), "resultados": report}


@app.get("/api/diag/radios")
async def api_diag_radios():
    """Interfaces hostapd reais de cada AP vs. o que está no config.yml.

    Se o SSID de IoT não estiver na lista do config de algum AP, o ban não era
    aplicado ali e o device escapava exatamente para onde não deveria.
    """
    s_cfg = get_settings()
    out = []
    for ap in s_cfg.aps:
        item = {"id": ap.id, "name": ap.name, "config": list(ap.radios)}
        try:
            reais = await wifi.hostapd_ifaces(ap)
            item["reais"] = reais
            item["faltando_no_config"] = [r for r in reais if r not in ap.radios]
            item["sobrando_no_config"] = [r for r in ap.radios if r not in reais]
            ssids = {}
            for iface in reais:
                try:
                    o = await wifi.pool.run(ap, f"iwinfo {iface} info 2>/dev/null "
                                                f"| head -1", timeout=6)
                    ssids[iface] = o.strip().split('ESSID:')[-1].strip().strip('"')
                except Exception:
                    ssids[iface] = "?"
            item["ssids"] = ssids
        except Exception as exc:
            item["erro"] = str(exc)
        out.append(item)
    return out


@app.get("/api/iot")
async def api_iot_list():
    """IoT e demais fixados, com o AP de agora e o AP preferido.

    Junta duas fontes: o snapshot ao vivo (onde cada um está neste momento) e
    o store (o que foi escolhido). A diferença entre os dois é o que interessa
    ver na tela.
    """
    from . import steering
    onde: dict[str, dict] = {}
    for ap in snapshot.get("aps", []):
        for c in ap.get("clients", []):
            onde[(c.get("mac") or "").lower()] = {
                "ap_atual": ap["id"], "ip": c.get("ip"),
                "name": c.get("name"), "icon": c.get("icon"),
                "band": c.get("band"), "signal": c.get("signal"),
            }
    for c in (snapshot.get("extra") or {}).get("clients", []):
        onde[(c.get("mac") or "").lower()] = {
            "ap_atual": "wired", "ip": c.get("ip"),
            "name": c.get("name"), "icon": c.get("icon"),
        }

    out = []
    macs = set(onde) | set(store.all())
    for mac in macs:
        meta = store.all().get(mac, {})
        vivo = onde.get(mac, {})
        ip = vivo.get("ip") or meta.get("ip")
        if not (steering.is_iot(ip) or meta.get("pref_ap")):
            continue
        out.append({
            "mac": mac,
            "name": meta.get("name") or vivo.get("name") or mac,
            "icon": meta.get("icon") or vivo.get("icon") or "generic",
            "ip": ip,
            "iot": steering.is_iot(ip),
            "link": meta.get("link") or "",
            "pref_ap": meta.get("pref_ap") or "",
            "ap_atual": vivo.get("ap_atual") or "",
            "band": vivo.get("band"),
            "signal": vivo.get("signal"),
            "online": bool(vivo),
        })
    out.sort(key=lambda d: (not d["online"], d["name"].lower()))
    return out


@app.post("/api/pin/iot")
async def api_pin_iot():
    """Fixa cada IoT online no AP em que está agora (foto do momento)."""
    from . import steering
    return await steering.pin_iot(snapshot.get("aps", []))


@app.post("/api/unpin/iot")
async def api_unpin_iot():
    from . import steering
    return {"soltos": await steering.unpin_iot()}


@app.get("/api/ap-internet")
async def api_ap_internet_get():
    from . import apwan
    return await apwan.status()


@app.post("/api/ap-internet")
async def api_ap_internet_set(payload: dict):
    """Liga/desliga a internet dos APs. payload: {"blocked": bool}."""
    from . import apwan
    blocked = bool((payload or {}).get("blocked"))
    try:
        return await apwan.set_blocked(blocked)
    except Exception as exc:
        raise HTTPException(502, f"falha ao alterar firewall: {exc}")


@app.post("/api/reboot/batch")
async def api_reboot_batch(payload: dict):
    """Reinicia os equipamentos escolhidos. Espera {"ids": [...]}.

    Os APs vão primeiro e o gateway por último, sempre. Eles ficam atrás do
    RM1800 na VLAN de gerência — derrubando o gateway antes, o comando SSH
    nem chegaria neles.
    """
    s_cfg = get_settings()
    pedidos = set((payload or {}).get("ids") or [])
    if not pedidos:
        raise HTTPException(400, "nenhum equipamento escolhido")

    aps = [ap for ap in s_cfg.aps if ap.id in pedidos]
    gw = s_cfg.gateway if s_cfg.gateway.id in pedidos else None
    desconhecidos = pedidos - {a.id for a in aps} - ({gw.id} if gw else set())
    if desconhecidos:
        raise HTTPException(404, f"desconhecido: {', '.join(sorted(desconhecidos))}")

    resultado = {"aps": [], "gateway": None, "erros": []}

    async def manda(host):
        try:
            await wifi.reboot_host(host)
            return True
        except Exception as exc:
            resultado["erros"].append(f"{host.name}: {exc}")
            return False

    if aps:
        saidas = await asyncio.gather(*[manda(ap) for ap in aps],
                                      return_exceptions=True)
        for ap, ok in zip(aps, saidas):
            resultado["aps"].append({"id": ap.id, "name": ap.name,
                                     "ok": ok is True})
    if gw:
        if aps:
            await asyncio.sleep(2)   # deixa os comandos saírem antes do caminho cair
        resultado["gateway"] = await manda(gw)

    log.info("reboot em lote: %d APs%s",
             sum(1 for a in resultado["aps"] if a["ok"]),
             " + gateway" if gw else "")
    return resultado


@app.post("/api/reboot/all")
async def api_reboot_all():
    """Reinicia a rede inteira: todos os APs e depois o gateway.

    A ordem não é detalhe. Os APs ficam atrás do RM1800 na VLAN de gerência —
    derrubando o gateway primeiro, o comando não chegaria neles e você teria
    o roteador reiniciando sozinho com os APs intactos.
    """
    s_cfg = get_settings()
    resultado = {"aps": [], "gateway": None, "erros": []}

    async def manda(host):
        try:
            await wifi.reboot_host(host)
            return True
        except Exception as exc:
            resultado["erros"].append(f"{host.name}: {exc}")
            return False

    # APs em paralelo: cada um tem sua própria conexão SSH
    saidas = await asyncio.gather(*[manda(ap) for ap in s_cfg.aps],
                                  return_exceptions=True)
    for ap, ok in zip(s_cfg.aps, saidas):
        resultado["aps"].append({"id": ap.id, "name": ap.name,
                                 "ok": ok is True})

    # espaço para os comandos saírem antes de o caminho cair
    await asyncio.sleep(2)
    resultado["gateway"] = await manda(s_cfg.gateway)

    log.info("reboot geral: %d APs + gateway",
             sum(1 for a in resultado["aps"] if a["ok"]))
    return resultado


@app.post("/api/reboot")
async def api_reboot(payload: dict):
    """Reinicia o gateway ou um AP. Espera {"id": "<id do equipamento>"}."""
    target = (payload or {}).get("id", "")
    s = get_settings()
    hosts = {s.gateway.id: s.gateway}
    hosts.update({ap.id: ap for ap in s.aps})
    host = hosts.get(target)
    if host is None:
        raise HTTPException(404, "equipamento desconhecido")
    try:
        await wifi.reboot_host(host)
    except Exception as exc:
        log.warning("reboot %s: %s", target, exc)
        raise HTTPException(502, f"falha ao reiniciar: {exc}")
    log.info("reboot solicitado: %s (%s)", host.name, host.host)
    return {"ok": True, "id": target, "name": host.name}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
