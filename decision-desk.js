"use strict";
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = (value, digits=0) => value !== null && value !== undefined && Number.isFinite(Number(value)) ? Number(value).toLocaleString("en-IN", {maximumFractionDigits:digits}) : "Unavailable";
const money = (value, currency) => value === null || value === undefined ? "Quote required" : `${currency === "INR" ? "₹" : "$"}${num(value)}`;
const date = value => {const d=new Date(value);return Number.isNaN(d.valueOf())?"Date unknown":d.toLocaleDateString("en-GB",{day:"numeric",month:"short",year:"numeric"});};
const pill = (text, kind="") => `<span class="pill ${kind}">${esc(text)}</span>`;
let quotes={}, busy=false, result=null, autoTimer=null, lastAutoKey=null;
const form=$("procurement-form");
const numeric = new Set(["monthly_demand_t","on_hand_t","safety_stock_t","safe_value_pct","domestic_lead_days","import_lead_days","planning_months"]);
function inputs() {return Object.fromEntries([...new FormData(form)].map(([k,v]) => [k,numeric.has(k)?Number(v):v]));}
function triggerState() {
  const safety=Number($("safety").value), percentage=Number($("safe-percent").value), stock=Number($("stock").value);
  const threshold=safety*(1-percentage/100), crossed=stock<=threshold;
  $("threshold-copy").textContent=`Review at ${num(threshold,2)} t or less — ${num(percentage,1)}% below ${num(safety,2)} t safety stock.`;
  $("trigger-status").textContent=$("auto-review").checked ? `Auto-review armed · entered stock ${crossed?"is at/below":"is above"} the trigger` : "Manual review ready · sample inventory";
  return {crossed,threshold};
}
function renderFeed() {
  const steel=$("material").value==="STEEL_HRC";
  const fields=steel ? [["STEEL:HRC:INDIA","India HRC reference"],["CME:HRC","US HRC futures proxy"],["FX:USDINR:FBIL","USD / INR reference"]] : [["LME:ZINC:CASH","LME zinc cash"],["MCX:ZINC","India zinc futures"],["FX:USDINR:FBIL","USD / INR reference"]];
  $("feed").innerHTML=fields.map(([id,name])=>{
    const q=quotes[id];
    if(!q || q.quality==="SIMULATED") return `<div class="quote"><span class="quote-name">${name}</span><strong class="quote-value">Unavailable</strong><span class="quote-meta stale">No verified observation received. Retry after the public-source refresh.</span></div>`;
    const stale=q.is_stale || q.quality==="UNKNOWN_AGE";
    return `<div class="quote"><span class="quote-name">${name}</span><strong class="quote-value">${num(q.price,2)} <small>${esc(q.unit)}</small></strong><span class="quote-meta ${stale?"stale":""}">${stale?"Stale / age uncertain":"Latest available"} · ${esc(date(q.observed_at))}</span><details><summary>Source & freshness</summary><p>${esc(q.source)}<br>Observed: ${esc(q.observed_at)}<br>Retrieved: ${esc(q.retrieved_at)}<br>${esc(q.note)}<br>Public reference or delayed feed; not an executable supplier quote.</p></details></div>`;
  }).join("");
}
async function marketSnapshot() {
  try {const response=await fetch("/api/procurement/market");if(!response.ok)throw new Error();quotes=(await response.json()).quotes || {};renderFeed();}
  catch {$("feed").innerHTML='<p class="muted" style="padding:16px">Market connection unavailable. Check the server and retry; no prices have been invented.</p>';}
}
function renderDecision(d) {
  const r=d.recommendation, inv=d.inventory;
  $("decision").innerHTML=`<p class="eyebrow">YOUR PROCUREMENT RECOMMENDATION <span class="mode-label">${esc(d.mode)}</span></p><h2 id="decision-title">${esc(r.action)}</h2><p>${esc(r.reason)}</p><div class="decision-metrics"><div><small>${r.buy_after_days?"PLANNED QUANTITY AT REVIEW":"REQUEST QUOTES FOR · NOT AN ORDER"}</small><strong>${num(r.buy_after_days?r.quantity_later_t:r.quantity_now_t,1)} tonnes</strong></div><div><small>COUNTRY TO APPROACH FIRST</small><strong>${esc(r.country || "Confirm expedited supply")}</strong></div><div><small>FIRST RFQ CONTACT · NOT A PRICE WINNER</small><strong>${esc(r.supplier || "Supplier qualification needed")}</strong></div></div>${d.errors.length?`<p class="decision-warning">${esc(d.errors.join(" "))}</p>`:""}<p class="decision-warning">${d.mode==="DEMO INVENTORY"?`Demo stock: ${num(inv.entered_stock_t,1)} t → ${num(inv.on_hand_t,1)} t. Real prices are unchanged. `:""}${esc(r.supplier_basis)}</p>`;
  $("inventory-summary").innerHTML=[["Usable company inventory",`${num(inv.on_hand_t,1)} t`],["Safety stock / trigger",`${num(inv.safety_stock_t,1)} / ${num(inv.trigger_t,1)} t`],["Days before using safety stock",inv.safe_days===null?"No consumption":`${num(inv.safe_days,1)} days`],["Days until stockout",inv.stockout_days===null?"No consumption":`${num(inv.stockout_days,1)} days`]].map(([k,v])=>`<div><small>${k}</small><strong>${v}</strong></div>`).join("");
  const m=d.market;
  $("timing-table").innerHTML=`<table><thead><tr><th>Purchase window</th><th>Reference / median per t</th><th>Model 80% range</th><th>Change vs now</th><th>Probability cheaper</th><th>Stock & delivery check</th></tr></thead><tbody><tr><td><strong>Now</strong><small>${m?esc(date(m.observation.observed_at)):"Source unavailable"}</small></td><td><strong>${m?money(m.benchmark,d.currency):"Unavailable"}</strong></td><td>Observed reference</td><td>Baseline</td><td>Not applicable</td><td>${pill(inv.urgent_bridge_t>0?"Expedite required":"Confirm delivery",inv.urgent_bridge_t>0?"warn":"neutral")}<small>${num(inv.lead_days)}-day planning lead time${inv.urgent_bridge_t>0?` · ${num(inv.urgent_bridge_t,1)} t shortage before arrival`:""}</small></td></tr>${d.forecast.map(f=>`<tr><td><strong>In ${f.days/30} month${f.days>30?"s":""}</strong><small>${esc(date(f.date))}</small></td><td><strong>${money(f.p50,d.currency)}</strong></td><td>${money(f.p10,d.currency)} – ${money(f.p90,d.currency)}</td><td>${f.change_pct>0?"+":""}${num(f.change_pct,1)}%</td><td>${num(f.probability_cheaper*100)}%<small>Model estimate, not a guarantee</small></td><td>${pill(f.feasible?"Stock allows waiting":"Bridge supply needed",f.feasible?"":"warn")}<small>${f.feasible?"Wait + delivery preserves reserve":`${num(f.bridge_t,1)} t to preserve reserve through arrival`}</small></td></tr>`).join("")}</tbody></table>`;
  $("forecast-basis").textContent=m ? `${m.basis} ${m.fresh?"Computed from latest available observations.":"STALE / DEGRADED: ranges are illustrative, not a current timing signal."} Source: ${m.observation.source}. History through ${date(m.history_observed_at)}. ${m.fx_observation?`FX: ${num(m.fx,4)} INR/USD, observed ${date(m.fx_observation.observed_at)}.`:""} 80% is the model's nominal range, not proven real-world coverage.` : "No forecast was generated because the market reference or history is unavailable. Inventory advice still applies.";
}
function renderCountries(d) {
  $("country-table").innerHTML=`<table><thead><tr><th>Country / screening order</th><th>Historical cost / t</th><th>Duty & trade measures</th><th>Delivery assumption</th><th>Logistics / evidence</th><th>Future cost scenario</th></tr></thead><tbody>${d.countries.map(c=>`<tr><td><strong>${c.rank?`${c.rank}. `:""}${esc(c.name)}</strong>${c.preferred?'<span class="preferred-mark">★ Your preference</span>':""}${c.domestic?'<small>Domestic sourcing route</small>':""}</td><td><strong>${money(c.screen_cost,d.currency)}</strong><small>${esc(c.price_basis)}</small>${c.freight_usd_per_t!==null?`<small>Freight context: $${num(c.freight_usd_per_t)}/t; already included in CIF.</small>`:""}</td><td>${c.duty_pct===null?"Verification required":`${num(c.duty_pct,2)}%`}<small>${c.domestic?"Domestic taxes still apply":c.duty_verified?"Evidence-backed tariff screen":"Indicative · customs sign-off needed"}</small><details><summary class="evidence-link">Duty detail</summary><small>${esc(c.duty_basis)}<br>${esc(JSON.stringify(c.surcharges))}<br>${esc(c.notes.join(" "))}</small></details></td><td>${num(c.lead_days)} days<small>Buyer assumption · not supplier ETA</small>${pill(c.can_arrive_before_stockout?(c.can_preserve_reserve?"Reserve covered":"Uses safety stock"):"Too late for stockout",c.can_preserve_reserve?"":"warn")}</td><td>${c.lpi===null?"LPI unavailable":`LPI ${num(c.lpi,1)} / 5`}<small>${c.proven_tonnes===null?"No shipment screen":`${num(c.proven_tonnes)} t historical trade`}</small><small>${c.researched_count} researched supplier${c.researched_count===1?"":"s"}</small></td><td>${c.future_screen.length?c.future_screen.slice(0,2).map(f=>`<div>${f.days/30} mo: ${money(f.median,d.currency)}</div>`).join(""):"Not estimable"}<small>Scaled historical screen; not a future quote</small></td></tr>`).join("")}</tbody></table>`;
  $("supplier-tabs").innerHTML=d.countries.map((c,n)=>`<button type="button" class="country-tab" data-country-index="${n}" aria-pressed="${n===0}">${esc(c.name)}${c.preferred?" ★":""} · ${c.suppliers.length}/3</button>`).join("");
  $("supplier-tabs").querySelectorAll("button").forEach(button=>button.addEventListener("click",()=>selectSuppliers(Number(button.dataset.countryIndex))));
  $("supplier-country").innerHTML=(d.supplier_catalogue || d.countries).map(c=>`<option value="${esc(c.country)}">${esc(c.name)} · ${c.suppliers.length} researched candidates</option>`).join("");
  $("supplier-coverage").textContent=d.coverage?`${d.coverage.supplier_count} catalogue candidates for this material across ${d.coverage.countries_with_candidates} of ${d.coverage.selectable_countries} selectable countries; ${d.coverage.public_contact_count} have researched public contact routes. Remaining country/product combinations are gaps, not zero suppliers in the market.`:"Supplier research coverage has not been calculated.";
  selectSuppliers(0);
}
function renderWorldwide(d) {
  const rows=d.worldwide_rates || [];
  $("worldwide-rates").innerHTML=rows.length?`<table><thead><tr><th>Reported country / area</th><th>Source period</th><th>Annual import value / t</th><th>Reported tonnes</th><th>Tariff mapping</th></tr></thead><tbody>${rows.map(r=>`<tr><td><strong>${esc(r.name)}</strong><small>Comtrade partner ${num(r.comtrade_code)}</small></td><td><a href="${esc(r.source_url)}" target="_blank" rel="noopener noreferrer">${esc(r.year)} source ↗</a><small>${r.provisional?"Incomplete-year / provisional":"Historical annual data"}</small></td><td>${r.unit_value_usd_per_t===null?"No comparable weight / rate":money(r.unit_value_usd_per_t,"USD")}<small>Not a supplier quote</small></td><td>${num(r.tonnes,1)}</td><td>${r.tariff_registry_supported?"Country in tariff registry; route evidence still required":"Outside current tariff registry; payable duty unknown"}</td></tr>`).join("")}</tbody></table>`:'<p class="footnote">No worldwide partner rows returned for this analysis. Public-source failure or missing coverage is not evidence of no suppliers.</p>';
}
function sourceLinks(ids, label="Sources & calculation") {
  return `<button type="button" class="text-button evidence-link source-jump" data-source-ids="${esc((ids || []).join(','))}">${esc(label)} ↗</button>`;
}
function renderSources(d) {
  const sources=d.sources || [];
  $("prediction-source-list").innerHTML=sources.map(s=>`<article class="source-record" id="source-${esc(s.id)}"><p class="eyebrow">${esc(s.kind)}</p><h3>${esc(s.title)}</h3><p>${esc(s.description)}</p>${s.url?`<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">Open source publication / data ↗</a>`:'<span class="muted">Internal method or entered assumption — no external publication.</span>'}<p class="source-dates">Source observation / period: ${esc(s.observed_at || "Not established / not applicable")}<br>Retrieved: ${esc(s.retrieved_at || "Not recorded / not applicable")}</p></article>`).join("");
  $("decision").insertAdjacentHTML("beforeend",sourceLinks(d.recommendation.source_ids,"Why this recommendation?"));
  const rows=$("timing-table").querySelectorAll("tbody tr");
  if(rows[0])rows[0].cells[0].insertAdjacentHTML("beforeend",sourceLinks(["benchmark"],"Price source"));
  d.forecast.forEach((f,n)=>{if(rows[n+1])rows[n+1].cells[0].insertAdjacentHTML("beforeend",sourceLinks(f.source_ids,`${f.days/30}-month prediction sources`));});
  $("country-table").querySelectorAll("tbody tr").forEach((row,n)=>{
    const c=d.countries[n];
    row.cells[0].insertAdjacentHTML("beforeend",sourceLinks(c.source_ids,"Country evidence"));
    if(c.future_screen.length)row.cells[row.cells.length-1].insertAdjacentHTML("beforeend",sourceLinks(c.future_screen[0].source_ids,"Scenario sources"));
  });
  $("negotiation").insertAdjacentHTML("beforeend",`<div>${sourceLinks(["negotiation","inventory"],"Basis for negotiation requests")}</div>`);
}
document.addEventListener("click",event=>{
  const browse=event.target.closest("[data-browse-suppliers]");
  if(browse){const group=result?.supplier_catalogue?.find(c=>c.country===browse.dataset.browseSuppliers);if(group)renderSupplierGroup(group);return;}
  const button=event.target.closest(".source-jump");if(!button)return;
  const ids=button.dataset.sourceIds.split(',');
  $("prediction-sources").open=true;
  document.querySelectorAll(".source-record").forEach(el=>el.classList.toggle("source-selected",ids.includes(el.id.replace("source-",""))));
  const target=ids.map(id=>$("source-"+id)).find(Boolean) || $("prediction-sources");
  target.scrollIntoView({behavior:"smooth",block:"center"});
  target.setAttribute("tabindex","-1");target.focus({preventScroll:true});
});
function selectSuppliers(index) {
  if(!result || !result.countries[index]) return;
  renderSupplierGroup(result.countries[index],index);
}
function renderSupplierGroup(c,index=-1) {
  $("supplier-country").value=c.country;
  const safeLink = value => {
    try {
      const url=new URL(value);
      return url.protocol==="https:" && !url.username && !url.password ? url.href : null;
    } catch {return null;}
  };
  const evidence = (supplier, field, fallback) => {
    const record=supplier.commercial_evidence?.[field];
    const url=safeLink(record?.url), productUrl=safeLink(supplier.url);
    return `<small>${esc(record?.note || fallback)}</small>${url?`<a class="evidence-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Commercial evidence / limitations ↗</a>`:productUrl?`<a class="evidence-link" href="${esc(productUrl)}" target="_blank" rel="noopener noreferrer">Product information (not a quote) ↗</a>`:""}${record?.observed_at?`<small>Publication date / period: ${esc(record.observed_at)}</small>`:""}${record?.automatic_check?checkEvidence(record.automatic_check):""}`;
  };
  const checkEvidence = check => !check ? '<small>Automatic publication check: not recorded.</small>' : `<details><summary class="evidence-link">Automatic check: ${esc(check.status)}${check.stale?" · stale / missing retrieval":""}</summary><small>Checked: ${esc(check.checked_at)}<br>Last successful retrieval: ${esc(check.last_success_at || "None")}<br>${esc(check.note)}${check.last_change_at?`<br>Content changed ${esc(check.last_change_at)} — review required.`:""}<br>Retrieval date is not the publication date or quote validity.</small></details>`;
  const supplierRows=c.suppliers.map(s=>{
    const productUrl=safeLink(s.url);
    const contactUrl=safeLink(s.contact_url);
    const contact=contactUrl?`<p><a class="evidence-link" href="${esc(contactUrl)}" target="_blank" rel="noopener noreferrer">Open supplier sales / RFQ channel ↗</a><small>${esc(s.contact_evidence)}<br>Enquiry channel published; acceptance of this order is NOT confirmed.</small></p>`:'<small>Current sales / RFQ channel still needs research.</small>';
    return `<tr><td><strong>${num(s.rank)}. ${esc(s.name)}</strong><small>${esc(s.supplier_type || "Supplier role unconfirmed")} · ${esc(s.product_match || "Product fit unconfirmed")}</small><small>${esc(s.evidence)}</small>${productUrl?`<a class="evidence-link" href="${esc(productUrl)}" target="_blank" rel="noopener noreferrer">Product publication ↗</a>`:""}${contact}<small>Catalogue review: ${esc(s.reviewed_at)} · not publication date · qualification required</small>${checkEvidence(s.automatic_check)}</td><td><strong>Quote required</strong>${evidence(s,"price","No current executable price per tonne verified. Request a dated quote with currency, grade, quantity and validity.")}</td><td><strong>Dated confirmation required</strong>${evidence(s,"delivery","Request premium, freight, insurance, customs and local delivery separately, with dated availability and delivery confirmation.")}<small>${num(c.lead_days)} days is a buyer route assumption, not this supplier's commitment.</small></td><td><strong>Not calculable</strong>${evidence(s,"change","Two comparable dated supplier quotes are required. A commodity forecast is not a supplier price change.")}</td><td>No current discount verified<small>${s.discount_tiers_t.length?`Ask for ${s.discount_tiers_t.map(t=>`${num(t,1)} t`).join(" / ")} price breaks.`:"Enter demand to calculate RFQ tiers."}</small><small>Discount not assumed or applied.</small></td></tr>`;
  }).join("");
  $("supplier-tabs").querySelectorAll("button").forEach((b,n)=>b.setAttribute("aria-pressed",String(n===index)));
  $("supplier-content").innerHTML=`<p class="supplier-intro"><strong>${esc(c.name)} · ${c.suppliers.length} evidenced candidate${c.suppliers.length===1?"":"s"}</strong><br>RFQ priority: published sales channel, product-fit evidence, then alphabetical ties. Not a supplier price ranking. Country location does not establish shipment origin. ${num(c.lead_days)} days is a route planning assumption, not a vendor commitment.</p><div class="table-wrap" tabindex="0" role="region" aria-label="Suppliers in ${esc(c.name)}"><table class="supplier-table"><thead><tr><th>Supplier & product evidence</th><th>Current quote / t</th><th>Charges & delivery</th><th>Price change</th><th>Quantity discount</th></tr></thead><tbody>${supplierRows}</tbody></table></div>${c.supplier_gap?`<div class="gap-notice">${num(c.supplier_gap)} of 3 supplier slots still need research. No verified candidates have been invented to fill the list. Confirm current product availability, shipment origin and plant qualification.</div>`:""}${(c.coverage_notes || []).map(note=>`<p class="footnote">${esc(note)}</p>`).join("")}`;
  const run=result.supplier_refresh?.last_run;
  const full=result.supplier_catalogue?.find(group=>group.country===c.country);
  if(full && full.suppliers.length>c.suppliers.length)$("supplier-content").innerHTML+=`<p class="footnote"><button type="button" class="text-button" data-browse-suppliers="${esc(c.country)}">View all ${full.suppliers.length} researched candidates in ${esc(c.name)} →</button></p>`;
  $("supplier-content").innerHTML+=`<p class="footnote">Daily evidence monitor: ${run?`${esc(run.status)} · started ${esc(run.started_at)} · finished ${esc(run.finished_at || "not finished")}`:"no run recorded"}. Checks existing public pages, not live quotes or new-supplier discovery.</p>`;
}
function render(d) {
  result=d;
  renderDecision(d);renderCountries(d);renderWorldwide(d);
  const r=d.recommendation, quantity=r.quantity_now_t || r.quantity_later_t;
  $("negotiation").innerHTML=`<div><h3>01 / Price & volume breaks</h3><p>Request a benchmark-linked price with the physical premium shown separately. ${quantity?`Ask for quotes at ${[1,1.5,2].map(m=>`${num(quantity*m,1)} t`).join(", ")}.`:"Confirm a purchase requirement before requesting volume tiers."} Record the discount per tonne, minimum lifting and quote validity. Compare extra carrying cost before taking more stock.</p></div><div><h3>02 / Delivery & landed cost</h3><p>Request a named Incoterm and delivery location, confirmed dispatch and arrival dates, freight, insurance and local charges. Validate the HS code, origin certificate and all duties. ${d.inventory.urgent_bridge_t>0?`Ask for ${num(d.inventory.urgent_bridge_t,1)} t expedited bridge supply before stockout.`:"Prefer staggered deliveries where it protects cash and storage capacity."}</p></div><div><h3>03 / Payment & quality</h3><p>Compare advance-payment discounts against financing cost and counterparty risk. Ask for credit or an LC option, mill certificates, the requested ${esc(d.inputs.specification || "material specification")}, inspection rights and claims terms. These are negotiation requests, not published supplier offers.</p></div>`;
  $("evidence-content").innerHTML=`<ul>${[...d.errors,...d.assumptions,...d.forecast.map(f=>`${f.days/30}-month calibration: ${f.calibration.empirical_coverage===null?"insufficient outcomes":`${num(f.calibration.empirical_coverage*100)}% observed range coverage`} across ${f.calibration.nonoverlapping_outcome_windows} non-overlapping outcome windows. ${f.calibration.note}`)].map(s=>`<li>${esc(s)}</li>`).join("")}</ul>`;
  $("input-summary").textContent=`${d.inputs.material==="ZINC_SHG"?"Zinc SHG":"Steel HRC"} · ${num(d.inventory.on_hand_t,1)} t usable · ${num(d.inputs.monthly_demand_t)} t/month · ${d.mode==="DEMO INVENTORY"?"demo stock override":"entered stock"}`;
  renderSources(d);
  $("results").hidden=false;$("empty-state").hidden=true;$("input-panel").open=false;
  $("decision").scrollIntoView({behavior:"smooth",block:"start"});
}
const analysisStages = [
  ["market", "Fetch market reference, history & FX"],
  ["countries", "Screen historical country costs & logistics"],
  ["analysis", "Calculate forecasts, inventory & catalogue matches"],
  ["tariffs", "Resolve remaining route tariffs"],
  ["sources", "Consolidate recommendation & source trail"]
];
function showProgress(update) {
  const row=$(`progress-${update.stage}`);
  if(!row)return;
  const status={running:"In progress",done:"Complete",warning:"Finished with limitations"}[update.status];
  if(!status)return;
  row.dataset.state=update.status;
  row.querySelector(".progress-state").textContent=status;
  row.querySelector(".progress-detail").textContent=update.detail;
  $("request-summary").textContent=update.detail;
}
async function readAnalysis(response, onProgress) {
  if(!response.ok){const error=await response.json();throw new Error(error.message || "Analysis failed");}
  // Preserve compatibility with non-streaming deployments and test clients.
  if(!response.headers.get("Content-Type")?.includes("application/x-ndjson"))return response.json();
  const reader=response.body.getReader(), decoder=new TextDecoder();
  let pending="", output=null;
  function consume(line){
    if(!line.trim())return;
    const event=JSON.parse(line);
    if(event.type==="progress")onProgress(event);
    if(event.type==="result")output=event.data;
    if(event.type==="error")throw new Error(event.message || "Analysis failed");
  }
  try {
    while(true){
      const {done,value}=await reader.read();
      pending+=decoder.decode(value,{stream:!done});
      const lines=pending.split("\n");pending=lines.pop();lines.forEach(consume);
      if(done){consume(pending);break;}
    }
  } finally {reader.releaseLock();}
  if(!output)throw new Error("The analysis connection ended before a result arrived. Please retry.");
  return output;
}
async function analyze(demo=false) {
  if(busy || !form.reportValidity()) return;
  const payload={...inputs(),demo_stock_breach:demo};
  busy=true;$("input-fields").disabled=true;$("analyze-button").disabled=true;$("demo-button").disabled=true;
  $("error").hidden=true;$("results").hidden=true;$("empty-state").hidden=true;$("request-status").hidden=false;
  $("request-summary").textContent=`${demo?"Applying the demo stock override. ":""}Connecting to the analysis service…`;
  $("request-steps").innerHTML=analysisStages.map(([id,label])=>`<li id="progress-${id}" data-state="waiting"><div><strong>${esc(label)}</strong><span class="progress-state">Waiting</span></div><p class="progress-detail"></p></li>`).join("");
  $("request-elapsed").textContent="0s elapsed";
  $("request-status").scrollIntoView({behavior:"smooth",block:"center"});
  const started=Date.now(), elapsed=setInterval(()=>{
    const seconds=Math.floor((Date.now()-started)/1000);
    $("request-elapsed").textContent=`${seconds}s elapsed${seconds>=30?" · still awaiting the running stage(s)":""}`;
  },1000);
  const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),240000);
  try {
    const response=await fetch("/api/procurement/recommend",{method:"POST",headers:{"Content-Type":"application/json","Accept":"application/x-ndjson"},body:JSON.stringify(payload),signal:controller.signal});
    const data=await readAnalysis(response,showProgress);
    render(data);marketSnapshot();
  } catch(error) {
    $("error").textContent=error.name==="AbortError"?"The public-source lookup timed out. Retry shortly; no order was created.":`Could not complete the recommendation: ${error.message}`;
    $("error").hidden=false;$("empty-state").hidden=false;
  } finally {
    clearTimeout(timeout);clearInterval(elapsed);busy=false;$("request-status").hidden=true;$("input-fields").disabled=false;$("analyze-button").disabled=false;$("demo-button").disabled=false;
  }
}
form.addEventListener("submit",e=>{e.preventDefault();analyze(false);});
$("demo-button").addEventListener("click",()=>analyze(true));
$("supplier-country").addEventListener("change",()=>{
  const group=result?.supplier_catalogue?.find(c=>c.country===$("supplier-country").value);
  if(group)renderSupplierGroup(group);
});
$("refresh-feed").addEventListener("click",marketSnapshot);
form.addEventListener("input",()=>{
  if(busy)return;
  const state=triggerState();
  $("results").hidden=true;$("empty-state").hidden=false;$("error").hidden=true;
  $("input-summary").textContent="Inputs changed — generate an updated recommendation.";
  clearTimeout(autoTimer);
  if(!state.crossed)lastAutoKey=null;
  if($("auto-review").checked && state.crossed){
    autoTimer=setTimeout(()=>{const key=JSON.stringify(inputs());if(key!==lastAutoKey && form.checkValidity()){lastAutoKey=key;analyze(false);}},800);
  }
});
$("material").addEventListener("change",()=>{$("specification").value=$("material").value==="ZINC_SHG"?"SHG zinc · minimum 99.995% purity":"HRC · IS 2062 E250 · confirm thickness and width";renderFeed();});
async function init() {
  triggerState();renderFeed();
  try {
    const response=await fetch("/api/procurement/context");if(!response.ok)throw new Error();const data=await response.json();
    const options=data.countries.map(c=>`<option value="${esc(c.code)}">${esc(c.name)}</option>`).join("");
    $("destination").innerHTML=options;$("preferred").innerHTML='<option value="ANY">No preference</option>'+options;
    $("destination").value="IND";$("preferred").value="IND";quotes=data.quotes || {};renderFeed();
  }catch{marketSnapshot();}
  setInterval(()=>{if(!document.hidden)marketSnapshot();},30000);
}
init();
