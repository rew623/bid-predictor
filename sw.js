/* 서비스워커 — 앱이 쓰는 파일(index.html, app.js, style.css, manifest.json, icons/)을 바꾸면 VERSION 을 올린다. */
const VERSION = '1.18.11';
const SHELL_CACHE = 'shell-' + VERSION;
const DATA_CACHE = 'data-v1';
const FONT_CACHE = 'fonts-v1';
const SHELL = ['./', 'index.html', 'app.js', 'style.css', 'manifest.json', 'icons/icon.svg', 'icons/icon-192.png', 'icons/icon-512.png'];

self.addEventListener('install', (e) => {
  // 새 버전은 대기 상태로 두고, 사용자가 "새로고침"을 누르면 SKIP_WAITING 으로 활성화
  e.waitUntil(caches.open(SHELL_CACHE).then(c => c.addAll(SHELL)));
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    for(const k of await caches.keys()){
      if(k.startsWith('shell-') && k !== SHELL_CACHE) await caches.delete(k);
    }
    await self.clients.claim();
  })());
});

self.addEventListener('message', (e) => {
  if(e.data?.type === 'SKIP_WAITING') self.skipWaiting();
  if(e.data?.type === 'GET_VERSION') e.ports[0]?.postMessage(VERSION);
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if(req.method !== 'GET') return;
  const url = new URL(req.url);

  if(url.origin === 'https://fonts.googleapis.com' || url.origin === 'https://fonts.gstatic.com'){
    e.respondWith(staleWhileRevalidate(req, FONT_CACHE));
    return;
  }
  if(url.origin !== location.origin) return;

  const scope = new URL(self.registration.scope).pathname;
  const path = url.pathname.startsWith(scope) ? url.pathname.slice(scope.length) : url.pathname;

  if(path.startsWith('data/')){
    // ?v=<갱신시각> 이 붙은 데이터 파일은 버전이 같으면 캐시 사용, 나머지(meta.json)는 네트워크 우선
    e.respondWith(url.searchParams.has('v') ? versionedData(req, url) : networkFirst(req, DATA_CACHE));
    return;
  }
  if(req.mode === 'navigate'){
    e.respondWith(networkFirst(req, SHELL_CACHE, 'index.html'));
    return;
  }
  // 앱 파일도 네트워크 우선 — 화면(index.html)만 새것이고 app.js 는 옛 캐시인 채로 섞이지 않게. 오프라인이면 캐시
  e.respondWith(networkFirst(req, SHELL_CACHE));
});

async function versionedData(req, url){
  const cache = await caches.open(DATA_CACHE);
  const hit = await cache.match(req);
  if(hit) return hit;
  try{
    const res = await fetch(req);
    if(res.ok){
      // 같은 파일의 옛 버전은 지운다
      for(const k of await cache.keys()){
        const ku = new URL(k.url);
        if(ku.pathname === url.pathname && ku.search !== url.search) await cache.delete(k);
      }
      await cache.put(req, res.clone());
    }
    return res;
  }catch(err){
    const old = await cache.match(req, {ignoreSearch:true});
    if(old) return old;
    throw err;
  }
}

async function networkFirst(req, cacheName, fallback){
  const cache = await caches.open(cacheName);
  try{
    const res = await fetch(req, {cache:'no-store'});
    if(res.ok) await cache.put(req, res.clone());
    return res;
  }catch(err){
    const hit = await cache.match(req, {ignoreSearch:true}) || (fallback && await caches.match(fallback));
    if(hit) return hit;
    throw err;
  }
}

async function staleWhileRevalidate(req, cacheName){
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req);
  const net = fetch(req).then(res => { if(res.ok || res.type === 'opaque') cache.put(req, res.clone()); return res; }).catch(() => hit);
  return hit || net;
}
