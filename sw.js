const CACHE="humanities-jobs-v3";
const APP=["index.html","manifest.webmanifest"];
self.addEventListener("install",event=>event.waitUntil(
  caches.open(CACHE).then(cache=>cache.addAll(APP)).then(()=>self.skipWaiting())
));
self.addEventListener("activate",event=>event.waitUntil(
  caches.keys()
    .then(keys=>Promise.all(keys.filter(key=>key!==CACHE).map(key=>caches.delete(key))))
    .then(()=>self.clients.claim())
));
self.addEventListener("fetch",event=>{
  if(event.request.url.includes("data/jobs.json")){
    const canonical=new URL("data/jobs.json",self.registration.scope).href;
    event.respondWith(
      fetch(event.request,{cache:"no-store"})
        .then(response=>{
          if(!response.ok)throw new Error("data response failed");
          const copy=response.clone();
          caches.open(CACHE).then(cache=>cache.put(canonical,copy));
          return response;
        })
        .catch(()=>caches.match(canonical))
    );
    return;
  }
  if(event.request.mode==="navigate"){
    event.respondWith(
      fetch(event.request,{cache:"no-store"})
        .then(response=>{
          const copy=response.clone();
          caches.open(CACHE).then(cache=>cache.put("index.html",copy));
          return response;
        })
        .catch(()=>caches.match("index.html"))
    );
    return;
  }
  event.respondWith(caches.match(event.request).then(cached=>cached||fetch(event.request)));
});
self.addEventListener("push",event=>{
  const data=event.data?event.data.json():{title:"인문잡",body:"새 인문학 채용공고가 등록되었습니다."};
  event.waitUntil(self.registration.showNotification(data.title,{body:data.body,data:{url:data.url||"./"}}));
});
self.addEventListener("notificationclick",event=>{event.notification.close();event.waitUntil(clients.openWindow(event.notification.data.url||"./"))});
