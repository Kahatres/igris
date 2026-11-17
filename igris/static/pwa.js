// Registers the service worker that lets the browser install Igris as an app.
"use strict";
if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("/sw.js", {scope: "/"}).catch(() => {});
}
