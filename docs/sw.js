// Push only. ponytail: no offline cache; the board is live data, add one if a stale board beats a blank one
self.addEventListener("push", e => {
  const d = e.data ? e.data.json() : {};
  e.waitUntil(Promise.all([
    self.registration.showNotification(d.title || "Live Bet Predictor",
      { body: d.body, tag: d.tag || undefined, icon: "icon-192.png", data: { url: d.url || "./" } }),
    navigator.setAppBadge ? navigator.setAppBadge().catch(() => {}) : null,
  ]));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = new URL(e.notification.data.url, self.registration.scope).href;
  e.waitUntil(clients.matchAll({ type: "window" }).then(ws =>
    ws.length ? ws[0].navigate(url).then(w => (w || ws[0]).focus()) : clients.openWindow(url)));
});
