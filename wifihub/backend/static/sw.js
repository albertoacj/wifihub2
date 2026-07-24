// Service worker: casca do app em cache + Web Push.
// Nunca cacheia /api (dados são sempre ao vivo).
const CACHE = "wifihub-v2";
const SHELL = ["/", "/manifest.webmanifest", "/static/icons/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/")) return; // dados sempre da rede
  // network-first: pega fresco, cai pro cache se estiver offline
  e.respondWith(
    fetch(e.request)
      .then((r) => {
        const copy = r.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
        return r;
      })
      .catch(() => caches.match(e.request).then((m) => m || caches.match("/")))
  );
});

/* ------------------------------ Web Push ------------------------------ */

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (_) {
    data = { title: "WifiHub", body: event.data ? event.data.text() : "" };
  }

  // iOS exige que TODO push mostre uma notificação visível.
  event.waitUntil(
    self.registration.showNotification(data.title || "WifiHub", {
      body: data.body || "",
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/icon-192.png",
      tag: data.tag || "wifihub",
      renotify: true,
      data: { url: data.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true })
      .then((list) => {
        for (const c of list) {
          if ("focus" in c) {
            c.navigate && c.navigate(target);
            return c.focus();
          }
        }
        return self.clients.openWindow(target);
      })
  );
});

// Navegador rotacionou a assinatura: registra a nova no servidor.
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(
    fetch("/api/push/key")
      .then((r) => r.json())
      .then(({ key }) =>
        self.registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: Uint8Array.from(
            atob(key.replace(/-/g, "+").replace(/_/g, "/")),
            (c) => c.charCodeAt(0)
          ),
        })
      )
      .then((sub) =>
        fetch("/api/push/subscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ subscription: sub.toJSON(), label: "renovada" }),
        })
      )
      .catch(() => {})
  );
});
