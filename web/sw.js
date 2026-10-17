// Casa service worker: shows web push notifications (hub/push.py) and opens the app on tap.
// No offline cache: the app is always loaded fresh from the hub.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data.json(); } catch { d = { title: "Casa", body: e.data?.text() || "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "Casa", {
    body: d.body || "", tag: d.tag || undefined, data: { url: d.url || "/" },
    icon: "/static/icon-192.png", badge: "/static/icon-192.png",
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL(e.notification.data?.url || "/", self.location.origin).href;
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) { if ("focus" in w) { await w.navigate(url).catch(() => {}); return w.focus(); } }
    return self.clients.openWindow(url);
  })());
});
