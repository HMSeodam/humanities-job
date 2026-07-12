const CACHE="humanities-jobs-v1";
const APP=["./","index.html","manifest.webmanifest","data/jobs.json"];
self.addEventListener("install",event=>event.waitUntil(caches.open(CACHE).then(c=>c.addAll(APP))));
self.addEventListener("activate",event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k))))));
self.addEventListener("fetch",event=>{
  if(event.request.url.includes("data/jobs.json")){
    event.respondWith(fetch(event.request).then(r=>{const copy=r.clone();caches.open(CACHE).then(c=>c.put(event.request,copy));return r}).catch(()=>caches.match(event.request)));
    return;
  }
  event.respondWith(caches.match(event.request).then(cached=>cached||fetch(event.request)));
});
self.addEventListener("push",event=>{
  const data=event.data?event.data.json():{title:"인문잡",body:"새 인문학 채용공고가 등록되었습니다."};
  event.waitUntil(self.registration.showNotification(data.title,{body:data.body,data:{url:data.url||"./"}}));
});
self.addEventListener("notificationclick",event=>{event.notification.close();event.waitUntil(clients.openWindow(event.notification.data.url||"./"))});
