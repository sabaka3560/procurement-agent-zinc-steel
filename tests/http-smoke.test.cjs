// Read-only focused-workspace checks; never calls approval/order/chat endpoints.
const assert=require('node:assert/strict');
const base=process.argv[2] || 'http://127.0.0.1:8878';
(async()=>{
 let checks=0;
 for(const path of ['/','/decision-desk.js','/decision-desk.css','/api/procurement/context','/api/procurement/market','/legacy']){
  const response=await fetch(base+path,{signal:AbortSignal.timeout(15000)});
  assert.equal(response.status,200,path);checks++;
 }
 const context=await (await fetch(base+'/api/procurement/context')).json();
 assert.equal(context.countries.length,42);checks++;
 for(const payload of [[],null,{material:'GOLD'},{destination:'BAD'},{preferred_source_country:'BAD'},
   {on_hand_t:-1},{monthly_demand_t:true},{safe_value_pct:100},{demo_stock_breach:'yes'}]){
  const response=await fetch(base+'/api/procurement/recommend',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:AbortSignal.timeout(15000)});
  assert.equal(response.status,400,JSON.stringify(payload));
  assert.ok((await response.json()).message);checks++;
 }
 for(const [body,status] of [['{broken',400],['x'.repeat(100001),413]]){
  const response=await fetch(base+'/api/procurement/recommend',{method:'POST',headers:{'Content-Type':'application/json'},body,signal:AbortSignal.timeout(15000)});
  assert.equal(response.status,status);checks++;
 }
 const responses=await Promise.all(Array.from({length:6},()=>fetch(base+'/api/procurement/context',{signal:AbortSignal.timeout(15000)})));
 assert.ok(responses.every(response=>response.status===200));checks++;
 console.log(`PASS: ${checks} HTTP checks on ${base}: assets, context, both UI routes, invalid types/ranges, malformed JSON, body limit and concurrent reads.`);
})().catch(error=>{console.error(error);process.exitCode=1;});
