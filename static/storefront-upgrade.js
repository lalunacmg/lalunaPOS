/* Uses the existing La Luna cart / menu functions rather than replacing the site. */
(()=>{
'use strict';
const API='/api';
const q=(s)=>document.querySelector(s);
const e=(str)=>String(str??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const money=n=>'₱'+Number(n||0).toLocaleString('en-PH',{minimumFractionDigits:2,maximumFractionDigits:2});
const params=new URLSearchParams(location.search);
const rawQrTable=params.get('table');
const qrTable=rawQrTable!==null && /^[A-Za-z0-9_-]{1,12}$/.test(rawQrTable.trim())?rawQrTable.trim():'';
const invalidQrTable=rawQrTable!==null&&!qrTable;
window.LaLunaTableQr=qrTable;
let ready=false, payments=[], suggestionBusy=false;
window.LaLunaOrderingReady=false;
let orderInFlight=false;
let retryTimer=null;
let connectionAttempts=0;
let menuRevision='';
let menuSyncTask=null;
let cartRevision=0;
const MENU_SYNC_INTERVAL_MS=15000;

// Table QR codes identify the table, but they DO NOT restrict the order type.
// Dine-In uses the scanned table. Take-Out orders intentionally have no table.
// Keep a customer's Take-Out choice even if the API reconnects or the menu reloads.
function applyTableContext(){
 if(!qrTable)return;
 const table=q('#llTableNum');
 const dineIn=q('.ot-option[onclick*="Dine-In"]');
 const takeaway=q('.ot-option[onclick*="Take-Out"]');
 const row=q('#llTableRow');
 if(table){table.value=qrTable;table.readOnly=true;table.setAttribute('aria-label','Table '+qrTable+' (set by QR code for Dine-In)');}
 if(takeaway){takeaway.disabled=false;takeaway.removeAttribute('aria-disabled');takeaway.removeAttribute('title');}
 // Select Dine-In by default ONLY when no order type has been chosen yet.
 // Do not call setOT(): it would navigate away from the Menu landing page.
 if(!curOT && dineIn){
  curOT='Dine-In';
  dineIn.classList.add('sel');
  dineIn.setAttribute('aria-pressed','true');
  const display=q('#otDisplay');if(display)display.textContent='Dine-In';
  if(typeof checkSendBtn==='function')checkSendBtn();
 }
 if(row)row.style.display=curOT==='Dine-In'?'':'none';
 for(const [selector,id] of [['#page-menu .menu-top','llMenuTableBanner'],['#page-order .order-body','llOrderTableBanner']]){
  const container=q(selector);
  if(!container||q('#'+id))continue;
  const banner=document.createElement('div');banner.id=id;banner.className='ll-table-qr-banner';banner.setAttribute('role','status');
  banner.textContent='Table '+qrTable+' QR | Choose Dine-In or Take-Out. The table is used for Dine-In only.';
  container.insertAdjacentElement('afterbegin',banner);
 }
}
window.LaLunaApplyQrTable=applyTableContext;
const api=async(path,options={})=>{
 const response=await fetch(API+path,{cache:'no-store',...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});
 let data={};try{data=await response.json();}catch{}
 if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'Request failed ('+response.status+')');
 return data;
};
const show=msg=>typeof toast==='function'?toast(msg):alert(msg);
const connectionBanner=()=>q('#llConnectionBanner');
function setConnectionState(kind,message){
 const banner=connectionBanner();
 if(!banner)return;
 banner.hidden=kind==='online';
 banner.dataset.state=kind;
 const messageEl=q('#llConnectionMessage');
 if(messageEl)messageEl.textContent=message;
 const retry=q('#llRetryConnection');
 if(retry)retry.disabled=kind==='connecting';
}
async function connectLive(){
 if(location.protocol==='file:'){
   setConnectionState('offline','This HTML file is opened directly. Run START_LA_LUNA_ALL_IN_ONE.bat and use http://127.0.0.1:8000/ for live orders.');
   return false;
 }
 clearTimeout(retryTimer);
 const attempt=++connectionAttempts;
 setConnectionState('connecting','Connecting to La Luna order system…');
 try{
   const health=await api('/health');
   if(!health.ok)throw Error('The server is not ready');
   // Check a stable API capability, not a release-specific build name. The old
   // exact build check silently broke QR checkout after every backend update.
   if(health.checkout_protocol!=='direct-orders-v1'||
      !Array.isArray(health.order_types)||
      !['Dine-In','Take-Out'].every(type=>health.order_types.includes(type))){
     throw Error('This server does not support the current direct-checkout API. Update the complete La Luna project, not just index.html.');
   }
   const data=await api('/menu');
   if(!Array.isArray(data.products)||!Array.isArray(data.payments))throw Error('Invalid menu data from the server');
   if(!data.products.length)throw Error('The product menu is empty. Check the back-office product settings.');
   payments=data.payments;
   mergeMenu(data.products);
   menuRevision=data.revision||'';
   renderPaymentChoices();
   ready=true;window.LaLunaOrderingReady=true;
   if(typeof checkSendBtn==='function')checkSendBtn();
   setConnectionState('online','Connected');
   applyTableContext();
   await refreshRewards();
   return true;
 }catch(err){
   ready=false;window.LaLunaOrderingReady=false;
   if(typeof checkSendBtn==='function')checkSendBtn();
   const message=String(err&&err.message||err||'Unknown connection error');
   console.warn('La Luna live ordering connection:',message);
   setConnectionState('offline','Ordering is unavailable: '+message+'. Orders are not sent until the connection is restored.');
   // Transient startup errors are retried without requiring a page reload.
   if(attempt<=2)retryTimer=setTimeout(connectLive,3500*attempt);
   return false;
 }
}

async function init(){
 const full=q('#cartFull'),tools=q('#page-order .order-body');
 if(!full||!tools){console.error('Customer UI is incomplete. Install the full updated static/index.html file.');return;}
 if(invalidQrTable)show('Invalid table QR link. Ask staff to generate a new table QR.');
 applyTableContext();
 const menuTop=q('#page-menu .menu-top');
 if(menuTop&&!q('#llConnectionBanner')){
  const banner=document.createElement('div');
  banner.id='llConnectionBanner';banner.className='ll-connection-banner';banner.setAttribute('role','status');
  banner.innerHTML='<span id="llConnectionMessage">Connecting to La Luna order system…</span><button type="button" id="llRetryConnection">Retry</button>';
  menuTop.insertAdjacentElement('afterend',banner);
  q('#llRetryConnection').addEventListener('click',()=>{connectionAttempts=0;connectLive();});
 }

 const ups=document.createElement('section');ups.className='ll-card';ups.id='llUpsells';ups.hidden=true;
 ups.innerHTML='<h3>✨ Goes well with your order</h3><p>Suggested using popular pairings and past orders.</p><div id="llUpsellList"></div>';
 full.insertBefore(ups,full.children[1]);
 const loyalty=document.createElement('section');loyalty.className='ll-card';loyalty.innerHTML=`<h3>⭐ La Luna Digital Loyalty</h3><p>Collect 8 stamps to earn a free drink. Stamps are credited when a registered customer's order is completed and marked paid. Earlier loyalty cards are not automatically migrated into this new system.</p>
 <div id="llLoyaltyJoin"><div class="ll-grid"><input class="ll-field" id="llLoyaltyName" placeholder="Full name" autocomplete="name"><input class="ll-field" id="llLoyaltyPhone" placeholder="Mobile number" autocomplete="tel"></div>
 <label class="ll-flex ll-muted" style="margin-top:10px"><input type="checkbox" id="llConsent"> I agree to store my contact details for La Luna loyalty rewards.</label>
 <button class="ll-action" id="llJoinBtn">Join Rewards</button><p class="ll-muted" style="margin-top:8px">Phone verification is not yet enabled. Save your card on this device.</p></div>
 <div id="llLoyaltyCard" hidden><strong id="llCardName"></strong><div class="ll-stamps" id="llStamps"></div><p id="llLoyaltyStats"></p><button class="ll-action ll-ghost" id="llRefreshCard">Refresh Card</button></div>
 <p id="llLoyaltyFeedback" role="status"></p>`;
 tools.appendChild(loyalty);
 q('#llJoinBtn').addEventListener('click',joinRewards);
 q('#llRefreshCard').addEventListener('click',refreshRewards);
 const track=document.createElement('section');track.className='ll-card';track.innerHTML='<h3>📦 Track a restaurant order</h3><p>Enter the reference provided after placing an online order.</p><div class="ll-flex"><input class="ll-field" id="llTrackID" placeholder="Order reference" style="flex:1;min-width:140px"><button class="ll-action" id="llTrackBtn" style="width:auto;margin:0">Track</button></div><p id="llTrackResult" role="status" style="margin-top:9px"></p>';
 tools.insertBefore(track,loyalty);
 q('#llTrackBtn').addEventListener('click',trackOrder);
 const nav=document.createElement('p');nav.style.cssText='margin:20px 0 10px;text-align:center;font-size:.7rem;color:#9f9485';
 nav.innerHTML='<a href="/backoffice" style="color:#ddb975;text-decoration:underline">Staff &amp; Back Office</a>';tools.appendChild(nav);
 let stored=params.get('order');try{stored=stored||localStorage.getItem('laluna_last_order');}catch{}
 if(stored&&q('#llTrackID'))q('#llTrackID').value=stored;
 if(stored)trackOrder();
 const previous=window.renderCart;
 if(typeof previous==='function')window.renderCart=function(){previous();setTimeout(refreshUpsells,0);};
 document.addEventListener('click',event=>{
  if(event.target.closest('.item-add-btn,.bs-add,.modal-add-btn,.cqb,.ci-rm'))setTimeout(refreshUpsells,160);
  if(event.target.closest('.ot-option')){const row=q('#llTableRow');if(row)row.style.display=curOT==='Dine-In'?'':'none';}
 });
 await connectLive();
 // Table QR images stay unchanged; an open customer page checks the CURRENT
 // database catalog while visible, and immediately on returning to the tab.
 setInterval(()=>{if(!document.hidden)refreshLiveMenu().catch(()=>{});},MENU_SYNC_INTERVAL_MS);
 for(const eventName of ['focus','pageshow','online']){
   window.addEventListener(eventName,()=>refreshLiveMenu().catch(()=>{}));
 }
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshLiveMenu().catch(()=>{});});
}

