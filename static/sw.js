// La Luna storefront: online-first. Do not cache old HTML or API responses.
const CURRENT='laluna-assets-product-photos-v21';
const ASSETS=['/static/storefront-upgrade.js','/static/upgrade.css','/static/backoffice.js','/static/backoffice.css','/static/logo-original.png','/static/logo-backoffice-transparent.png'];
self.addEventListener('install',event=>event.waitUntil(self.skipWaiting()));
self.addEventListener('activate',event=>event.waitUntil((async()=>{
 const names=await caches.keys();
 await Promise.all(names.map(name=>caches.delete(name)));
 await self.clients.claim();
})()));
// No fetch interception. The live server is always the source of truth for pages and ordering.
