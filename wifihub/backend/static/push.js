/* Web Push do WifiHub.
   Só a lógica: o botão fica no topo da página, montado pelo index.html.
   iOS só libera Notification.requestPermission() dentro de um toque do
   usuário e só quando o app está na tela de início — daí o botão. */
(function () {
  const SUPPORTED = "serviceWorker" in navigator && "PushManager" in window;
  const STANDALONE =
    window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;
  const IOS = /iPad|iPhone|iPod/.test(navigator.userAgent);

  function b64ToU8(b64) {
    const pad = "=".repeat((4 - (b64.length % 4)) % 4);
    const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }

  // fallback: se o app não passar um toast, mostra um balão simples
  function fallbackToast(msg) {
    const el = document.createElement("div");
    el.textContent = msg;
    el.style.cssText =
      "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);" +
      "background:#20242A;color:#fff;border:1px solid #3a4048;padding:10px 14px;" +
      "border-radius:10px;font:13px system-ui;z-index:9999;max-width:80vw;text-align:center";
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }
  const say = (fn, msg, cor) => (fn ? fn(msg, cor) : fallbackToast(msg));

  async function subscribe(toast) {
    if (!SUPPORTED) {
      say(toast, "Este navegador não suporta notificações push.");
      return false;
    }
    if (IOS && !STANDALONE) {
      say(toast, "No iPhone: abra pelo ícone da tela de início.");
      return false;
    }

    const perm = await Notification.requestPermission();
    if (perm !== "granted") {
      say(toast, "Permissão negada. Libere nas configurações do sistema.");
      return false;
    }

    const reg = await navigator.serviceWorker.ready;
    const { key } = await fetch("/api/push/key").then((r) => r.json());
    if (!key) {
      say(toast, "Servidor sem chave VAPID configurada.");
      return false;
    }

    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: b64ToU8(key),
      });
    }

    await fetch("/api/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subscription: sub.toJSON(),
        label: navigator.userAgent.slice(0, 60),
      }),
    });

    // dispara um teste na hora: no iPhone não dá para abrir console,
    // então a resposta do provedor tem que aparecer na tela
    say(toast, "Registrado. Testando…");
    try {
      const r = await fetch("/api/push/test", { method: "POST" }).then((x) => x.json());
      const mine = (r.resultados || []).find((x) =>
        IOS ? /Apple/.test(x.provider) : !/Apple/.test(x.provider)
      );
      if (!mine) say(toast, "Registrado ✓ (sem retorno do teste)");
      else if (mine.ok) say(toast, "Notificações ativadas ✓");
      else {
        let motivo = mine.error || "";
        try { motivo = JSON.parse(motivo).reason || motivo; } catch (_) {}
        say(toast, `Falhou ${mine.status}: ${motivo || mine.hint || "sem detalhe"}`.slice(0, 160),
            "var(--alert)");
      }
    } catch (e) {
      say(toast, "Registrado, mas o teste falhou: " + e.message, "var(--alert)");
    }
    return true;
  }

  async function unsubscribe(toast) {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (!sub) return;
    await fetch("/api/push/unsubscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint: sub.endpoint }),
    });
    await sub.unsubscribe();
    say(toast, "Notificações desativadas neste aparelho.");
  }

  async function isActive() {
    if (!SUPPORTED || Notification.permission !== "granted") return false;
    const reg = await navigator.serviceWorker.ready;
    return !!(await reg.pushManager.getSubscription());
  }

  window.wifihubPush = { supported: SUPPORTED, ios: IOS, standalone: STANDALONE,
                         subscribe, unsubscribe, isActive };
})();