// Match the backend's server-side variant/add-on rules for cart display.
// The server STILL owns the final price and inventory validation at checkout.
function refreshedUnitPrice(product,mods){
 let base=Number(product.price);
 if(product.bundle_qty){
   if(!mods.startsWith('Milktea: '))return null;
   return base;
 }
 let parts=mods?mods.split(', '):[];
 const variants=product.variants||[],addons=product.addons||[];
 if(product.dual){
   const chosen=(parts.shift()||'').split(' + ');
   if(chosen.length!==2||chosen.some(x=>!variants.some(v=>v.l===x)))return null;
 }else if(variants.length){
   const chosen=parts.filter(x=>variants.some(v=>v.l===x));
   if(chosen.length!==1)return null;
   base=Number(variants.find(v=>v.l===chosen[0]).p);
   parts=parts.filter(x=>x!==chosen[0]);
 }
 const optionNames=parts;
 if(new Set(optionNames).size!==optionNames.length)return null;
 for(const name of optionNames){
   const add=addons.find(x=>x.name===name);
   if(!add)return null;
   base+=Number(add.price);
 }
 return Math.round(base*100)/100;
}
function mergeMenu(products){
 if(typeof MENU==='undefined')return false;
 const toOriginal=p=>({id:p.id,cat:p.category,img:/^\/media\/products\/[a-f0-9]{32}\.webp$/.test(p.image_url||'')?p.image_url:'',icon:e(p.icon),name:e(p.name),pop:!!p.popular,desc:e(p.description),p:Number(p.price),v:p.variants.map(v=>({l:e(v.l),p:Number(v.p)})),a:p.addons.map(a=>({name:e(a.name),price:Number(a.price)})),dual:!!p.dual,bundleQty:p.bundle_qty||0,bundle:!!p.bundle_qty});
 const available=products.filter(x=>x.active && (x.stock===null||x.stock>0));
 const byId=new Map(available.map(x=>[x.id,x]));
 let changedCart=false;
 let removed=0;
 // Keep the existing customer cart whenever possible; remove unavailable
 // items and recalculate prices if the back office changed the catalog.
 // Never carry an old price into the review screen after a live menu edit.
 cart=cart.filter(item=>{
   const p=byId.get(item.iid);
   const price=p?refreshedUnitPrice(p,String(item.mods||'').replace(/&amp;/g,'&').replace(/&#39;/g,"'")):null;
   if(price===null || !Number.isFinite(price)){
     removed+=1;changedCart=true;return false;
   }
   if(item.up!==price || item.name!==e(p.name) || item.icon!==e(p.icon))changedCart=true;
   item.up=price;item.name=e(p.name);item.icon=e(p.icon);item.total=Math.round(price*item.qty*100)/100;
   return true;
 });
 MENU.splice(0,MENU.length,...available.map(toOriginal));
 CATS.splice(1,CATS.length-1,...[...new Set(MENU.map(p=>p.cat))]);
 if(typeof currentCategory!=='undefined'&&!CATS.includes(currentCategory))currentCategory='All';
 buildChips();
 const active=[...document.querySelectorAll('.chip')].find(x=>x.textContent===(typeof currentCategory!=='undefined'?currentCategory:'All'));
 document.querySelectorAll('.chip').forEach(x=>x.classList.toggle('active',x===active));
 buildMenu(typeof currentCategory!=='undefined'?currentCategory:'All');
 buildBS();
 if(curItem){
   const overlay=q('#modalOverlay');
   // Do not allow a product modal opened before the update to add an
   // outdated price or discontinued modifier after the catalog changes.
   if(overlay?.classList.contains('open')&&typeof closeModal==='function'){
     closeModal();
     show('The menu has changed. Reopen the item to see its latest options.');
   }
 }
 updBadge();renderCart();
 // A pending review must never silently submit changed prices/items shown in
 // the previous confirmation popup. Require a fresh review of the new cart.
 if(changedCart && !orderInFlight && q('#confirmPopup')?.classList.contains('show') &&
    typeof closeConfirmPopup==='function')closeConfirmPopup(true);
 if(changedCart)cartRevision+=1;
 if(removed)show(removed+' unavailable cart item(s) removed after a menu update.');
 else if(changedCart)show('Menu prices or items changed. Please review your cart.');
 return changedCart;
}
async function refreshLiveMenu(force=false){
 // A checkout must WAIT for any background menu sync already in progress.
 // Otherwise a stale review could be submitted while new prices are loading.
 if(menuSyncTask){
   if(!force)return menuSyncTask;
   try{await menuSyncTask;}catch{}
 }
 const task=(async()=>{
   if(!ready)return connectLive();
   try{
     const version=await api('/menu/revision');
     if(!force&&version.revision===menuRevision)return false;
     const data=await api('/menu');
     if(!Array.isArray(data.products)||!Array.isArray(data.payments)||!data.revision)throw Error('Menu response incomplete');
     if(data.revision===menuRevision)return false;
     const cartChanged=mergeMenu(data.products);
     const previousPayment=curPayment;
     payments=data.payments;
     renderPaymentChoices();
     if(previousPayment&&payments.some(x=>x.name===previousPayment)){
       const option=[...document.querySelectorAll('.pay-option')].find(x=>x.querySelector('.pay-name')?.textContent===previousPayment);
       if(option)option.click();
     }else if(previousPayment)curPayment='';
     if(typeof checkSendBtn==='function')checkSendBtn();
     menuRevision=data.revision;
     applyTableContext();
     return cartChanged;
   }catch(err){
     ready=false;window.LaLunaOrderingReady=false;
     if(typeof checkSendBtn==='function')checkSendBtn();
     setConnectionState('offline','Menu connection lost. Retry to restore current products and prices.');
     throw err;
   }
 })();
 menuSyncTask=task;
 try{return await task;}finally{if(menuSyncTask===task)menuSyncTask=null;}
}
function renderPaymentChoices(){
 const rows=q('.pay-options');if(!rows)return;
 rows.innerHTML='';
 for(const p of payments){
  const el=document.createElement('div');el.className='pay-option';el.role='button';el.tabIndex=0;
  el.innerHTML='<div class="pay-radio"><div class="pay-radio-dot"></div></div><span class="pay-icon">💳</span><div class="pay-info"><div class="pay-name">'+e(p.name)+'</div><div class="pay-note">'+e(p.instructions)+'</div></div>';
  el.addEventListener('click',()=>setPayment(el,p.name));el.addEventListener('keydown',ev=>{if(ev.key==='Enter')el.click();});rows.appendChild(el);
 }
}
// Exactly one customer checkout. No Messenger, third-party site, or fallback channel.
// A stable idempotency key prevents duplicate orders when the network times out.
function orderKeyFor(payload){
 const signature=JSON.stringify(payload);
 let previous=null;
 try{previous=JSON.parse(sessionStorage.getItem('laluna_pending_order')||'null');}catch{}
 if(previous&&previous.signature===signature&&/^[0-9a-f-]{36}$/i.test(previous.key))return previous.key;
 const key=globalThis.crypto&&crypto.randomUUID?crypto.randomUUID():
   'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g,c=>{const n=(crypto.getRandomValues(new Uint8Array(1))[0]&15);return c==='x'?n.toString(16):((n&3)|8).toString(16);});
 try{sessionStorage.setItem('laluna_pending_order',JSON.stringify({signature,key}));}catch{}
 return key;
}
async function submitOnline(){
 if(orderInFlight)throw Error('Your order is already being submitted');
 if(!ready)throw Error('The restaurant order system is offline; reconnect and retry.');
 const previousCartRevision=cartRevision;
 const changedBeforeCheckout=await refreshLiveMenu(true);
 if(changedBeforeCheckout||cartRevision!==previousCartRevision){
   if(typeof closeConfirmPopup==='function')closeConfirmPopup(true);
   throw Error('The menu was updated. Review your revised cart before confirming the order.');
 }
 if(!cart.length)throw Error('Add at least one product');
 const name=g('custName'),phone=g('custPhone');
 if(name.length<2||!/^[0-9]{10,15}$/.test(phone.replace(/\D/g,'')))throw Error('Enter a valid name and phone number');
 if(!['Dine-In','Take-Out'].includes(curOT))throw Error('Choose Dine-In or Take-Out');
 if(!curPayment)throw Error('Choose a payment method');
 const table=curOT==='Dine-In'?(qrTable||g('llTableNum')):'';
 if(curOT==='Dine-In'&&!/^[A-Za-z0-9_-]{1,12}$/.test(table))throw Error('Enter a valid table number');
 const guests=curOT==='Dine-In'?Number(g('llGuestCount')||1):1;
 if(!Number.isInteger(guests)||guests<1||guests>30)throw Error('Enter a valid guest count (1–30)');
 const payload={name,phone,fulfillment:curOT,table_number:table,guests,payment_method:curPayment,notes:g('custNotes'),address:'',scheduled:'',items:cart.map(x=>({product_id:x.iid,qty:x.qty,mods:x.mods.replace(/&amp;/g,'&').replace(/&#39;/g,"'")}))};
 payload.client_order_key=orderKeyFor(payload);
 orderInFlight=true;
 try{
  const order=await api('/orders',{method:'POST',body:JSON.stringify(payload)});
  if(!order||!order.order_id)throw Error('Restaurant did not confirm the order reference; retry before starting another order.');
  try{sessionStorage.removeItem('laluna_pending_order');localStorage.setItem('laluna_last_order',order.order_id);}catch{}
  if(q('#llTrackID'))q('#llTrackID').value=order.order_id;
  cart=[];updBadge();renderCart();
  return {...order,fulfillment:payload.fulfillment,table_number:payload.table_number};
 }finally{orderInFlight=false;}
}
window.LaLunaSubmitOrder=submitOnline;
async function refreshUpsells(){
 const box=q('#llUpsells');if(!ready||!box||suggestionBusy)return;
 if(!cart.length){box.hidden=true;return;}
 suggestionBusy=true;
 try{
 const ids=[...new Set(cart.map(x=>x.iid))].join(',');const data=await api('/upsells?ids='+encodeURIComponent(ids));
 box.hidden=!data.recommendations.length;
 q('#llUpsellList').innerHTML=data.recommendations.map(r=>'<div class="ll-reco"><span style="font-size:1.5rem">'+e(r.icon)+'</span><div style="flex:1"><strong>'+e(r.name)+'</strong><small>'+e(r.reason)+' · '+money(r.price)+'</small></div><button type="button" data-rec="'+Number(r.id)+'">Add</button></div>').join('');
 q('#llUpsellList').querySelectorAll('button').forEach(btn=>btn.onclick=()=>openModal(Number(btn.dataset.rec)));
 }catch(err){box.hidden=true;}finally{suggestionBusy=false;}
}
async function joinRewards(){
 const name=q('#llLoyaltyName').value,phone=q('#llLoyaltyPhone').value,consent=q('#llConsent').checked;
 const btn=q('#llJoinBtn');btn.disabled=true;
 try{
  const result=await api('/loyalty/join',{method:'POST',body:JSON.stringify({name,phone,consent})});
  localStorage.setItem('laluna_card_token',result.card_token);await refreshRewards();
  q('#llLoyaltyFeedback').textContent='Card created and saved on this browser.';
 }catch(err){q('#llLoyaltyFeedback').textContent=err.message;}finally{btn.disabled=false;}
}
async function refreshRewards(){
 const token=localStorage.getItem('laluna_card_token');if(!token)return;
 try{
  const r=await api('/loyalty/card?token='+encodeURIComponent(token));
  q('#llLoyaltyJoin').hidden=true;q('#llLoyaltyCard').hidden=false;
  q('#llCardName').textContent=r.name;
  q('#llStamps').innerHTML=Array.from({length:8},(_,i)=>'<span aria-label="'+(i<r.stamps%8?'Earned':'Not earned')+'">'+(i<r.stamps%8?'⭐':'☆')+'</span>').join('');
  q('#llLoyaltyStats').textContent=r.stamps+' stamps · '+r.visits+' visits · '+r.available_rewards+' available free drink reward(s). Ask staff to redeem.';
 }catch(err){q('#llLoyaltyFeedback').textContent='Could not load card: '+err.message;}
}
async function trackOrder(){
 const id=q('#llTrackID')?.value.trim().toUpperCase();if(!id)return;
 try{
  const o=await api('/orders/'+encodeURIComponent(id)+'/status');
  q('#llTrackResult').textContent='Order '+o.id+': '+o.status+' · Payment '+o.payment_status+' · '+money(o.total);
 }catch(err){q('#llTrackResult').textContent=err.message;}
}
document.addEventListener('DOMContentLoaded',init);
})();
