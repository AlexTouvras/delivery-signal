const CACHE = "delivery-signal-v1";
const SHARE = "delivery-signal-share";

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll([
    "./index.html",
    "./app.js",
    "./manifest.webmanifest",
    "./icon-192.png",
    "./icon-512.png",
    "./engine/pipeline.py",
    "./engine/extract.py",
  ])));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method === "POST" && url.pathname.endsWith("/share")) {
    event.respondWith(receiveShare(event.request));
    return;
  }
  if (event.request.method !== "GET" || url.origin !== self.location.origin) {
    return;
  }
  event.respondWith(networkFirst(event.request));
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    if (response.ok) {
      cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    const cached = await cache.match(request);
    if (cached) {
      return cached;
    }
    throw error;
  }
}

async function receiveShare(request) {
  const form = await request.formData();
  const file = form.get("export");
  if (file && typeof file.arrayBuffer === "function") {
    const body = await file.arrayBuffer();
    const headers = new Headers({
      "Content-Type": "text/csv",
      "X-File-Name": file.name || "export.csv",
    });
    const cache = await caches.open(SHARE);
    await cache.put(new URL("./shared-export", self.registration.scope), new Response(body, { headers }));
  }
  return Response.redirect(new URL("./index.html?shared=1", self.registration.scope), 303);
}
