// Run with node tests/outlook.browser.cjs <absolute path to playwright package>.
// Requires the isolated synthetic fixture on localhost:8878.
const assert = require('node:assert/strict');
const { chromium } = require(process.argv[2] || 'playwright');
(async () => {
  const browser = await chromium.launch({headless:true, channel:'msedge'});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('https://**', route => route.abort());
    await page.goto('http://127.0.0.1:8878');
    await page.waitForFunction(() => !window.purchaseOutlookBusy && !document.getElementById('outlook-results').hidden);
    assert.equal(await page.locator('#outlook-results .outlook-grid').count(),2);
    assert.equal(await page.locator('#outlook-results .outlook-grid').last().locator('.outlook-card').count(),3);
    await page.selectOption('#cfg-source-country','IND');
    await page.fill('#cfg-safe-pct','7');
    await page.click('#btn-demo-threshold');
    await page.waitForFunction(() => !window.purchaseOutlookBusy && document.getElementById('outlook-mode').textContent.includes('DEMO'));
    assert.match(await page.locator('#outlook-status').innerText(),/Threshold crossed/);
    assert.match(await page.locator('#outlook-results').innerText(),/Domestic Mill A/);
    assert.match(await page.locator('#outlook-results').innerText(),/Tata Steel/);
    assert.match(await page.locator('#outlook-results').innerText(),/SAIL/);
    assert.equal(await page.locator('#btn-approve').isDisabled(),true);
    await page.selectOption('#cfg-material','ZINC_SHG');
    assert.equal(await page.locator('#cfg-baseline').inputValue(),'');
    await page.click('#btn-outlook-run');
    await page.waitForFunction(() => !window.purchaseOutlookBusy && !document.getElementById('outlook-results').hidden);
    assert.match(await page.locator('#outlook-results').innerText(),/Hindustan Zinc/);
    assert.match(await page.locator('#outlook-results').innerText(),/Historical discount lead/);
    assert.match(await page.locator('#outlook-results').innerText(),/Not applied to costs/);
    assert.equal(await page.locator('#btn-approve').isDisabled(),false);
    // Freshness/errors hide previous recommendations instead of leaving them current.
    await page.route('**/api/evaluate',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({status:'unavailable',reason:'TEST SOURCE OUTAGE'})}));
    await page.click('#btn-outlook-run');
    await page.waitForFunction(()=>!window.purchaseOutlookBusy && document.getElementById('outlook-status').textContent.includes('TEST SOURCE OUTAGE'));
    assert.equal(await page.locator('#outlook-results').isVisible(),false);
    await page.unroute('**/api/evaluate');
    await page.setViewportSize({width:390,height:844});
    await page.click('#btn-outlook-run');
    await page.waitForFunction(()=>!window.purchaseOutlookBusy && !document.getElementById('outlook-results').hidden);
    const width = await page.locator('#purchase-outlook').evaluate(n=>n.getBoundingClientRect().width);
    assert.ok(width<=390,'Outlook must fit the mobile viewport');
    assert.deepEqual(errors,[]);
    console.log('PASS: browser inputs, threshold demo, top-three steel/zinc suppliers, discount disclosure, demo approval isolation, baseline reset, outage handling and mobile layout');
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
