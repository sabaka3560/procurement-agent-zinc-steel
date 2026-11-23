"use strict";
const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const vm=require("node:vm");

const source=fs.readFileSync(path.join(__dirname,"..","decision-desk.js"),"utf8");
const start=source.indexOf("function selectSuppliers(index) {");
const end=source.indexOf("\nfunction render(d)",start);
assert.ok(start>=0 && end>start,"Supplier renderer is available");
const supplier={rank:1,name:"Test supplier",evidence:"Product only",url:"https://example.com/product",reviewed_at:"2026-09-08",discount_tiers_t:[100,150]};
const country={name:"Spain",lead_days:35,suppliers:[supplier],supplier_gap:2};
const content={innerHTML:""};
const context={
  URL,
  result:{countries:[country]},
  $:id=>id==="supplier-content"?content:{querySelectorAll:()=>[]},
  esc:value=>String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])),
  num:value=>String(value),
};
vm.createContext(context);
vm.runInContext(source.slice(start,end),context);
context.selectSuppliers(0);
assert.match(content.innerHTML,/Quote required/);
assert.match(content.innerHTML,/Product information \(not a quote\)/);
assert.match(content.innerHTML,/Two comparable dated supplier quotes/);
assert.match(content.innerHTML,/35 days is a buyer route assumption/);
assert.match(content.innerHTML,/2 of 3 supplier slots still need research/);

supplier.commercial_evidence={
  price:{status:"historical",note:"Historical price, not a current offer",url:"https://example.com/archive",observed_at:"2025-06-09"},
  delivery:{status:"unknown",note:'<img src=x onerror="alert(1)">',url:"javascript:alert(1)",observed_at:'<script>alert(1)</script>'},
  change:{note:"No comparable observations",url:"http://example.com/insecure"},
};
supplier.automatic_check={status:"failed",checked_at:"2026-09-08",last_success_at:"2026-09-06",stale:true,last_change_at:"2026-09-06",note:"Prior publication retained"};
supplier.contact_url="https://example.com/sales";
supplier.contact_evidence="Public RFQ form; no order confirmed";
context.selectSuppliers(0);
assert.match(content.innerHTML,/Open supplier sales \/ RFQ channel/);
assert.match(content.innerHTML,/acceptance of this order is NOT confirmed/);
assert.match(content.innerHTML,/Automatic check: failed/);
assert.match(content.innerHTML,/review required/);
assert.match(content.innerHTML,/Retrieval date is not the publication date/);
assert.match(content.innerHTML,/href="https:\/\/example.com\/archive"/);
assert.match(content.innerHTML,/Publication date \/ period: 2025-06-09/);
assert.match(content.innerHTML,/Historical price, not a current offer/);
assert.match(content.innerHTML,/Quote required/);
assert.match(content.innerHTML,/&lt;img/);
assert.doesNotMatch(content.innerHTML,/<img|<script|href="javascript:|href="http:/);
supplier.commercial_evidence.price.url="https://user:password@example.com/private";
supplier.url="javascript:alert(1)";
supplier.contact_url="javascript:alert(1)";
context.selectSuppliers(0);
assert.doesNotMatch(content.innerHTML,/href="https:\/\/user:|href="javascript:/);
country.suppliers=[];
country.supplier_gap=3;
context.selectSuppliers(0);
assert.match(content.innerHTML,/0 evidenced candidates/);
assert.match(content.innerHTML,/3 of 3 supplier slots still need research/);
console.log("Supplier evidence rendering: all assertions passed");
