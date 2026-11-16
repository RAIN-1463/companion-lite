// Companion Lite · Service Worker
// 目标：断网也能打开外壳；接口永远走网络（不缓存聊天内容）

const CACHE = "companion-v4";
const SHELL = [
  "/",
  "/manifest.webmanifest",
  "/icon-192.png",
  "/icon-512.png",
  "/icon-maskable-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  if (url.origin !== self.location.origin) return;

  // 接口一律走网络（聊天内容不进缓存）
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/backgrounds/")) {
    e.respondWith(fetch(e.request).then((r) => {
      if (r.status >= 500) throw new Error("upstream " + r.status);
      return r;
    }).catch(() => new Response(
      JSON.stringify({ error: "offline" }),
      { status: 503, headers: { "Content-Type": "application/json" } }
    )));
    return;
  }

  // 外壳：网络优先，失败回缓存，再失败给离线页
  if (e.request.mode === "navigate" || url.pathname === "/") {
    e.respondWith(
      fetch(e.request)
        .then((r) => {
          if (!r.ok) throw new Error("bad status " + r.status);
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put("/", copy)).catch(() => {});
          return r;
        })
        .catch(() => caches.match("/").then((r) => r || offlinePage()))
    );
    return;
  }

  // 静态资源：缓存优先
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request).then((r) => {
      const copy = r.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
      return r;
    }).catch(() => offlinePage()))
  );
});

function offlinePage() {
  return new Response(
    `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
     <style>html,body{height:100%;margin:0;background:#111318;color:#d7dce8;
     font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
     display:flex;align-items:center;justify-content:center;text-align:center;line-height:1.9}
     .b{font-size:15px}.s{font-size:12px;color:#8b93a5;margin-top:10px}</style>
     <div><div class="b">连不上她那边</div>
     <div class="s">电脑可能关机了，或者 Tailscale 断了<br>连上以后刷新一下就好</div></div>`,
    { headers: { "Content-Type": "text/html; charset=utf-8" } }
  );
}
