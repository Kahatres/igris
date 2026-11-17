// Igris service worker: makes the page installable as an app and shows a Turkish
// "cannot reach Igris" page when the server is down. It caches nothing and never
// touches API calls, so every answer still comes live from the server.
"use strict";

const OFFLINE = `<!DOCTYPE html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Igris</title>
<style>html,body{margin:0;height:100%;background:#000;color:#e4e6ff;
font-family:"Segoe UI",system-ui,sans-serif}main{min-height:100%;display:grid;
place-content:center;gap:12px;padding:16px;text-align:center}h1{font-size:20px;margin:0}
p{color:#8a8fbf;margin:0;max-width:34em}button{justify-self:center;margin-top:8px;
padding:10px 18px;border-radius:999px;border:1px solid rgba(120,255,170,.35);
background:transparent;color:#5dffd0;font:inherit;cursor:pointer}</style></head>
<body><main><h1>Igris'e ulaşılamıyor</h1>
<p>Masaüstü bilgisayar kapalı olabilir, Igris çalışmıyor olabilir ya da bu cihaz
Tailscale'e bağlı değil.</p>
<button type="button" onclick="location.reload()">Tekrar dene</button></main></body></html>`;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(fetch(event.request).catch(() => new Response(OFFLINE, {
    status: 503, headers: {"Content-Type": "text/html; charset=utf-8"},
  })));
});
