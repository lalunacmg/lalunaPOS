(()=>{'use strict';
const $=sel=>document.querySelector(sel),$$=sel=>[...document.querySelectorAll(sel)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const productPhoto=p=>/^\/media\/products\/[a-f0-9]{32}\.webp$/.test(p?.image_url||'')?'<img class="product-photo" src="'+esc(p.image_url)+'" alt="" loading="lazy" decoding="async">':'<span class="product-emoji">'+esc(p?.icon||'🍽️')+'</span>';
const peso=n=>'₱'+Number(n||0).toLocaleString('en-PH',{minimumFractionDigits:2,maximumFractionDigits:2});
const dt=s=>new Date(s).toLocaleString('en-PH',{timeZone:'Asia/Manila',dateStyle:'medium',timeStyle:'short'});
const prefGet=key=>{try{return window.localStorage.getItem(key)}catch{return null}};
const prefSet=(key,value)=>{try{window.localStorage.setItem(key,value)}catch{/* privacy mode: use current field value */}};
let csrf='',user=null,page='overview',allProducts=[],allCategories=[],allOrders=[],allCustomers=[],payMethods=[],posCart=[],saveHandler=null,salesData=null,qrNetwork=null,qrBlobUrl=null,printerData=null,lastPOSOrder=null,ordersRefreshing=false,managedTables=[],employees=[],registerState=null,analyticsView='restaurant',pendingTableSelection=null,lastTableAction='',orderTableFilter=null,posExistingTicket=null,posOriginalCart=[],tableOpenTickets=[];
const request=async(path,options={})=>{
 let hdr={'Content-Type':'application/json',...(options.headers||{})};if(options.method && options.method!=='GET')hdr['X-CSRF-Token']=csrf;
 const r=await fetch('/api'+path,{credentials:'same-origin',...options,headers:hdr});
 let data={};try{data=await r.json();}catch{}
 if(!r.ok){if(r.status===401&&page!=='login')logoutLocal();throw Error(typeof data.detail==='string'?data.detail:'Request failed ('+r.status+')');}
 return data;
};
function notify(msg,error=false){$('#alerts').innerHTML='<div class="toast'+(error?' error':'')+'">'+esc(msg)+'</div>';clearTimeout(notify.timer);notify.timer=setTimeout(()=>$('#alerts').innerHTML='',4000);}
function logoutLocal(){csrf='';user=null;$('#appView').hidden=true;$('#loginView').hidden=false;page='login';}
function loginOK(data){csrf=data.csrf;user=data;$('#appView').hidden=false;$('#loginView').hidden=true;$('#staffWho').textContent=data.name+' · '+data.role;page='';navigate('orders').catch(()=>{});}
$('#loginForm').addEventListener('submit',async ev=>{ev.preventDefault();const btn=$('#loginBtn');btn.disabled=true;$('#loginError').textContent='';const f=new FormData(ev.target);try{let v=await request('/auth/login',{method:'POST',body:JSON.stringify(Object.fromEntries(f))});loginOK(v);}catch(err){$('#loginError').textContent=err.message;}finally{btn.disabled=false;}});
$('#logoutBtn').onclick=async()=>{try{await request('/auth/logout',{method:'POST'});}catch{}logoutLocal();};
$('#tabletLogout').onclick=()=>$('#logoutBtn').click();
async function autoLogin(){try{const me=await request('/auth/me');loginOK(me);}catch{logoutLocal();}}
const PROTECTED_PAGES=new Set(['overview','employees','products','inventory','customers','settings']);
function promptAdminPin(){
 return new Promise(resolve=>{
  const gate=$('#adminGate'),form=$('#adminGateForm'),input=$('#adminGatePin');
  const error=$('#adminGateError'),submit=form.querySelector('[type=submit]');
  let busy=false;
  gate.hidden=false;input.value='';error.textContent='';submit.disabled=false;input.focus();
  const finish=(approved)=>{
   gate.hidden=true;form.onsubmit=null;$('#adminGateCancel').onclick=null;
   input.value='';resolve(approved);
  };
  $('#adminGateCancel').onclick=()=>{if(!busy)finish(false);};
  form.onsubmit=async e=>{
   e.preventDefault();if(busy)return;
   const pin=input.value.trim();
   if(!pin){error.textContent='Enter the admin access code.';input.focus();return;}
   busy=true;submit.disabled=true;error.textContent='Checking admin code...';
   try{
    await request('/admin/access/unlock',{method:'POST',body:JSON.stringify({pin})});
    finish(true);
   }catch(err){
    // Keep the dialog visible for correction. Older releases silently closed it.
    error.textContent=err.message;input.value='';input.focus();
   }finally{busy=false;submit.disabled=false;}
  };
 });
}
async function navigate(p){
 if(PROTECTED_PAGES.has(p)){
  const state=await request('/admin/access/state');
  if(!state.unlocked){
   const approved=await promptAdminPin();if(!approved)return;
  }
 }
 page=p;$$('.panel-page').forEach(s=>s.hidden=s.id!=='p-'+p);$$('.side-nav button').forEach(b=>{const selected=b.dataset.page===p;b.classList.toggle('active',selected);if(selected)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});$('#pageTitle').textContent=({overview:'Business Overview',orders:'Order Management',pos:'Point of Sale',tables:'Tables',register:'Cash Register',employees:'Employees',products:'Product Catalog',inventory:'Inventory',customers:'Customers & Rewards',settings:'Settings & Staff'})[p];$('#breadcrumb').textContent='LA LUNA / '+p.toUpperCase();const fun={overview:loadOverview,orders:loadOrders,pos:loadPOS,tables:loadTables,register:loadRegister,employees:loadEmployees,products:loadProducts,inventory:loadInventory,customers:loadCustomers,settings:loadSettings}[p];const loading=Promise.resolve().then(()=>fun?.()).catch(e=>{notify(e.message,true);throw e;});window.scrollTo(0,0);return loading;}
$$('.side-nav button').forEach(b=>b.onclick=()=>navigate(b.dataset.page).catch(()=>{}));
const table=(heads,rows)=>'<table class="data-table"><thead><tr>'+heads.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.join('')+'</tbody></table>';
function kpi(label,value,hint=''){return '<div class="kpi"><span class="klabel">'+esc(label)+'</span><strong>'+esc(value)+'</strong><small>'+esc(hint)+'</small></div>';}
async function loadOverview(){const d=await request('/admin/overview');salesData=d;
 $('#kpiGrid').innerHTML=kpi('Net sales',peso(d.revenue),'Paid minus refunded')+kpi('Today net',peso(d.today_revenue),'PH time · payment/refund date')+kpi('Paid orders',d.paid_orders,'AOV '+peso(d.average_order))+kpi('Orders to process',d.pending_orders,'Active order queue');
 const a=d.sales_audit||{};
 const confirmed=a.reconciled===true;
 const historic=Number(a.legacy_date_estimates||0), unknown=Number(a.legacy_unreconstructable_refunds||0);
 const auditMessage=confirmed?'Ledger matches current paid orders':'ACTION REQUIRED · Ledger differs from paid order records by '+peso((a.difference_cents||0)/100);
 $('#salesAudit').innerHTML='<div class="sales-audit-title"><strong>'+esc(auditMessage)+'</strong><small>Updated '+esc(dt(a.as_of_utc||new Date().toISOString()))+' · '+esc(a.timezone||'Asia/Manila')+'</small></div>'+
   '<div class="sales-audit-totals"><div>Gross payments<strong>'+peso(a.gross_payments)+'</strong></div><div>Recorded refunds<strong>'+peso(a.refunds)+'</strong></div><div>Net sales<strong>'+peso(a.net_sales)+'</strong></div></div>'+
   "<p class=\"hint\">Source: recorded transactions in this server's SQLite database. New sales and refunds use the actual recording time. "+
   (historic?historic+' historical paid order(s) use approximate payment dates (original order date). ':'')+
   (unknown?unknown+' historical refund(s) predate the ledger and cannot be dated from existing data. ':'')+
   'Only mark orders paid after confirming payment; cash and manual transfers are not independently verified.</p>';
 $('#salesAudit').classList.toggle('audit-warning',!confirmed||!!historic||!!unknown);
 $('#growth').innerHTML='<div class="list-line"><span>Registered customers</span><strong>'+d.customers+'</strong></div><div class="list-line"><span>Joined in past 30 days</span><strong>'+d.new_customers_30d+'</strong></div>';
 $('#lowStock').innerHTML=d.low_stock.length?d.low_stock.map(p=>'<div class="list-line"><span>'+esc(p.name)+'</span><strong>'+p.stock+' remaining</strong></div>').join(''):'<p>No tracked items at or below 5 units.</p>';
 $('#topItems').innerHTML=d.best_sellers.length?d.best_sellers.map((p,i)=>'<div class="list-line"><span>'+(i+1)+'. '+esc(p.name)+'</span><strong>'+p.units+' sold</strong></div>').join(''):'<p>No paid orders yet.</p>';
 const max=Math.max(1,...d.daily.map(x=>x.revenue));
 loadRestaurantAnalytics().catch(e=>notify(e.message,true));
 $('#salesChart').innerHTML=d.daily.length?d.daily.map(x=>'<div class="bar-slot" title="'+esc(x.date)+': '+peso(x.revenue)+'"><div class="bar" style="height:'+Math.max(3,Math.round(x.revenue/max*170))+'px"></div><small>'+esc(x.date.slice(5))+'</small></div>').join(''):'<p style="align-self:center">No paid sales recorded yet.</p>';
}
$('#refreshDash').onclick=()=>loadOverview().catch(e=>notify(e.message,true));
$('#csvExport').onclick=async()=>{
 try{
  const r=await fetch('/api/admin/sales-ledger.csv',{credentials:'same-origin',cache:'no-store'});
  if(!r.ok)throw Error('Could not export the transaction ledger ('+r.status+')');
  const blob=await r.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');
  a.href=url;a.download='laluna-sales-ledger-'+new Date().toISOString().slice(0,10)+'.csv';a.click();
  setTimeout(()=>URL.revokeObjectURL(url),5000);
 }catch(e){notify(e.message,true);}
};
async function openPaymentDialog(order){
 if(!order||order.payment_status!=='unpaid')return notify('Only unpaid tickets can be settled.',true);
 try{
  const [pay,emp]=await Promise.all([request('/admin/payments'),request('/admin/employees')]);
  const methods=pay.payments.filter(x=>x.enabled),activeEmployees=emp.employees.filter(x=>x.active);
  if(!methods.length)throw Error('No enabled payment methods. Ask an administrator to configure Settings.');
  const html='<div class="checkout-flow"><p><strong>'+esc(order.fulfillment)+(order.table_number?' · Table '+esc(order.table_number):'')+'</strong> · '+esc(order.id)+'</p>'+
   '<div class="checkout-line">Subtotal <strong>'+peso(order.subtotal)+'</strong></div>'+
   (order.delivery_fee?'<div class="checkout-line">Delivery <strong>'+peso(order.delivery_fee)+'</strong></div>':'')+
   '<label>Payment type (required)<select class="field" id="checkoutPayment">'+methods.map(m=>'<option value="'+esc(m.name)+'">'+esc(m.name)+'</option>').join('')+'</select></label>'+
   '<label>Discount eligibility<select id="checkoutDiscountKind" class="field"><option value="">No discount</option><option value="Senior">Senior</option><option value="PWD">PWD</option></select></label>'+
   '<div class="input-row"><label>Discount scope<select id="checkoutDiscountScope" class="field"><option value="bill">Total bill (items only)</option><option value="item">Selected item(s)</option></select></label><label>Manual discount (%)<input id="checkoutDiscountPercent" class="field" type="number" min="0" max="100" step="0.01" value="0"/></label></div>'+
   '<div id="checkoutEligibleItems" class="checkout-items" hidden><p class="hint">Choose eligible items for item-level discount:</p>'+order.items.map((i,n)=>'<label class="checkout-item"><input type="checkbox" data-discount-index="'+n+'"/> '+esc(i.qty+' × '+i.name)+' <strong>'+peso(i.total)+'</strong></label>').join('')+'</div>'+
   '<label>Staff member on receipt (optional)<select id="checkoutEmployee" class="field"><option value="">Keep original / unassigned</option>'+activeEmployees.map(e=>'<option value="'+e.id+'">'+esc(e.name)+'</option>').join('')+'</select></label>'+
   '<div class="checkout-line">Discount <strong id="checkoutDiscountValue">₱0.00</strong></div><div class="checkout-line grand">Amount due <strong id="checkoutDue">'+peso(order.total)+'</strong></div>'+
   '<div id="checkoutCashControls"><label>Cash received (₱)<input id="checkoutTender" class="field" type="number" min="0" step="0.01" inputmode="decimal" value="0"/></label><p class="hint">Tap denominations to ADD to cash received; ₱400 is a convenient total preset, not a banknote.</p><div class="tender-buttons">'+[10,20,50,100,200,400,500,1000].map(x=>'<button type="button" class="outline auto" data-tender="'+x+'">₱'+x+'</button>').join('')+'<button type="button" class="primary auto" id="checkoutExact">Exact Amount</button><button type="button" class="outline auto" id="checkoutResetTender">Clear</button></div></div>'+
   '<div class="checkout-line grand">Change <strong id="checkoutChange">₱0.00</strong></div><p class="hint" id="checkoutWarning" role="status">Verify payment actually received before proceeding. Discount does not automatically calculate VAT exemption.</p></div>';
  openDialog('Payment & Checkout · '+order.id,html,async()=>{
   const calculation=updateCheckoutTotals();
   const payment_method=$('#checkoutPayment').value;
   if(calculation.discount_kind&&calculation.percent<=0)throw Error('Enter a discount percentage greater than zero.');
   if(payment_method==='Cash'&&calculation.tender<calculation.total)throw Error('Received cash is less than amount due.');
   if(!window.confirm('Confirm '+payment_method+' payment of '+peso(calculation.total)+' received? This records the ticket as PAID.'))throw Error('Payment not confirmed');
   const payload={payment_method,discount_kind:calculation.discount_kind,discount_scope:calculation.discount_scope,
    discount_percent:calculation.percent,discount_item_indices:calculation.indices,
    tendered_amount:calculation.tender,employee_id:$('#checkoutEmployee').value||null};
   const settled=await request('/admin/orders/'+encodeURIComponent(order.id)+'/settle',{method:'POST',body:JSON.stringify(payload)});
   await loadOrders();
   return 'Paid ticket '+settled.order_id+' · '+peso(settled.total)+(payment_method==='Cash'?' · Change '+peso(settled.change_amount):'');
  });
  $('#saveDialog').textContent='Proceed · Complete Ticket';
  $('#checkoutPayment').value=methods.some(m=>m.name==='Cash')?'Cash':methods[0].name;
  if(order.served_by_employee_id)$('#checkoutEmployee').value=String(order.served_by_employee_id);
  function updateCheckoutTotals(){
   const discount_kind=$('#checkoutDiscountKind').value;
   const discount_scope=discount_kind?$('#checkoutDiscountScope').value:'';
   const percent=discount_kind?Number($('#checkoutDiscountPercent').value):0;
   const indices=$$('[data-discount-index]:checked').map(el=>Number(el.dataset.discountIndex));
   const eligible=discount_scope==='item'?indices.reduce((v,i)=>v+Math.round(Number(order.items[i].total)*100),0):discount_scope==='bill'?Math.round(Number(order.subtotal)*100):0;
   const discount=Number.isFinite(percent)&&percent>=0&&percent<=100?Math.round(eligible*percent/100):0;
   const totalCents=Math.max(0,Math.round(Number(order.subtotal)*100)+Math.round(Number(order.delivery_fee||0)*100)-discount);
   const total=totalCents/100,tender=Number($('#checkoutTender').value||0),cash=$('#checkoutPayment').value==='Cash';
   $('#checkoutEligibleItems').hidden=discount_scope!=='item';$('#checkoutCashControls').hidden=!cash;
   $('#checkoutDiscountValue').textContent=peso(discount/100);$('#checkoutDue').textContent=peso(total);
   $('#checkoutChange').textContent=cash?peso(Math.max(0,tender-total)):'—';
   $('#checkoutWarning').textContent=cash&&tender<total?'Cash received is below the total due.':
    'Review the discount and payment. Senior/PWD VAT treatment and eligibility require separate compliance checks.';
   return {discount_kind,discount_scope,percent,indices,total,tender};
  }
  $('#checkoutPayment').onchange=updateCheckoutTotals;
  $('#checkoutDiscountKind').onchange=updateCheckoutTotals;
  $('#checkoutDiscountScope').onchange=updateCheckoutTotals;
  $('#checkoutDiscountPercent').oninput=updateCheckoutTotals;
  $('#checkoutEligibleItems').onchange=updateCheckoutTotals;
  $('#checkoutTender').oninput=updateCheckoutTotals;
  $$('[data-tender]').forEach(b=>b.onclick=()=>{$('#checkoutTender').value=(Number($('#checkoutTender').value||0)+Number(b.dataset.tender)).toFixed(2);updateCheckoutTotals();});
  $('#checkoutExact').onclick=()=>{$('#checkoutTender').value=updateCheckoutTotals().total.toFixed(2);updateCheckoutTotals();};
  $('#checkoutResetTender').onclick=()=>{$('#checkoutTender').value='0';updateCheckoutTotals();};
  updateCheckoutTotals();
 }catch(err){notify(err.message,true);}
}
function ordersEndpoint(){return '/admin/orders'+(orderTableFilter!==null?'?table_id='+encodeURIComponent(orderTableFilter)+'&limit=500':'');}
async function loadOrders(){
 const d=await request(ordersEndpoint());allOrders=d.orders;
 const scope=$('#tableOrdersScope');
 if(orderTableFilter!==null){
  const t=d.table;
  scope.hidden=false;
  scope.querySelector('[data-table-label]').textContent=t?'Table '+t.number+' · '+t.location+' · '+t.seats+' seats':'Selected table';
  scope.querySelector('[data-table-count]').textContent=d.total+' recorded ticket(s)'+(d.total>d.orders.length?' · showing newest '+d.orders.length:'')+' · historical table numbers retained';
 }else scope.hidden=true;
 paintOrders();setOrdersLiveStatus();
}
function setOrdersLiveStatus(message='Auto-refreshing every 7 seconds'){
 const node=$('#ordersLiveStatus');if(node)node.textContent=message;
}
async function refreshOrdersQuietly(){
 if(page!=='orders'||!user||document.hidden||ordersRefreshing)return;
 // Avoid interrupting an active order edit or changing a status selection.
 if(document.activeElement?.closest('#orderList')||!$('#dialog').hidden)return;
 ordersRefreshing=true;
 try{
  const data=await request(ordersEndpoint());
  const previousIds=new Set(allOrders.map(o=>o.id));
  const signature=items=>JSON.stringify(items.map(o=>[o.id,o.updated,o.status,o.payment_status]));
  if(signature(data.orders)!==signature(allOrders)){
   const added=data.orders.filter(o=>!previousIds.has(o.id));
   allOrders=data.orders;paintOrders();
   if(added.length)notify(added.length+' new order(s) · '+(added[0].fulfillment==='Dine-In'?'Table '+added[0].table_number:'Take-Out'));
  }
  setOrdersLiveStatus('Live · Checked '+new Date().toLocaleTimeString());
 }catch(e){setOrdersLiveStatus('Sync unavailable · Press Refresh');}
 finally{ordersRefreshing=false;}
}

function paintOrders(){const search=$('#orderSearch').value.toLowerCase(),filter=$('#orderFilter').value;
 const orders=allOrders.filter(o=>(filter==='all'||o.status===filter)&&[o.id,o.customer_name,o.phone,o.fulfillment,o.table_number].join(' ').toLowerCase().includes(search));
 $('#orderList').innerHTML=orders.length?table(['Reference / Date','Customer','Items','Fulfillment','Total','Payment','Order status','Actions'],orders.map(o=>'<tr class="order-ticket-row" data-ticket="'+esc(o.id)+'"><td><b>'+esc(o.id)+'</b><br><span class="hint">'+dt(o.created)+'<br>'+esc(o.channel)+(o.served_by_name?' · Served by '+esc(o.served_by_name):'')+'</span></td><td>'+esc(o.customer_name||'Walk-in')+'<br><span class="hint">'+esc(o.phone||'')+'</span></td><td>'+o.items.map(i=>'<div>'+i.qty+'× '+esc(i.name)+(i.mods?' <span class="hint">('+esc(i.mods)+')</span>':'')+'</div>').join('')+'<span class="hint">'+esc(o.notes||'')+'</span></td><td>'+esc(o.fulfillment)+'<br><span class="hint">'+(o.table_number?'<strong class="ll-table-order">Table '+esc(o.table_number)+'</strong><br><small class="hint">'+esc(o.guests||1)+' diner(s)</small>':esc(o.address||o.scheduled||(o.fulfillment==='Take-Out'?'Counter collection':'')))+'</span></td><td><b>'+peso(o.total)+'</b></td><td><span class="status '+esc(o.payment_status)+'">'+esc(o.payment_status)+'</span><br><small>'+esc(o.payment_method)+'</small>'+(o.discount_amount?'<br><small class="hint">Discount −'+peso(o.discount_amount)+'</small>':'')+'</td><td><span class="status '+esc(o.status)+'">'+esc(o.status)+'</span><br><select data-action="status" data-id="'+esc(o.id)+'">'+['pending','accepted','preparing','ready','completed','delivered','cancelled'].map(x=>'<option value="'+x+'"'+(o.status===x?' selected':'')+'>'+x+'</option>').join('')+'</select></td><td><div class="order-actions">'+(o.payment_status==='unpaid'&&o.status!=='cancelled'?'<button class="ticket-pay-btn" type="button" data-action="settle" data-id="'+esc(o.id)+'">Pay / Complete</button>':'')+'<button type="button" data-action="print" data-id="'+esc(o.id)+'">Print Receipt</button><button type="button" data-action="bleprint" data-id="'+esc(o.id)+'">Bluetooth LE</button><button type="button" data-action="detail" data-id="'+esc(o.id)+'">Details</button></div></td></tr>')):'<p>No matching orders.</p>';
 $('#orderList').querySelectorAll('select').forEach(el=>el.onchange=async()=>{
  const o=allOrders.find(x=>x.id===el.dataset.id);if(!o)return;
  const status=el.dataset.action==='status'?el.value:o.status;
  const payment_status=o.payment_status;
  if((status==='completed'&&o.payment_status==='unpaid')||(status==='delivered'&&o.payment_status==='unpaid')){paintOrders();return openPaymentDialog(o);}
  if((status==='cancelled'&&!confirm('Cancel this order? Inventory will be returned if deducted.'))){paintOrders();return;}
  try{await request('/admin/orders/'+encodeURIComponent(o.id),{method:'PATCH',body:JSON.stringify({status,payment_status})});notify('Order '+o.id+' updated');await loadOrders();}catch(e){notify(e.message,true);paintOrders();}
 });
 $('#orderList').querySelectorAll('[data-action="settle"]').forEach(btn=>btn.onclick=()=>{
  const o=allOrders.find(x=>x.id===btn.dataset.id);if(o)openPaymentDialog(o);
 });
 $('#orderList').querySelectorAll('[data-ticket]').forEach(row=>row.onclick=e=>{
  if(e.target.closest('button,select,input,a'))return;
  const o=allOrders.find(x=>x.id===row.dataset.ticket);
  if(o?.payment_status==='unpaid'&&o.status!=='cancelled')openPaymentDialog(o);
 });
 $('#orderList').querySelectorAll('[data-action="print"]').forEach(btn=>btn.onclick=()=>openReceipt(btn.dataset.id));
 $('#orderList').querySelectorAll('[data-action="bleprint"]').forEach(btn=>btn.onclick=()=>printViaBle(btn.dataset.id).catch(e=>notify(e.message,true)));
 $('#orderList').querySelectorAll('[data-action="directprint"]').forEach(btn=>btn.onclick=()=>directPrintOrder(btn.dataset.id).catch(e=>notify(e.message,true)));
 $('#orderList').querySelectorAll('[data-action="detail"]').forEach(btn=>btn.onclick=()=>{const o=allOrders.find(x=>x.id===btn.dataset.id);openDialog('Order '+o.id,'<div class="list-line"><span>Customer</span><strong>'+esc(o.customer_name)+' · '+esc(o.phone)+'</strong></div><div class="list-line"><span>Fulfillment</span><strong>'+esc(o.fulfillment)+'</strong></div><div class="list-line"><span>Address / Table</span><strong>'+esc(o.address||o.table_number||o.scheduled||'—')+'</strong></div><div class="list-line"><span>Diners</span><strong>'+esc(o.fulfillment==='Dine-In'?(o.guests||1):'—')+'</strong></div><div class="list-line"><span>Payment</span><strong>'+esc(o.payment_method)+'</strong></div><div class="list-line"><span>Notes</span><strong>'+esc(o.notes)+'</strong></div>'+o.items.map(x=>'<div class="list-line"><span>'+x.qty+'× '+esc(x.name)+'<br><span class="hint">'+esc(x.mods)+'</span></span><strong>'+peso(x.total)+'</strong></div>').join('')+(o.fulfillment==='Delivery'?'<hr><h3>Local delivery fee</h3><div class="input-row"><input id="llFeeInput" class="field" type="number" min="0" max="5000" step="0.01" value="'+o.delivery_fee+'" style="flex:1"><button id="llSaveFee" class="outline auto">Update Fee</button></div><p class="hint">Confirm fee with customer before marking paid.</p>':'')+'<div class="pos-total"><span>Total</span><strong>'+peso(o.total)+'</strong></div><div class="printer-actions"><button id="llPreviewReceipt" class="outline">Preview / Print Receipt</button><button id="llDirectReceipt" class="outline">Send from Windows Server</button><button id="llBleReceipt" class="outline">Print via Connected Bluetooth LE</button></div>',null);
 $('#llPreviewReceipt').onclick=()=>openReceipt(o.id);$('#llDirectReceipt').onclick=()=>directPrintOrder(o.id).catch(e=>notify(e.message,true));$('#llBleReceipt').onclick=()=>printViaBle(o.id).catch(e=>notify(e.message,true));
 $('#llDirectReceipt').hidden=!printerData?.capabilities?.windows;
 if(o.fulfillment==='Delivery'){
   $('#llSaveFee').onclick=async()=>{try{await request('/admin/orders/'+o.id,{method:'PATCH',body:JSON.stringify({delivery_fee:Number($('#llFeeInput').value)})});closeDialog();notify('Delivery fee updated');loadOrders();}catch(e){notify(e.message,true);}};
 }
 });
}
$('#orderSearch').oninput=paintOrders;$('#orderFilter').onchange=paintOrders;$('#refreshOrders').onclick=()=>loadOrders().catch(e=>notify(e.message,true));
$('#clearTableOrders').onclick=()=>{orderTableFilter=null;$('#orderSearch').value='';$('#orderFilter').value='all';loadOrders().catch(e=>notify(e.message,true));};
async function fetchProducts(){let v=await request('/admin/products');allProducts=v.products;return allProducts;}
async function loadCategories(){
 const result=await request('/admin/categories');allCategories=result.categories;
 const root=$('#categoryList');
 root.innerHTML=allCategories.length?allCategories.map(c=>
  '<div class="category-chip"><span>'+esc(c.name)+' <small>('+c.product_count+' item'+(c.product_count===1?'':'s')+')</small></span><button type="button" class="outline" data-category-rename="'+c.id+'" aria-label="Rename '+esc(c.name)+'">Rename</button></div>').join(''):
  '<p class="hint">No categories yet. Create one to start.</p>';
 root.querySelectorAll('[data-category-rename]').forEach(b=>b.onclick=()=>editCategory(allCategories.find(c=>c.id===Number(b.dataset.categoryRename))));
}
async function loadProducts(){await Promise.all([fetchProducts(),loadCategories()]);paintProducts();}
function paintProducts(){
 const search=$('#productSearch').value.toLowerCase();
 const ps=allProducts.filter(p=>[p.name,p.category].join(' ').toLowerCase().includes(search));
 $('#productList').innerHTML=table(['Product','Category','Price','Stock','Availability','Customer menu','Edit'],ps.map(p=>{
   const avail=p.active?'POS available':'Inactive';
   const customer=p.active?(p.customer_visible?'Customer + POS':'POS only'):(p.customer_visible?'Hidden (inactive)':'Backend only (inactive)');
   return '<tr><td><span class="product-name-cell">'+productPhoto(p)+' <b>'+esc(p.name)+'</b></span></td><td>'+esc(p.category)+'</td><td>'+peso(p.price)+'</td><td>'+esc(p.stock===null?'Unlimited':p.stock)+'</td><td><span class="status '+(p.active?'accepted':'cancelled')+'">'+avail+'</span></td><td>'+esc(customer)+'</td><td><button data-edit="'+p.id+'">Edit</button></td></tr>';
 }));
 $('#productList').querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>editProduct(allProducts.find(p=>p.id===+b.dataset.edit)));
}
$('#productSearch').oninput=paintProducts;
$('#newProduct').onclick=()=>editProduct(null);
$('#newCategory').onclick=()=>editCategory(null);
function openDialog(title,html,handler){$('#saveDialog').textContent='Save Changes';$('#dialogTitle').textContent=title;$('#dialogBody').innerHTML=html;saveHandler=handler;$('#saveDialog').hidden=!handler;$('#dialog').hidden=false;}
function closeDialog(){$('#dialog').hidden=true;saveHandler=null;}
$('#closeDialog').onclick=closeDialog;$('#cancelDialog').onclick=closeDialog;$('#dialog').addEventListener('click',e=>{if(e.target.id==='dialog')closeDialog();});
$('#saveDialog').onclick=async()=>{if(!saveHandler)return;const b=$('#saveDialog');b.disabled=true;try{const message=await saveHandler();closeDialog();notify(message||'Saved successfully');}catch(e){notify(e.message,true);}finally{b.disabled=false;}};
function editCategory(category){
 const body='<form id="categoryForm"><label>Category name<input class="field" name="name" required maxlength="60" autocomplete="off" placeholder="e.g. Seasonal Specials" value="'+esc(category?.name||'')+'"/></label><p class="hint">Renaming an existing category moves its current items together. Historical order receipts retain their original category details.</p></form>';
 openDialog(category?'Rename Category':'Create Category',body,async()=>{
   const name=$('#categoryForm').elements.name.value.trim();
   await request(category?'/admin/categories/'+category.id:'/admin/categories',{
     method:category?'PATCH':'POST',body:JSON.stringify({name})});
   await loadProducts();
 });
}
function editProduct(p){
 const input=(label,key,type='text')=>'<label>'+label+'<input class="field" name="'+key+'" type="'+type+'" value="'+esc(p?.[key]??'')+'"></label>';
 const catOptions='<option value="">Select a category</option>'+allCategories.map(c=>'<option value="'+esc(c.name)+'"'+(c.name.toLowerCase()===(p?.category||'').toLowerCase()?' selected':'')+'>'+esc(c.name)+'</option>').join('');
 let editingId=p?.id||null;
 const photoUrl=p?.image_url||'';
 const photoBlock='<div class="product-photo-editor"><div class="product-photo-preview">'+(photoUrl?'<img id="productImagePreview" src="'+esc(photoUrl)+'" alt="Product photo preview">':'<img id="productImagePreview" alt="Product photo preview" hidden>')+
  '<span id="productPhotoFallback"'+(photoUrl?' hidden':'')+'>'+esc(p?.icon||'🍽️')+'</span></div>'+
  '<div class="product-photo-controls"><strong>Menu photo</strong><label for="productImageFile">Upload / replace photo<input class="field" id="productImageFile" type="file" accept="image/jpeg,image/png,image/webp,image/gif,image/heic,image/heif,.heic,.heif"></label>'+
  '<p class="hint">JPG, PNG, WebP, GIF or iPhone HEIC · Max 8 MB. Images are resized automatically. Optional: the emoji is used if no photo is uploaded.</p>'+
  (p?.image_url?'<button class="outline auto" type="button" id="removeProductImage">Remove current photo</button>':'')+'</div></div>';
 const body='<form id="productForm">'+photoBlock+input('Product name','name')+
  '<label>Category<select class="field" name="category" required>'+catOptions+'</select></label><p class="hint">Need a different category? Close this form and select + New Category.</p>'+
  '<div class="input-row"><div style="flex:1">'+input('Icon / emoji','icon')+'</div><div style="flex:1">'+input('Price ₱','price','number')+'</div><div style="flex:1">'+input('Cost ₱','cost','number')+'</div></div>'+
  '<label>Description<textarea name="description" class="field">'+esc(p?.description||'')+'</textarea></label>'+input('Current stock (blank = unlimited)','stock','number')+
  '<div class="product-visibility"><label><input type="checkbox" name="active"'+(!p||p.active?' checked':'')+'> Available for staff POS</label><label><input type="checkbox" name="customer_visible"'+(!p||p.customer_visible?' checked':'')+'> Show on customer QR menu</label><p class="hint">For back-office-only items, keep POS enabled and uncheck customer visibility. Unchecking POS availability makes this item unavailable everywhere.</p></div>'+
  '<label><input type="checkbox" name="popular"'+(p?.popular?' checked':'')+'> Bestseller</label><label><input type="checkbox" name="dual"'+(p?.dual?' checked':'')+'> Two flavors (e.g. wings)</label>'+input('Bundle flavor count (0 = normal)','bundle_qty','number')+
  '<label>Variants JSON <span class="hint">[{"l":"Iced","p":120}]</span><textarea class="field" name="variants">'+esc(JSON.stringify(p?.variants||[],null,2))+'</textarea></label><label>Add-ons JSON <span class="hint">[{"name":"Pearl","price":15}]</span><textarea name="addons" class="field">'+esc(JSON.stringify(p?.addons||[],null,2))+'</textarea></label></form>';
 openDialog(p?'Edit '+p.name:'Create Product',body,async()=>{
   const f=$('#productForm');if(!f.elements.category.value)throw Error('Select a category or create one first');
   let d=Object.fromEntries(new FormData(f));for(const x of ['active','customer_visible','popular','dual'])d[x]=f.elements[x].checked;
   d.price=Number(d.price);d.cost=Number(d.cost);d.stock=d.stock===''?null:Number(d.stock);d.bundle_qty=Number(d.bundle_qty)||0;
   try{d.variants=JSON.parse(d.variants);d.addons=JSON.parse(d.addons);}catch{throw Error('Variants and add-ons must be valid JSON arrays');}
   // Keep the created product ID on retry so a failed photo upload cannot
   // accidentally create duplicate products if staff press Save again.
   const result=await request(editingId?'/admin/products/'+editingId:'/admin/products',{method:editingId?'PUT':'POST',body:JSON.stringify(d)});
   if(!editingId)editingId=result.id;
   const photo=$('#productImageFile').files[0];
   if(photo){
    const kind=photo.type||({'heic':'image/heic','heif':'image/heif','jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png','webp':'image/webp','gif':'image/gif'}[photo.name.toLowerCase().split('.').pop()]||'');
    if(!['image/jpeg','image/png','image/webp','image/gif','image/heic','image/heif'].includes(kind))throw Error('Product saved, but please select a JPG, PNG, WebP, GIF or HEIC photo.');
    if(photo.size>8*1024*1024)throw Error('Product saved, but the image is over 8 MB. Choose a smaller photo.');
    try{await request('/admin/products/'+editingId+'/image',{method:'PUT',body:photo,headers:{'Content-Type':kind}});}
    catch(error){throw Error('Product saved; photo upload failed: '+error.message+'. Choose the image again and retry Save.');}
   }
   await loadProducts();
 });
 const fileInput=$('#productImageFile'),preview=$('#productImagePreview'),fallback=$('#productPhotoFallback');
 let previewURL='';
 fileInput.addEventListener('change',()=>{
  if(previewURL)URL.revokeObjectURL(previewURL);
  const file=fileInput.files[0];
  if(!file){preview.hidden=!photoUrl;preview.src=photoUrl;fallback.hidden=!!photoUrl;return;}
  if(file.size>8*1024*1024){notify('Maximum image size is 8 MB',true);fileInput.value='';return;}
  previewURL=URL.createObjectURL(file);
  preview.src=previewURL;preview.hidden=false;fallback.hidden=true;
 });
 preview.addEventListener('error',()=>{preview.hidden=true;fallback.hidden=false;});
 if(p?.image_url){
  $('#removeProductImage').onclick=async()=>{
   if(!confirm('Remove this product photo? The emoji will show instead.'))return;
   try{await request('/admin/products/'+p.id+'/image',{method:'DELETE'});fileInput.value='';preview.hidden=true;preview.removeAttribute('src');fallback.hidden=false;await loadProducts();notify('Product photo removed');}
   catch(err){notify(err.message,true);}
  };
 }
}
async function loadInventory(){await fetchProducts();$('#inventoryList').innerHTML=table(['Product','Stock','Status','Adjustment'],allProducts.map(p=>'<tr><td>'+esc(p.icon)+' '+esc(p.name)+'</td><td>'+esc(p.stock===null?'Unlimited':p.stock)+'</td><td><span class="status '+(p.stock!==null&&p.stock<=5?'cancelled':'accepted')+'">'+(p.stock===null?'Untracked':p.stock<=5?'Low / Out':'Available')+'</span></td><td>'+((p.stock!==null)?'<button data-stock="'+p.id+'">Adjust stock</button>':'<button data-stockinit="'+p.id+'">Start tracking</button>')+'</td></tr>'));
 $$('[data-stock]').forEach(b=>b.onclick=()=>{const p=allProducts.find(x=>x.id===+b.dataset.stock);const amt=prompt('Stock adjustment (+ received, - used) for '+p.name,'10');if(amt===null)return;const change=Number(amt);const note=prompt('Reason / stock movement note','Stock received')||'Manual adjustment';request('/admin/inventory/'+p.id,{method:'POST',body:JSON.stringify({change,note})}).then(()=>{notify('Stock adjusted');loadInventory();}).catch(e=>notify(e.message,true));});
 $$('[data-stockinit]').forEach(b=>b.onclick=()=>{const p=allProducts.find(x=>x.id===+b.dataset.stockinit);const val=prompt('Enter current quantity for '+p.name,'20');if(val===null)return;request('/admin/products/'+p.id,{method:'PUT',body:JSON.stringify({...p,stock:Number(val)})}).then(()=>{notify('Stock tracking enabled');loadInventory();}).catch(e=>notify(e.message,true));});}
async function loadCustomers(){const d=await request('/admin/customers');allCustomers=d.customers;paintCustomers();}
function paintCustomers(){const term=$('#customerSearch').value.toLowerCase();const cs=allCustomers.filter(x=>[x.name,x.phone].join(' ').toLowerCase().includes(term));$('#customerList').innerHTML=table(['Customer','Phone','Joined','Visits','Stamps','Lifetime','Actions'],cs.map(c=>'<tr><td>'+esc(c.name)+'</td><td>'+esc(c.phone)+'</td><td>'+dt(c.joined)+'</td><td>'+c.visits+'</td><td><span class="status accepted">⭐ '+c.stamps+'</span></td><td>'+peso(c.lifetime_spend)+'</td><td>'+(c.stamps>=8?'<button data-redeem="'+c.id+'">Redeem Free Drink</button>':'<span class="hint">Needs 8 stamps</span>')+'</td></tr>'));
 $$('[data-redeem]').forEach(b=>b.onclick=async()=>{const c=allCustomers.find(x=>x.id===+b.dataset.redeem);if(!confirm('Redeem 8 stamps for a free drink for '+c.name+'?'))return;try{let r=await request('/admin/loyalty/redeem/'+c.id,{method:'POST'});notify(r.reward);loadCustomers();}catch(e){notify(e.message,true);}});}
$('#customerSearch').oninput=paintCustomers;
async function loadPOS(){
 await fetchProducts();await loadPosTableOptions();
 try{printerData=await request('/admin/printers');}catch{}
 const v=await request('/admin/payments');payMethods=v.payments.filter(x=>x.enabled);
 $('#posPayment').innerHTML='<option value="Pending selection">Pending selection</option>';
 const employeeData=await request('/admin/employees');employees=employeeData.employees;
 const existingEmployee=$('#posEmployee').value;
 $('#posEmployee').innerHTML='<option value="">No staff selected (optional)</option>'+employees.filter(x=>x.active).map(x=>'<option value="'+x.id+'">'+esc(x.name)+'</option>').join('');
 if(employees.some(e=>e.active&&String(e.id)===existingEmployee))$('#posEmployee').value=existingEmployee;
 const categories=[...new Set(allProducts.filter(p=>p.active).map(p=>p.category))].sort((a,b)=>a.localeCompare(b));
 const existingCategory=$('#posCategory').value;
 $('#posCategory').innerHTML='<option value="">All menu categories</option>'+categories.map(c=>'<option value="'+esc(c)+'">'+esc(c)+'</option>').join('');
 if(categories.includes(existingCategory))$('#posCategory').value=existingCategory;
 paintPOSProducts();paintPOSCart();showPOSCashHelp();await loadTableOpenTickets();
}
function paintPOSProducts(){
 const s=$('#posSearch').value.toLowerCase().trim(),category=$('#posCategory').value;
 const visible=allProducts.filter(p=>p.active&&(p.stock===null||p.stock>0)&&(!category||p.category===category)&&
     (p.name.toLowerCase().includes(s)||p.category.toLowerCase().includes(s)));
 $('#posProducts').innerHTML=visible.map(p=>'<button class="pos-tile" data-pos="'+p.id+'"><span class="pos-product-media">'+productPhoto(p)+'</span><b>'+esc(p.name)+'</b><small class="hint">'+esc(p.category)+'</small><em>'+peso(p.price)+'</em></button>').join('')||'<p>No products in this category match your search.</p>';
 $$('[data-pos]').forEach(b=>b.onclick=()=>addToPOS(+b.dataset.pos));
}
$('#posSearch').oninput=paintPOSProducts;$('#posCategory').onchange=paintPOSProducts;
function addToPOS(id){const p=allProducts.find(p=>p.id===id);let mods='',up=p.price;if(p.bundle_qty){notify('Bundle options are handled in customer checkout; add as separate normal product in POS.',true);return;}
 if(p.variants?.length){const raw=prompt('Select a variant for '+p.name+'\n'+p.variants.map((v,i)=>(i+1)+'. '+v.l+' · '+peso(v.p)).join('\n'),'1');if(raw===null)return;const v=p.variants[Number(raw)-1];if(!v){notify('Invalid variant',true);return;}mods=v.l;up=v.p;}
 if(p.dual){const opts=p.variants.map((v,i)=>(i+1)+'. '+v.l).join('\n');const a=prompt('First wing flavor:\n'+opts,'1');if(a===null)return;const b=prompt('Second wing flavor:\n'+opts,'2');if(b===null)return;if(!p.variants[Number(a)-1]||!p.variants[Number(b)-1]){notify('Invalid flavor',true);return;}mods=p.variants[Number(a)-1].l+' + '+p.variants[Number(b)-1].l;up=p.price;}
 if(p.addons?.length){const a=prompt('Optional add-ons: enter comma-separated numbers; blank for none\n'+p.addons.map((v,i)=>(i+1)+'. '+v.name+' · '+peso(v.price)).join('\n'),'');if(a===null)return;if(a.trim()){const picks=[...new Set(a.split(',').map(t=>Number(t.trim())))];if(picks.some(n=>!p.addons[n-1])){notify('Invalid add-on',true);return;}const chosen=picks.map(n=>p.addons[n-1]);mods+=(mods?', ':'')+chosen.map(x=>x.name).join(', ');up+=chosen.reduce((s,x)=>s+x.price,0);}}
 const found=posCart.find(x=>x.product_id===id&&x.mods===mods);if(found)found.qty++;else posCart.push({product_id:id,name:p.name,icon:p.icon,mods,qty:1,unit_price:up});paintPOSCart();}
function paintPOSCart(){const container=$('#posCart');container.innerHTML=posCart.length?posCart.map((x,i)=>'<div class="pos-row"><div><b>'+esc(x.name)+'</b><div class="hint">'+esc(x.mods)+' · '+peso(x.unit_price)+'</div></div><div><button data-posdec="'+i+'">−</button> '+x.qty+' <button data-posinc="'+i+'">+</button></div><strong>'+peso(x.unit_price*x.qty)+'</strong></div>').join(''):'<p>No products in the sale yet.</p>';const base=posCart.reduce((s,x)=>s+x.unit_price*x.qty,0);const fee=$('#posType').value==='Delivery'?Number($('#posDeliveryFee')?.value||0):0;$('#posTotal').textContent=peso(base+(Number.isFinite(fee)?fee:0));$$('[data-posinc]').forEach(b=>b.onclick=()=>{posCart[+b.dataset.posinc].qty++;paintPOSCart();});$$('[data-posdec]').forEach(b=>b.onclick=()=>{let i=+b.dataset.posdec;posCart[i].qty--;if(posCart[i].qty<=0)posCart.splice(i,1);paintPOSCart();});}
function showPOSCashHelp(){const el=$('#posCashHelp');if(el)el.textContent='Payment method, Senior/PWD discount and cash change are processed from Orders → Pay / Complete.';}
function posOrderPayload(){
 const fulfillment=$('#posType').value;
 const table=fulfillment==='Dine-In'?$('#posTable').value:'';
 const guests=fulfillment==='Dine-In'?Number($('#posGuests').value):1;
 if(!['Dine-In','Take-Out','Delivery'].includes(fulfillment))throw Error('Choose Dine-In, Take-Out or Delivery');
 if(fulfillment==='Dine-In'){
  if(!table)throw Error('Select an active table first. Configure it under Tables if necessary.');
  const configured=managedTables.find(t=>t.active&&t.number===table);
  if(!configured)throw Error('Selected table is not active. Refresh the POS table list.');
  if(!Number.isInteger(guests)||guests<1||guests>configured.seats)throw Error('Enter a valid guest count (1–'+configured.seats+') for '+table+'.');
 }
 const method='Pending selection';
 const address=fulfillment==='Delivery'?$('#posDeliveryAddress').value.trim():'';
 const fee=fulfillment==='Delivery'?Number($('#posDeliveryFee').value):0;
 if(fulfillment==='Delivery'){
  if(address.length<5)throw Error('Enter the full delivery address.');
  if(!$('#posName').value.trim())throw Error('Enter the recipient name for delivery.');
  if($('#posPhone').value.replace(/\D/g,'').length<10)throw Error('Enter a valid delivery contact number.');
  if(!Number.isFinite(fee)||fee<0||fee>5000)throw Error('Delivery fee must be PHP 0–5,000.');
 }
 return {name:$('#posName').value,phone:$('#posPhone').value,employee_id:$('#posEmployee').value||null,fulfillment,
   table_number:table,guests,payment_method:method,address,delivery_fee:fee,notes:$('#posAddress').value,
   items:posCart.map(x=>({product_id:x.product_id,qty:x.qty,mods:x.mods}))};
}
function keyOfItem(x){return String(x.product_id)+'\u001f'+String(x.mods||'');}
function tableTicketChanges(){
 const orig=new Map(),current=new Map(),specs=new Map();
 for(const x of posOriginalCart){const k=keyOfItem(x);orig.set(k,(orig.get(k)||0)+Number(x.qty));specs.set(k,x);}
 for(const x of posCart){const k=keyOfItem(x);current.set(k,(current.get(k)||0)+Number(x.qty));specs.set(k,x);}
 return [...new Set([...orig.keys(),...current.keys()])].map(k=>({product_id:specs.get(k).product_id,
   mods:specs.get(k).mods||'',delta:(current.get(k)||0)-(orig.get(k)||0)})).filter(x=>x.delta!==0);
}
function selectExistingTicket(){
 const id=$('#posOpenTicket').value;
 posExistingTicket=tableOpenTickets.find(o=>o.id===id)||null;
 posOriginalCart=posExistingTicket?posExistingTicket.items.map(i=>({...i})):[];
 posCart=posOriginalCart.map(i=>({...i}));
 if(posExistingTicket){
  $('#posPayment').value='Pending selection';
  $('#posGuests').value=posExistingTicket.guests||1;
  $('#posName').value=posExistingTicket.customer_name||'';
  $('#posPhone').value=posExistingTicket.phone||'';
  $('#posAddress').value=posExistingTicket.notes||'';
  $('#posEmployee').value=posExistingTicket.served_by_employee_id||'';
 }
 $('#posTicketHint').textContent=posExistingTicket?
  'EDITING '+posExistingTicket.id+' · Add/remove items then save to THIS SAME ticket. Existing item prices are preserved. Paid or closed tickets cannot be changed.':
  'NEW ticket selected. Existing table orders are NOT merged automatically unless you select one above.';
 $('#sendTableOrder').textContent=posExistingTicket?'Update This Table Ticket':'Send Order to Table · Unpaid';
 $('#completeSale').textContent=posExistingTicket?'Update Ticket & Go to Payment':'Send to Orders for Payment';
 paintPOSCart();
}
async function loadTableOpenTickets(){
 const row=$('#posExistingTicketRow');
 if($('#posType').value!=='Dine-In'||!$('#posTable').value){row.hidden=true;posExistingTicket=null;posOriginalCart=[];return;}
 const tab=managedTables.find(x=>x.active&&x.number===$('#posTable').value);
 if(!tab){row.hidden=true;return;}
 const selectedTable=tab.id;
 const d=await request('/admin/orders?table_id='+encodeURIComponent(tab.id)+'&limit=500');
 if($('#posType').value!=='Dine-In'||!managedTables.some(x=>x.id===selectedTable&&x.number===$('#posTable').value))return;
 tableOpenTickets=d.orders.filter(o=>o.payment_status==='unpaid'&&o.fulfillment==='Dine-In'&&
   ['pending','accepted','preparing','ready'].includes(o.status));
 const currentId=posExistingTicket?.id;
 $('#posOpenTicket').innerHTML='<option value="">'+(tableOpenTickets.length?'Create a separate NEW ticket (only if needed)':'Create the FIRST table ticket')+'</option>'+
  tableOpenTickets.map(o=>'<option value="'+esc(o.id)+'">'+esc(o.id)+' · '+peso(o.total)+' · '+esc(o.status)+' · '+o.items.length+' item line(s)</option>').join('');
 row.hidden=false;
 $('#posOpenTicket').value=(currentId&&tableOpenTickets.some(o=>o.id===currentId))?currentId:(tableOpenTickets[0]?.id||'');
 selectExistingTicket();
}
$('#posTable').addEventListener('change',()=>loadTableOpenTickets().catch(e=>notify(e.message,true)));
$('#posOpenTicket').addEventListener('change',selectExistingTicket);
function ticketUpdatePayload(payload,complete_paid){
 return {expected_updated:posExistingTicket.updated,changes:tableTicketChanges(),guests:payload.guests,
  name:payload.name,phone:payload.phone,notes:payload.notes,employee_id:payload.employee_id,
  payment_method:payload.payment_method,complete_paid};
}
function showTableAction(text){
 lastTableAction=text;const node=$('#tableActionResult');node.hidden=false;node.textContent=text;
}
$('#completeSale').onclick=async()=>{
 if(!posCart.length)return notify('Add a product first',true);
 let payload;try{payload=posOrderPayload();}catch(e){return notify(e.message,true);}
 const btn=$('#completeSale'),other=$('#sendTableOrder');btn.disabled=true;other.disabled=true;
 try{
  const r=posExistingTicket?await request('/admin/table-orders/'+encodeURIComponent(posExistingTicket.id)+'/items',{method:'PATCH',body:JSON.stringify(ticketUpdatePayload(payload,false))}):await request('/admin/pos-tickets',{method:'POST',body:JSON.stringify(payload)});
  posCart=[];paintPOSCart();$('#posName').value='';$('#posPhone').value='';$('#posAddress').value='';
  $('#posDeliveryAddress').value='';$('#posDeliveryFee').value='0';posExistingTicket=null;posOriginalCart=[];
  orderTableFilter=null;$('#orderSearch').value='';$('#orderFilter').value='all';
  await navigate('orders');
  const selected=allOrders.find(o=>o.id===r.order_id);
  notify('Ticket '+r.order_id+' saved unpaid. Select payment and complete in Orders.');
  if(selected)openPaymentDialog(selected);
 }catch(e){notify(e.message,true);}finally{btn.disabled=false;other.disabled=false;}
};
$('#sendTableOrder').onclick=async()=>{
 if(!posCart.length)return notify('Add products to the table first',true);
 let payload;try{payload=posOrderPayload();}catch(e){return notify(e.message,true);}
 if(payload.fulfillment!=='Dine-In')return notify('Select Dine-In to send an unpaid order to a table.',true);
 const ticket={...payload};
 if(!confirm('Send an UNPAID order to '+ticket.table_number+'? It will appear in Tables and Orders.'))return;
 const btn=$('#sendTableOrder'),other=$('#completeSale');btn.disabled=true;other.disabled=true;
 try{
  const r=posExistingTicket?await request('/admin/table-orders/'+encodeURIComponent(posExistingTicket.id)+'/items',{method:'PATCH',body:JSON.stringify(ticketUpdatePayload(ticket,false))}):await request('/admin/table-orders',{method:'POST',body:JSON.stringify(ticket)});
  posCart=[];paintPOSCart();$('#posName').value='';$('#posPhone').value='';$('#posAddress').value='';
  const result='UNPAID ticket '+r.order_id+(posExistingTicket?' UPDATED for ':' assigned to table ')+ticket.table_number+'. Open Tables to see it, or View Orders for payment.';
  showTableAction(result);posExistingTicket=null;posOriginalCart=[];navigate('tables').catch(()=>{});notify('Table '+ticket.table_number+' ticket saved');
 }catch(e){notify(e.message,true);}finally{btn.disabled=false;other.disabled=false;}
};
async function loadSettings(){await loadQRNetwork();await loadPrinterSettings();const pay=await request('/admin/payments');payMethods=pay.payments;$('#paymentList').innerHTML=payMethods.map(p=>'<div class="list-line"><span>'+esc(p.name)+'<br><small class="hint">'+esc(p.instructions)+'</small></span><button class="outline" data-payment="'+p.id+'">'+(p.enabled?'Disable':'Enable')+'</button></div>').join('');$$('[data-payment]').forEach(b=>b.onclick=()=>{const p=payMethods.find(x=>x.id===+b.dataset.payment);request('/admin/payments/'+p.id,{method:'PUT',body:JSON.stringify({enabled:!p.enabled})}).then(loadSettings).catch(e=>notify(e.message,true));});if(user.role==='owner'){const s=await request('/admin/staff');paintStaff(s.staff);}else{$('#staffPanel').hidden=true;}}
// Tablet printing is sent from the local browser. Render cannot access tablet Bluetooth.
// Windows RAW / COM printing remains an optional local-server mode.
function openReceipt(orderId){
 const width=prefGet('laluna-paper-width')||$('#printerWidth')?.value||String(printerData?.settings?.paper_width||58);
 const url=orderId?'/api/admin/receipts/'+encodeURIComponent(orderId)+'?paper_width='+encodeURIComponent(width):'/api/admin/receipts/test?paper_width='+encodeURIComponent(width);
 const tab=window.open(url,'_blank');
 if(!tab){notify('Your browser blocked the receipt tab. Tap the Open Receipt link below.',true);const a=document.createElement('a');a.href=url;a.target='_blank';a.rel='noopener';a.textContent='Open Receipt';a.className='outline';a.style.display='inline-block';a.style.margin='8px';$('#alerts').append(a);}
}
async function directPrintOrder(orderId){
 const path=orderId?'/admin/printers/print/'+encodeURIComponent(orderId):'/admin/printers/test';
 const r=await request(path,{method:'POST',body:'{}'});
 notify(r.message||'Receipt sent. Check your printer.');return r;
}

// Web Bluetooth LE is available on compatible Android Chromium browsers over HTTPS.
// NOT generic Bluetooth Classic, and NOT supported by Safari on iPad.
const bleProfiles={
 fff0:{service:'0000fff0-0000-1000-8000-00805f9b34fb',write:'0000fff1-0000-1000-8000-00805f9b34fb'},
 nordic:{service:'6e400001-b5a3-f393-e0a9-e50e24dcca9e',write:'6e400002-b5a3-f393-e0a9-e50e24dcca9e'}
};
let bleDevice=null,bleWrite=null,bleBusy=false;
function bleSupported(){return !!(navigator.bluetooth && window.isSecureContext);}
function tabletPrinterHints(){
 const ios=/iPad|iPhone|iPod/.test(navigator.userAgent)||(/Macintosh/.test(navigator.userAgent)&&navigator.maxTouchPoints>1);
 const android=/Android/i.test(navigator.userAgent);
 $('#bleConnect').disabled=!bleSupported();
 $('#bleProfile').disabled=!bleSupported();
 $('#bleDisconnect').disabled=!bleWrite;
 $('#bleTest').disabled=!bleWrite||bleBusy;
 $('#tabletPrinterHelp').textContent=ios?'iPad: Use Safari Print with an AirPrint-compatible Wi-Fi receipt printer. Bluetooth-only ESC/POS models normally require a separate iOS printer app. Safari does not support Web Bluetooth.'
 :android?(bleSupported()?'Android: Turn on Bluetooth, choose your BLE profile, and tap Connect Bluetooth Printer. For classic Bluetooth printers, pair in Android settings and enable a compatible print-service app.':'Android: Open this site in Chrome over HTTPS to use compatible Bluetooth LE. You can also use Android system printing with a printer service.')
 :'Computer: Use the browser print dialog, or install a Windows ESC/POS printer queue / Bluetooth COM port (local server only).';
}
function bleState(msg,error=false){$('#bleStatus').textContent=msg;$('#bleStatus').classList.toggle('print-error',error);tabletPrinterHints();}
$('#bleProfile').value=prefGet('laluna-ble-profile')||'fff0';
$('#bleProfile').onchange=()=>{prefSet('laluna-ble-profile',$('#bleProfile').value);if(bleDevice?.gatt?.connected)bleDevice.gatt.disconnect();bleWrite=null;bleState('Profile changed. Connect to the printer again.');};
$('#bleConnect').onclick=async()=>{
 if(!bleSupported())return bleState('This browser cannot connect directly to Bluetooth LE printers. Use Print via Device Print Dialog.',true);
 const profile=bleProfiles[$('#bleProfile').value];
 if(!profile)return bleState('Choose a supported printer profile.',true);
 try{
  // requestDevice must be called as a direct result of the Connect button click.
  const device=await navigator.bluetooth.requestDevice({acceptAllDevices:true,optionalServices:[profile.service]});
  const gatt=await device.gatt.connect();
  const service=await gatt.getPrimaryService(profile.service);
  const characteristic=await service.getCharacteristic(profile.write);
  if(!characteristic.properties.write&&!characteristic.properties.writeWithoutResponse)throw Error('Printer does not provide a writable receipt characteristic.');
  bleDevice=device;bleWrite=characteristic;
  device.addEventListener('gattserverdisconnected',()=>{bleWrite=null;bleState('Printer disconnected. Tap Connect Bluetooth Printer again.');});
  bleState('CONNECTED: '+(device.name||'Bluetooth LE printer')+'. Test print before serving customers.');
 }catch(e){bleWrite=null;bleState('Connection failed: '+e.message+'. Try another profile or device printing.',true);}
};
$('#bleDisconnect').onclick=()=>{if(bleDevice?.gatt?.connected)bleDevice.gatt.disconnect();bleWrite=null;bleState('Printer disconnected.');};
async function printViaBle(orderId){
 if(!bleWrite||!bleDevice?.gatt?.connected)throw Error('Connect a compatible Bluetooth LE receipt printer in Settings first.');
 if(bleBusy)throw Error('Printer busy; wait for the current receipt.');
 const width=Number(prefGet('laluna-paper-width')||$('#printerWidth').value)||58;
 const path=orderId?'/receipt-bytes/'+encodeURIComponent(orderId):'/receipt-bytes/test';
 bleBusy=true;tabletPrinterHints();
 try{
  const r=await fetch('/api/admin'+path+'?paper_width='+width,{credentials:'same-origin',cache:'no-store'});
  if(!r.ok)throw Error('Receipt unavailable ('+r.status+'). Sign in again if your session expired.');
  const bytes=new Uint8Array(await r.arrayBuffer());
  // Conservative 20-byte GATT chunks accommodate older ESC/POS BLE printers.
  // Delays reduce device-buffer overrun on inexpensive thermal printers.
  for(let pos=0;pos<bytes.length;pos+=20){
   if(!bleDevice.gatt.connected)throw Error('Printer disconnected during receipt. Check paper and reconnect.');
   const chunk=bytes.slice(pos,pos+20);
   if(bleWrite.properties.writeWithoutResponse&&bleWrite.writeValueWithoutResponse)await bleWrite.writeValueWithoutResponse(chunk);
   else await bleWrite.writeValueWithResponse(chunk);
   await new Promise(done=>setTimeout(done,35));
  }
  bleState('Receipt data sent to '+(bleDevice.name||'printer')+'. Confirm paper printed; Bluetooth transfer is not print confirmation.');
 }finally{bleBusy=false;tabletPrinterHints();}
}
$('#bleTest').onclick=()=>printViaBle(null).catch(e=>bleState(e.message,true));
$('#mobilePrintTest').onclick=()=>openReceipt(null);
tabletPrinterHints();

$('#printLastReceipt').onclick=()=>{if(lastPOSOrder)openReceipt(lastPOSOrder);};
function printerControlStatus(data){
 const c=data.capabilities,method=$('#printerMethod').value;
 const deviceList=method==='windows_raw'?c.queues:method==='bluetooth_serial'?c.serial_ports.map(p=>p.device+' — '+p.description):[];
 const detail=method==='browser'?'Open a receipt and choose a printer in this device’s print dialog. iPad requires AirPrint; Android may need a printer service.'
   :!c.windows?'Windows server-side direct printing is unavailable on Render. Use tablet/device printing above.'
   :method==='windows_raw'&&!c.win32print_installed?'Install pywin32 from requirements-printer-windows.txt and restart the server.'
   :method==='bluetooth_serial'&&!c.pyserial_installed?'Install pyserial from requirements-printer-windows.txt and restart the server.'
   :deviceList.length+' detected device(s). Choose a paired ESC/POS printer.';
 $('#printerStatus').textContent=detail+(c.queue_error?' Printer discovery error: '+c.queue_error:'')+(c.serial_error?' COM discovery error: '+c.serial_error:'');
}
function paintPrinterTargets(){
 if(!printerData)return;
 let method=$('#printerMethod').value;const c=printerData.capabilities,selection=$('#printerTarget').value||printerData.settings.target;
 for(const o of $('#printerMethod').options){if(o.value!=='browser'){o.disabled=!c.windows;o.hidden=!c.windows;}}
 if(!c.windows&&method!=='browser'){method='browser';$('#printerMethod').value='browser';}
 $('#printerTargetRow').hidden=method==='browser';
 $('#printerBaudRow').hidden=method!=='bluetooth_serial';
 const items=method==='windows_raw'?c.queues.map(n=>({id:n,label:n})):method==='bluetooth_serial'?c.serial_ports.map(p=>({id:p.device,label:p.device+' — '+p.description})):[];
 $('#printerTarget').innerHTML='<option value="">Select a detected printer / port</option>'+items.map(x=>'<option value="'+esc(x.id)+'">'+esc(x.label)+'</option>').join('');
 if(items.some(x=>x.id===selection))$('#printerTarget').value=selection;
 $('#printerAutoPOS').disabled=method==='browser';
 if(method==='browser')$('#printerAutoPOS').checked=false;
 $('#printerDirectTest').disabled=method==='browser';
 printerControlStatus(printerData);
}
async function loadPrinterSettings(){
 try{
  const r=await request('/admin/printers');printerData=r;
  $('#printerMethod').value=r.settings.method;$('#printerWidth').value=prefGet('laluna-paper-width')||String(r.settings.paper_width);
  $('#printerBaud').value=String(r.settings.baud_rate);$('#printerAutoPOS').checked=!!r.settings.auto_print_pos;
  paintPrinterTargets();
  $('#savePrinter').disabled=user?.role==='cashier';
 }catch(e){$('#printerStatus').textContent='Could not load printer list: '+e.message;}
}
$('#printerWidth').onchange=()=>{prefSet('laluna-paper-width',$('#printerWidth').value);notify('Receipt paper width saved for this device.');};
$('#printerMethod').onchange=paintPrinterTargets;
$('#refreshPrinters').onclick=()=>loadPrinterSettings().catch(e=>notify(e.message,true));
$('#printerBrowserTest').onclick=()=>openReceipt(null);
$('#printerDirectTest').onclick=()=>directPrintOrder(null).catch(e=>notify(e.message,true));
$('#savePrinter').onclick=async()=>{
 const settings={method:$('#printerMethod').value,paper_width:Number($('#printerWidth').value),
  target:$('#printerTarget').value,baud_rate:Number($('#printerBaud').value),auto_print_pos:$('#printerAutoPOS').checked};
 try{
  const r=await request('/admin/printers',{method:'PUT',body:JSON.stringify(settings)});
  if(printerData)printerData.settings=r.settings;
  notify('Receipt printer settings saved. Print a test receipt.');
  paintPrinterTargets();
 }catch(e){notify(e.message,true);}
};
$('#addPayMethod').onclick=async()=>{const name=$('#payNewName').value,instructions=$('#payNewNote').value;try{await request('/admin/payments',{method:'POST',body:JSON.stringify({name,instructions})});$('#payNewName').value='';$('#payNewNote').value='';loadSettings();notify('Payment method added');}catch(e){notify(e.message,true);}};
function paintStaff(staff){
 $('#staffPanel').hidden=false;
 $('#staffList').innerHTML=staff.map(s=>'<div class="list-line"><div><b>'+esc(s.name)+'</b> <small class="hint">('+esc(s.role)+')</small><br><span class="hint">'+esc(s.email)+'</span></div><div>'+esc(s.active?'Active':'Disabled')+' '+(s.role==='owner'?'':'<button class="outline" data-edit-staff="'+s.id+'">Edit account</button>')+'</div></div>').join('');
 $$('[data-edit-staff]').forEach(b=>b.onclick=()=>{
  const item=staff.find(x=>x.id===Number(b.dataset.editStaff));if(!item)return;
  openDialog('Edit staff login — '+item.name,'<form id="editStaffForm"><label>Staff name<input class="field" name="name" required value="'+esc(item.name)+'"></label><label>Email<input type="email" class="field" name="email" required value="'+esc(item.email)+'"></label><label>Login role<select class="field" name="role"><option value="cashier"'+(item.role==='cashier'?' selected':'')+'>Cashier</option><option value="manager"'+(item.role==='manager'?' selected':'')+'>Manager</option></select></label><label>New login password (leave blank to keep)<input type="password" name="password" class="field" minlength="12" autocomplete="new-password"></label><label><input name="active" type="checkbox"'+(item.active?' checked':'')+'> Login enabled</label></form>',async()=>{
   const f=$('#editStaffForm'),d=Object.fromEntries(new FormData(f));d.active=f.elements.active.checked;if(!d.password)delete d.password;
   await request('/admin/staff/'+item.id,{method:'PATCH',body:JSON.stringify(d)});await loadSettings();notify('Staff login updated');
  });
 });
}
$('#addStaff').onclick=async()=>{const d={name:$('#newStaffName').value,email:$('#newStaffEmail').value,password:$('#newStaffPassword').value,role:$('#newStaffRole').value};try{await request('/admin/staff',{method:'POST',body:JSON.stringify(d)});$('#newStaffName').value='';$('#newStaffEmail').value='';$('#newStaffPassword').value='';notify('Staff account created');loadSettings();}catch(e){notify(e.message,true);}};

// Seating management, staff directory, cash counting and dashboard views.
async function loadTables(){
 const data=await request('/admin/tables');
 if(page!=='tables')return;
 managedTables=data.tables;
 $('#newTable').hidden=!['owner','manager'].includes(user.role);
 const rows=data.tables.map(t=>{
  const status=t.active?'Active':'Inactive';
  const time=t.last_order_at?'Last order: '+dt(t.last_order_at):'No orders recorded';
  return '<article class="table-tile'+(t.active?'':' inactive')+'" data-table-card="'+t.id+'">'+
   '<div class="table-header"><h3>Table '+esc(t.number)+'</h3><span class="status '+(t.active?'accepted':'cancelled')+'">'+status+'</span></div>'+
   '<div class="table-cap"><strong>Location:</strong> '+esc(t.location)+'</div>'+
   '<div class="table-cap"><strong>Capacity:</strong> '+t.seats+' customer(s)</div>'+
   '<div class="table-metrics"><span><strong>'+t.open_orders+'</strong> open ticket(s)</span><span><strong>'+t.paid_orders+'</strong> paid order(s)</span></div>'+
   '<div class="table-cap"><strong>Net sales:</strong> '+peso(t.paid_revenue)+' · '+t.total_orders+' total order(s)</div>'+
   '<div class="table-cap hint">'+esc(time)+'</div>'+
   '<div class="actions">'+(['owner','manager'].includes(user.role)?'<button type="button" data-edit-table="'+t.id+'">Edit Table</button>':'')+
   '<button type="button" data-use-table="'+t.id+'"'+(t.active?'':' disabled')+'>POS Order</button>'+
   '<button type="button" data-view-table="'+t.id+'">View Orders</button>'+
   ((!t.active&&['owner','manager'].includes(user.role))?'<button type="button" class="danger-table" data-delete-table="'+t.id+'">Delete Inactive Table</button>':'')+
   '<button type="button" data-table-qr="'+t.id+'"'+(t.active?'':' disabled')+'>Generate QR</button></div></article>';
 }).join('');
 $('#tableCards').innerHTML=rows||'<p>No tables configured. Add your restaurant tables with their actual capacities and locations.</p>';
 const updated=$('#tableUpdatedAt');if(updated)updated.textContent='Synced '+new Date().toLocaleTimeString('en-PH');
 $$('[data-edit-table]').forEach(b=>b.onclick=()=>{const t=managedTables.find(t=>t.id===Number(b.dataset.editTable));if(t)editTable(t);});
 $$('[data-delete-table]').forEach(b=>b.onclick=async()=>{
  const t=managedTables.find(x=>x.id===Number(b.dataset.deleteTable));if(!t||t.active)return;
  if(!confirm('Remove inactive Table '+t.number+' from the restaurant? Historic orders and sales are preserved, but its printed QR will stop working.'))return;
  try{await request('/admin/tables/'+t.id,{method:'DELETE'});
    notify('Inactive Table '+t.number+' removed; sales history retained.');await loadTables();}
  catch(e){notify(e.message,true);}
 });
 $$('[data-use-table]').forEach(b=>b.onclick=async()=>{
  const t=managedTables.find(x=>x.id===Number(b.dataset.useTable));
  if(!t?.active)return notify('Enable this table before using it',true);
  pendingTableSelection=t.number;
  $('#posType').value='Dine-In';showPosTable();
  try{await navigate('pos');if($('#posTable').value!==t.number)throw Error('Could not select table '+t.number);notify('POS is ready for Table '+t.number+' · '+t.seats+' seats.');}
  catch(e){notify('Unable to open table POS: '+e.message,true);}
 });
 $$('[data-view-table]').forEach(b=>b.onclick=()=>{
  const t=managedTables.find(x=>x.id===Number(b.dataset.viewTable));if(!t)return;
  orderTableFilter=t.id;$('#orderSearch').value='';$('#orderFilter').value='all';
  navigate('orders').catch(()=>{});
 });
 $$('[data-table-qr]').forEach(b=>b.onclick=async()=>{
  const t=managedTables.find(x=>x.id===Number(b.dataset.tableQr));if(!t?.active)return notify('Enable this table before generating its QR',true);
  try{
   await navigate('settings');$('#qrTable').value=t.number;
   await generateQR(t.number);
  }catch(e){notify('Could not generate QR for '+t.number+': '+e.message,true);}
 });
}
function editTable(t){
 if(!['owner','manager'].includes(user.role))return notify('Owner/manager access required',true);
 const body='<form id="tableForm"><label>Table number<input class="field" name="number" required maxlength="12" pattern="[A-Za-z0-9_-]{1,12}" value="'+esc(t?.number||'')+'"></label>'+ 
 '<label>Customers / seating capacity<input class="field" name="seats" type="number" required min="1" max="30" value="'+(t?.seats||4)+'"></label>'+
 '<label>Table location<input class="field" name="location" required maxlength="80" placeholder="Main Dining Area, Patio, Upstairs" value="'+esc(t?.location||'')+'"></label>'+
 (t?'<label><input type="checkbox" name="active"'+(t.active?' checked':'')+'> Active table</label>':'')+
 (t?'<p class="hint">Renaming or disabling a table requires closing its open orders. Previously placed orders remain linked to this table; the permanent QR stays valid after renaming.</p>':'')+'</form>';
 openDialog(t?'Edit Table '+t.number:'Add Restaurant Table',body,async()=>{
  const f=$('#tableForm');if(!f.reportValidity())throw Error('Complete each required table field.');
  const d=Object.fromEntries(new FormData(f));d.number=d.number.trim().toUpperCase();d.location=d.location.trim();d.seats=Number(d.seats);
  if(t)d.active=f.elements.active.checked;
  await request(t?'/admin/tables/'+t.id:'/admin/tables',{method:t?'PATCH':'POST',body:JSON.stringify(d)});
  await loadTables();
  showTableAction('Table '+d.number+' saved · '+d.seats+' seat(s) · '+d.location+'. '+(t&&t.number!==d.number?'Its permanent QR remains valid; older receipts retain the original table number.':''));
  notify('Table '+d.number+' saved');
 });
 $('#tableForm').onsubmit=e=>{e.preventDefault();$('#saveDialog').click();};
}
$('#newTable').onclick=()=>editTable(null);
$('#refreshTables').onclick=()=>loadTables().catch(e=>notify(e.message,true));
async function loadPosTableOptions(){
 const d=await request('/admin/tables');managedTables=d.tables;
 const select=$('#posTable'),old=select.value;
 select.innerHTML='<option value="">Select table</option>'+d.tables.filter(t=>t.active).map(t=>'<option value="'+esc(t.number)+'">'+esc(t.number+' · '+t.location+' · '+t.seats+' seats')+'</option>').join('');
 if(d.tables.some(t=>t.active&&t.number===old))select.value=old;
 if(pendingTableSelection){select.value=pendingTableSelection;pendingTableSelection=null;}
 showPosTable();
}
function showPosTable(){const dine=$('#posType').value==='Dine-In',delivery=$('#posType').value==='Delivery';$('#posDineTableRow').hidden=!dine;$('#posTableHint').hidden=!dine;$('#posExistingTicketRow').hidden=!dine;$('#posDeliveryFields').hidden=!delivery;$('#sendTableOrder').hidden=!dine;if(!dine&&posExistingTicket){posExistingTicket=null;posOriginalCart=[];posCart=[];$('#sendTableOrder').textContent='Send Order to Table · Unpaid';$('#completeSale').textContent='Send to Orders for Payment';}paintPOSCart();}
$('#posType').onchange=()=>{showPosTable();loadTableOpenTickets().catch(e=>notify(e.message,true));};$('#posDeliveryFee').oninput=paintPOSCart;
async function loadEmployees(){
 const data=await request('/admin/employees');employees=data.employees;
 $('#newEmployee').hidden=!['owner','manager'].includes(user.role);
 $('#employeeList').innerHTML=table(['Employee','Role / title','PIN configured','Status','Action'],employees.map(e=>'<tr><td><b>'+esc(e.name)+'</b></td><td>'+esc(e.title)+'</td><td>'+esc(e.pin_set?'Yes':'Set PIN first')+'</td><td>'+esc(e.active?'Active':'Disabled')+'</td><td>'+(['owner','manager'].includes(user.role)?'<button data-employee="'+e.id+'">Edit</button>':'—')+'</td></tr>'));
 $$('[data-employee]').forEach(b=>b.onclick=()=>editEmployee(employees.find(x=>x.id===Number(b.dataset.employee))));
}
function editEmployee(e){
 if(!['owner','manager'].includes(user.role))return;
 openDialog(e?'Edit '+e.name:'Add employee','<form id="employeeForm"><label>Employee name<input class="field" name="name" maxlength="80" required value="'+esc(e?.name||'')+'"></label><label>Role / title<input class="field" name="title" maxlength="80" required value="'+esc(e?.title||'Service Staff')+'"></label><label>Register PIN <small class="hint">'+(e?'Leave blank to keep existing PIN':'6–8 digits; required to unlock register')+'</small><input class="field" name="pin" type="password" inputmode="numeric" minlength="6" maxlength="8" pattern="[0-9]{6,8}" autocomplete="new-password" placeholder="••••••"></label>'+(e?'<label><input name="active" type="checkbox"'+(e.active?' checked':'')+'> Active employee</label>':'')+'</form>',async()=>{
 const f=$('#employeeForm'),d=Object.fromEntries(new FormData(f));if(!d.pin)delete d.pin;if(e)d.active=f.elements.active.checked;
 await request(e?'/admin/employees/'+e.id:'/admin/employees',{method:e?'PATCH':'POST',body:JSON.stringify(d)});await loadEmployees();notify('Employee saved. Register PIN is not shown.');
 });
}
$('#newEmployee').onclick=()=>editEmployee(null);
const cashDenominations=[['coin_001','Coin ₱0.01'],['coin_005','Coin ₱0.05'],['coin_010','Coin ₱0.10'],['coin_025','Coin ₱0.25'],['coin_1','Coin ₱1'],['coin_5','Coin ₱5'],['coin_10','Coin ₱10'],['coin_20','Coin ₱20'],['bill_20','Bill ₱20'],['bill_50','Bill ₱50'],['bill_100','Bill ₱100'],['bill_200','Bill ₱200'],['bill_500','Bill ₱500'],['bill_1000','Bill ₱1,000']];
$('#denomGrid').innerHTML=cashDenominations.map(([key,label])=>'<label class="denom-item">'+esc(label)+'<input class="field" type="number" min="0" max="10000" step="1" value="0" data-denom="'+key+'"></label>').join('');
function readCashCounts(){const values={};$$('[data-denom]').forEach(i=>{if(!/^(0|[1-9][0-9]*)$/.test(i.value)||Number(i.value)>10000)throw Error('Enter whole non-negative counts (maximum 10,000 each)');values[i.dataset.denom]=Number(i.value);});return values;}
function showCashTotal(){try{const v=readCashCounts();const rates=registerState?.denominations||{};$('#registerCountTotal').textContent=peso(Object.entries(v).reduce((n,[k,qty])=>n+qty*(rates[k]||0)/100,0));}catch{$('#registerCountTotal').textContent='Invalid count';}}
$$('[data-denom]').forEach(i=>i.oninput=showCashTotal);
async function loadRegister(){
 const [r,e]=await Promise.all([request('/admin/register'),request('/admin/employees')]);registerState=r;employees=e.employees;
 const active=r.active;
 const chosen=$('#registerEmployee').value;
 // Show every active employee, including staff whose PIN still needs setup.
 const eligible=employees.filter(e=>e.active);
 $('#registerEmployee').innerHTML='<option value="">Select an employee</option>'+eligible.map(e=>'<option value="'+e.id+'">'+esc(e.name+' · '+e.title+(e.pin_set?'':' (PIN setup required)'))+'</option>').join('');
 if(eligible.some(e=>String(e.id)===chosen))$('#registerEmployee').value=chosen;
 updateRegisterEmployeeHint();
 $('#registerSummary').innerHTML=active?'<h3 class="register-open-label">OPEN · '+esc(active.employee_name)+'</h3><div class="kpi-grid">'+kpi('Opening float',peso(active.opening_total))+kpi('Recorded cash collections',peso(active.cash_sales),active.cash_orders+' sale(s)')+kpi('Expected drawer cash',peso(active.expected_cash))+kpi('Opened at',dt(active.opened_at))+'</div>'+(active.opening_note?'<p><b>Opening notes:</b> '+esc(active.opening_note)+'</p>':''):'<h3>Cash register is currently closed</h3><p>Choose an employee with a configured PIN to open it.</p>';
 $('#registerActionTitle').textContent=active?'Close Cash Register — Final Count':'Open Cash Register — Opening Count';$('#registerAction').textContent=active?'Close & Reconcile':'Open Cash Register';
 $('#registerCountStep').textContent=active?'2. Count closing coins and paper bills':'2. Count opening coins and paper bills';
 $('#registerHistory').innerHTML=table(['#','Employee','Status','Opened','Variance','Register notes'],r.history.map(x=>'<tr><td>'+x.id+'</td><td>'+esc(x.employee_name)+'</td><td>'+esc(x.status)+'</td><td>'+dt(x.opened_at)+'</td><td>'+esc(x.variance_cents===null?'—':peso(x.variance_cents/100))+'</td><td class="ll-register-notes">'+esc([x.opening_note?'Open: '+x.opening_note:'',x.closing_note?'Close: '+x.closing_note:''].filter(Boolean).join(' · ')||'—')+'</td></tr>'));
 $$('[data-denom]').forEach(x=>x.value=0);$('#registerPin').value='';showCashTotal();$('#registerNote').value='';
}
function updateRegisterEmployeeHint(){
 const current=employees.find(e=>String(e.id)===$('#registerEmployee').value);
 const note=$('#registerEmployeeHint');
 if(!current){note.textContent='Select the employee responsible for the cash drawer.';return;}
 note.textContent=current.pin_set
  ? current.name+' is selected. Count the cash and enter your individual PIN.'
  : current.name+' has no register PIN yet. An owner or manager must configure it under Employees before opening the register.';
}
$('#registerEmployee').onchange=()=>{updateRegisterEmployeeHint();$('#registerPin').value='';};
$('#refreshRegister').onclick=()=>loadRegister().catch(e=>notify(e.message,true));
$('#registerAction').onclick=async()=>{
 const active=registerState?.active,employee_id=Number($('#registerEmployee').value),pin=$('#registerPin').value;
 const selected=employees.find(e=>e.id===employee_id);
 if(!selected)return notify('Select your name from the employee list',true);
 if(!selected.pin_set)return notify('Employee PIN setup required. Ask an owner or manager to configure a PIN under Employees.',true);
 if(!/^[0-9]{6,8}$/.test(pin))return notify('Enter your personal 6–8 digit register PIN',true);
 let counts;try{counts=readCashCounts();}catch(e){return notify(e.message,true);}
 if(!confirm((active?'Close cash register and record variance?':'Open cash register with the counted starting float?')+'\nCounted: '+$('#registerCountTotal').textContent))return;
 const b=$('#registerAction');b.disabled=true;
 try{const result=await request(active?'/admin/register/close':'/admin/register/open',{method:'POST',body:JSON.stringify({employee_id,pin,counts,note:$('#registerNote').value})});notify(active?'Register closed. Cash variance: '+peso(result.variance):'Cash register opened for '+result.employee_name);await loadRegister();}
 catch(e){notify(e.message,true);}finally{b.disabled=false;$('#registerPin').value='';}
};
function plotBars(entries,labelKey){const max=Math.max(1,...entries.map(e=>Number(e.revenue)));return '<div class="mini-bars">'+entries.map(e=>'<div title="'+esc(e[labelKey])+': '+peso(e.revenue)+'"><i style="height:'+Math.max(2,Math.round(e.revenue/max*135))+'px"></i><small>'+esc(labelKey==='hour'?e.hour.slice(0,2):e.day.slice(0,3))+'</small></div>').join('')+'</div>';}
function analyticsTable(heads,rows){return table(heads,rows);}
async function loadRestaurantAnalytics(){
 const d=await request('/admin/restaurant-dashboard');
 const restaurant=analyticsView==='restaurant';
 $('#restaurantAnalyticsBtn').classList.toggle('primary',restaurant);$('#posAnalyticsBtn').classList.toggle('primary',!restaurant);
 const metrics=restaurant?[
 ['Net sales',peso(d.revenue)],['Paid orders',d.orders],['Avg paid order',peso(d.average_order)],['Dine-in guests',d.guests],['Dine-in revenue / guest',peso(d.revenue_per_guest)],['Discounts','Not tracked']]:
 [['POS revenue',peso(d.pos_revenue)],['POS orders',d.pos_orders],['Avg POS order',peso(d.pos_orders?d.pos_revenue/d.pos_orders:0)],['Cash drawer',d.open_register?'Open':'Closed']];
 const cards='<div class="kpi-grid">'+metrics.map(m=>kpi(m[0],m[1])).join('')+'</div>';
 if(!restaurant){
   const monthly=table(['Month','POS orders'],d.pos_monthly.map(m=>'<tr><td>'+esc(m.month)+'</td><td>'+m.orders+'</td></tr>'));
   const sessions=table(['Session','Orders','Revenue','Status'],d.register_sessions.map(m=>'<tr><td>#'+m.session+'</td><td>'+m.orders+'</td><td>'+peso(m.revenue)+'</td><td>'+esc(m.status)+'</td></tr>'));
   $('#restaurantAnalytics').innerHTML=cards+'<div class="analytics-charts"><div class="analytics-box"><h4>Orders by Month</h4>'+monthly+'</div><div class="analytics-box"><h4>Register Sessions</h4>'+sessions+'</div></div>';
   return;
 }

 const detail=(heads,entries,fields)=>analyticsTable(heads,entries.map(e=>'<tr>'+fields.map(([key,currency])=>'<td>'+esc(currency?peso(e[key]):e[key])+'</td>').join('')+'</tr>'));
 $('#restaurantAnalytics').innerHTML=cards+'<div class="analytics-charts"><div class="analytics-box"><h4>Hourly net payments/refunds · PH time</h4>'+plotBars(d.hourly,'hour')+'</div><div class="analytics-box"><h4>Net payments/refunds by weekday · PH time</h4>'+plotBars(d.weekly,'day')+'</div><div class="analytics-box"><h4>Top Products · estimated margin</h4>'+detail(['Product','Units','Revenue','Est. margin'],d.products,[['name'],['units'],['revenue',true],['estimated_margin',true]])+'<p class="hint">Indicative gross margin only; new orders snapshot product cost. Historical items may use current product costs. Excludes overhead and fees.</p></div><div class="analytics-box"><h4>Top Categories</h4>'+detail(['Category','Units','Revenue'],d.categories,[['name'],['units'],['revenue',true]])+'</div><div class="analytics-box"><h4>Top Tables per Revenue</h4>'+detail(['Table','Revenue'],d.tables,[['table'],['revenue',true]])+'</div><div class="analytics-box"><h4>Employees / staff recording paid orders</h4>'+detail(['Employee / staff','Revenue'],d.responsibles,[['name'],['revenue',true]])+'</div></div>';
}
$('#restaurantAnalyticsBtn').onclick=()=>{analyticsView='restaurant';loadRestaurantAnalytics().catch(e=>notify(e.message,true));};
$('#posAnalyticsBtn').onclick=()=>{analyticsView='pos';loadRestaurantAnalytics().catch(e=>notify(e.message,true));};
async function loadQRNetwork(){
 try{
  qrNetwork=await request('/admin/qr-network');
  const entries=qrNetwork.wifi_addresses||[];
  const urls=qrNetwork.wifi_urls||[];
  $('#qrUrlHints').innerHTML=urls.map(url=>'<option value="'+esc(url)+'"></option>').join('');
  const select=$('#qrNetworkChoice');
  select.innerHTML='<option value="">Choose the actual Wi-Fi adapter...</option>'+entries.map(entry=>
   '<option value="'+esc(entry.url)+'">'+esc(entry.adapter)+' — '+esc(entry.url)+'</option>').join('');
  select.onchange=()=>{$('#qrBaseUrl').value=select.value;$('#qrDisplay').textContent='';};
  if(qrNetwork.public_mode&&qrNetwork.public_url){
   // Render/custom-domain deployments should never keep an old Wi-Fi or
   // localhost value from a previous visit when generating a table QR.
   $('#qrBaseUrl').value=qrNetwork.public_url;
  }
  else if(!$('#qrBaseUrl').value){
   const publicUrl=qrNetwork.public_url||(location.protocol==='https:'?location.origin:'');
   if(publicUrl){$('#qrBaseUrl').value=publicUrl;}
   // When multiple adapters are present, don't silently choose a VPN or
   // stale address. Staff must select their connected Wi-Fi adapter.
   else if(qrNetwork.wifi_mode&&entries.length===1){
    $('#qrBaseUrl').value=entries[0].url;select.value=entries[0].url;
   }
  }
  $('#genQR').disabled=!qrNetwork.permanent_table_qr_ready;
  const note=qrNetwork.public_mode
   ? 'PUBLIC HTTPS ACTIVE. Your table codes are permanent, repeat-use links tied to the table record and work on mobile data or ANY Wi-Fi. Do not change the public domain after printing. Test on mobile data before distributing QRs. Hosted back office: '+qrNetwork.remote_backoffice_url
   : qrNetwork.wifi_mode
   ? 'LOCAL Wi-Fi mode: permanent table QRs are DISABLED here because customers on mobile data cannot reach this computer. Open your public HTTPS /backoffice on Render to print reusable table codes. Local phone tests can still use the Wi-Fi address above.'
   : 'LOCAL ONLY: permanent table QR creation is disabled. Open the live HTTPS Render /backoffice to generate codes for any Wi-Fi or mobile data.';
  $('#qrBaseUrl').readOnly=!!qrNetwork.public_mode;
  if(qrNetwork.public_mode){select.disabled=true;select.innerHTML='<option value="">Public HTTPS mode (Wi-Fi adapter not needed)</option>';}
  else {select.disabled=false;}
  $('#qrLanHelp').textContent=note;
 }catch(e){$('#qrLanHelp').textContent='Unable to detect local access: '+e.message;}
}
function selectedQRBase(){
 const raw=$('#qrBaseUrl').value.trim().replace(/\/+$/,'');
 let u;try{u=new URL(raw);}catch{throw Error('Choose your Wi-Fi adapter or enter a full HTTPS URL.');}
 if(!['http:','https:'].includes(u.protocol)||!u.hostname||u.username||u.password||u.pathname!=='/'||u.search||u.hash)throw Error('Enter only the root URL, without a page path or query.');
 if(['127.0.0.1','localhost','0.0.0.0','[::1]'].includes(u.hostname.toLowerCase()))throw Error('127.0.0.1 / localhost is unreachable from customer phones. Choose your PC Wi-Fi IP or public HTTPS domain.');
 return raw;
}
async function generateQR(table){
 const isTest=table==='__phone_test__';
 if(!isTest&&table!==null&&!/^[a-z0-9_-]{1,12}$/i.test(table)){notify('Use 1–12 letters, numbers, dashes or underscores for the table number.',true);return;}
 const btn=isTest?$('#genTestQR'):table===null?$('#genGeneralQR'):$('#genQR');
 btn.disabled=true;
 try{
  if(table!==null&&!isTest&&(!qrNetwork||!qrNetwork.permanent_table_qr_ready))
   throw Error('Permanent QRs need a live Render HTTPS site. Open your LIVE /backoffice, not a localhost copy.');
  const base=selectedQRBase();
  // Generate permanent table codes ONLY from the running public backend, not
  // arbitrary local IPs or a handwritten/temporary URL.
  const route=isTest?'/api/admin/qr-test':table===null?'/api/admin/qr-menu':'/api/admin/qr-static/'+encodeURIComponent(table);
  const uri=route+(isTest||table===null?'?base_url='+encodeURIComponent(base):'');
  const r=await fetch(uri,{credentials:'same-origin',cache:'no-store'});
  if(!r.ok){let msg='QR generation failed ('+r.status+')';try{const data=await r.json();msg=data.detail||msg;}catch{}throw Error(msg);}
  if(!(r.headers.get('content-type')||'').includes('image/png'))throw Error('Server did not return a QR PNG image.');
  const blob=await r.blob();
  if(qrBlobUrl)URL.revokeObjectURL(qrBlobUrl);
  qrBlobUrl=URL.createObjectURL(blob);
  const target=r.headers.get('X-QR-Target');
  if(!target)throw Error('Server returned no QR destination. Update the complete La Luna application.');
  const pane=$('#qrDisplay');pane.innerHTML='';
  const image=document.createElement('img');image.src=qrBlobUrl;image.alt=isTest?'La Luna phone connection test QR':table===null?'La Luna general ordering QR':'La Luna table '+table+' QR';pane.appendChild(image);
  const targetLink=document.createElement('a');targetLink.href=target;targetLink.target='_blank';targetLink.rel='noopener noreferrer';targetLink.textContent=target;pane.appendChild(targetLink);
  const hint=document.createElement('p');hint.className='hint';hint.textContent=isTest?'SCAN THIS ON A PHONE USING MOBILE DATA. Confirm CONNECTED before creating table QRs.':table===null?'General ordering QR. Test over mobile data before printing.':'PERMANENT REUSABLE TABLE QR: Reuse the same printed code. Dine-In follows this table even if it is renamed; Take-Out uses counter collection. Keep this public HTTPS domain active.';pane.appendChild(hint);
  const save=document.createElement('a');save.href=qrBlobUrl;save.download=isTest?'laluna-phone-connection-test.png':'laluna-'+(table===null?'general-ordering':'table-'+table)+'-qr.png';save.textContent='Download QR PNG';pane.appendChild(save);
  notify(isTest?'Scan test QR on mobile data and confirm CONNECTED.':table===null?'General ordering QR created.':'Permanent reusable table QR created. You can print this once and scan it repeatedly.');
 }catch(e){notify(e.message,true);$('#qrDisplay').textContent='Could not generate QR: '+e.message;}
 finally{btn.disabled=table!==null&&!isTest?!qrNetwork?.permanent_table_qr_ready:false;}
}
$('#genGeneralQR').onclick=()=>generateQR(null);
$('#genTestQR').onclick=()=>generateQR('__phone_test__');
$('#genQR').onclick=()=>generateQR($('#qrTable').value.trim());
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('#dialog').hidden)closeDialog();});
setInterval(refreshOrdersQuietly,7000);
setInterval(()=>{if(page==='tables'&&user&&!document.hidden&&$('#dialog').hidden)loadTables().catch(e=>notify('Table sync unavailable: '+e.message,true));},10000);
setInterval(()=>{if(page==='overview'&&user&&!document.hidden)loadOverview().catch(e=>notify(e.message,true));},15000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshOrdersQuietly();});
autoLogin();
})();
