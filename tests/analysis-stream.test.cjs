// Network-free contract tests for the streamed response parser.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const code=fs.readFileSync('decision-desk.js','utf8');
const fn=code.slice(code.indexOf('async function readAnalysis('),code.indexOf('async function analyze('));
const context={TextDecoder};vm.createContext(context);vm.runInContext(fn,context);
function streamed(text){
  const bytes=new TextEncoder().encode(text);
  return new Response(new ReadableStream({start(controller){
    // Deliberately split JSON and multi-byte characters across chunks.
    for(let i=0;i<bytes.length;i+=3)controller.enqueue(bytes.slice(i,i+3));
    controller.close();
  }}),{headers:{'Content-Type':'application/x-ndjson'}});
}
(async()=>{
  const updates=[];
  const output=await context.readAnalysis(streamed([
    JSON.stringify({type:'progress',stage:'market',status:'running',detail:'Fetching ₹ reference'}),
    JSON.stringify({type:'result',data:{ok:true}})
  ].join('\n')),event=>updates.push(event));
  assert.equal(output.ok,true);assert.equal(updates[0].detail,'Fetching ₹ reference');
  assert.equal((await context.readAnalysis(new Response('{"ok":true}'),()=>{})).ok,true);
  await assert.rejects(context.readAnalysis(streamed('{"type":"error","message":"SOURCE FAILED"}\n'),()=>{}),/SOURCE FAILED/);
  await assert.rejects(context.readAnalysis(streamed('{"type":"progress"}\n'),()=>{}),/ended before a result/);
  await assert.rejects(context.readAnalysis(new Response('{"message":"BAD INPUT"}',{status:400}),()=>{}),/BAD INPUT/);
  await assert.rejects(context.readAnalysis(streamed('not json\n'),()=>{}));
  console.log('PASS: streamed progress, split UTF-8/JSON chunks, final line, JSON compatibility, server errors and truncated streams.');
})().catch(error=>{console.error(error);process.exitCode=1;});
