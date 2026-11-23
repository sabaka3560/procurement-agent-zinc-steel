/* No supplier or user strings enter HTML without escaping. */
(() => {
  const root = document.getElementById('purchase-outlook');
  const status = document.getElementById('outlook-status');
  const results = document.getElementById('outlook-results');
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const controls = document.createElement('div');
  controls.className = 'outlook-controls';
  controls.innerHTML = `
    <label>Preferred source country<select id="cfg-source-country">
      <option value="ANY">Any country</option><option value="IND">India</option><option value="JPN">Japan</option>
      <option value="KOR">South Korea</option><option value="VNM">Vietnam</option><option value="CHN">China</option>
      <option value="IDN">Indonesia</option><option value="AUS">Australia</option><option value="ARE">UAE</option>
    </select></label>
    <label>Safe price rise (%)<input id="cfg-safe-pct" type="number" min="0.1" max="100" step="0.1" value="5" required></label>
    <label>Monitoring baseline / tonne<input id="cfg-baseline" type="number" min="0.000001" step="any" placeholder="First fetched price"></label>
    <button id="btn-outlook-run" type="button">Refresh forecast &amp; shortlist</button>
    <button id="btn-demo-threshold" class="secondary" type="button">Demo: cross safe value</button>
    <button id="btn-baseline-reset" class="secondary" type="button">Reset baseline</button>`;
  root.insertBefore(controls, status);
  const el = id => document.getElementById(id);
  const key = () => `procurement-baseline:${el('cfg-material').value}:${el('cfg-dest').value}`;
  function restore() {
    try { el('cfg-baseline').value = sessionStorage.getItem(key()) || ''; }
    catch (_) { el('cfg-baseline').value = ''; }
    results.hidden = true;
    status.textContent = 'Inputs changed. Refresh the advisory to update this material and destination.';
  }
  for (const id of ['cfg-material','cfg-dest']) el(id).addEventListener('change', restore);
  for (const id of ['cfg-source-country','cfg-safe-pct','cfg-baseline']) el(id).addEventListener('change', () => {
    results.hidden = true;
    status.textContent = 'Inputs changed. Refresh to apply the new preference or price threshold.';
  });
  document.querySelectorAll('.config-strip input').forEach(n => n.addEventListener('change', () => {
    results.hidden = true;
    status.textContent = 'Company inputs changed. Refresh to recalculate the timing, quantity and supplier shortlist.';
  }));
  el('btn-baseline-reset').onclick = () => {
    try { sessionStorage.removeItem(key()); } catch (_) {}
    el('cfg-baseline').value = '';
    results.hidden = true;
    status.textContent = 'The next successful refresh will set a new baseline from the market reference.';
  };
  el('btn-outlook-run').onclick = () => window.runLiveEvaluation(false);
  el('btn-demo-threshold').onclick = () => window.runLiveEvaluation(true);
  restore();

  window.purchaseOutlookInputs = demo => {
    for (const id of ['cfg-safe-pct', 'cfg-baseline']) if (!el(id).reportValidity()) return null;
    const safe = Number(el('cfg-safe-pct').value);
    if (!Number.isFinite(safe) || safe < .1 || safe > 100) return null;
    return {preferred_source_country: el('cfg-source-country').value, safe_value_pct: safe,
      monitoring_baseline_price: el('cfg-baseline').value ? Number(el('cfg-baseline').value) : null,
      demo_threshold_crossed: Boolean(demo)};
  };
  window.purchaseOutlookLoading = demo => {
    for (const id of ['btn-outlook-run','btn-demo-threshold','btn-run-eval','btn-baseline-reset']) el(id).disabled = true;
    // Freeze inputs so a late response cannot be displayed under a different material.
    document.querySelectorAll('.config-strip input,.config-strip select,.outlook-controls input,.outlook-controls select').forEach(n => n.disabled = true);
    results.hidden = true;
    status.textContent = demo ? 'Simulating a threshold crossing; refreshing the forecast and country evidence…' : 'Fetching market evidence and comparing 30, 60 and 90 day scenarios…';
  };
  window.purchaseOutlookLoaded = () => {
    for (const id of ['btn-outlook-run','btn-demo-threshold','btn-run-eval','btn-baseline-reset']) el(id).disabled = false;
    document.querySelectorAll('.config-strip input,.config-strip select,.outlook-controls input,.outlook-controls select').forEach(n => n.disabled = false);
  };
  window.purchaseOutlookError = message => {
    results.hidden = true;
    status.textContent = `Could not produce an updated advisory: ${message}. Previous results are hidden.`;
    el('outlook-mode').textContent = 'Unavailable';
  };
  window.renderPurchaseOutlook = o => {
    if (!o) return window.purchaseOutlookError('No outlook returned');
    const money = v => v == null ? 'Unavailable' : `${o.currency} ${Number(v).toLocaleString(undefined, {maximumFractionDigits: 2})}`;
    const pct = v => v == null ? 'Not measured' : `${(v * 100).toFixed(1)}%`;
    const mode = el('outlook-mode');
    const demo = o.mode === 'DEMO_SIMULATION';
    mode.textContent = demo ? 'DEMO · simulated price crossing' : 'Latest available data';
    mode.className = `outlook-pill${demo ? ' demo' : ''}`;
    el('cfg-baseline').value = o.trigger.baseline_price;
    try { sessionStorage.setItem(key(), o.trigger.baseline_price); } catch (_) {}
    const t = o.timing;
    status.textContent = `${o.trigger.crossed ? 'Threshold crossed — review triggered.' : 'Price is within the safe threshold.'} Baseline ${money(o.trigger.baseline_price)} → ceiling ${money(o.trigger.threshold_price)} (+${o.trigger.safe_value_pct}%). ${demo ? 'Simulated' : 'Observed'} comparison ${money(o.trigger.evaluated_price)}. ${o.trigger.note}`;
    const names = {SECURE_NEAR_TERM_SUPPLY: 'Secure near-term supply now', WAIT_AND_REVIEW: `Wait toward ${t.buy_after_days} days, with ongoing review`, REVIEW_QUOTES_NOW: 'Review supplier quotes now', NO_PURCHASE_REQUIREMENT: 'Monitor — no consumption requirement', REFRESH_MARKET_DATA: 'Refresh market data before deciding when to buy'};
    const sourceName = t.recommended_country_name || 'More supplier research needed';
    const preferredGap = t.preferred_country !== 'ANY' && !t.preferred_country_has_researched_suppliers;
    const headline = `<div class="outlook-decision"><strong>${esc(names[t.action] || t.action)}</strong>
      <p>${esc(t.reason)}</p><p>Source country for RFQ: <strong>${esc(sourceName)}</strong>.
      Near-term supply to confirm: <strong>${esc(t.request_quantity_now_t.toLocaleString())} tonnes</strong>.
      This quantity excludes undated pipeline; reconcile receipt dates before placing an order.</p>
      <p class="outlook-muted">${esc(t.country_reason)} ${t.lowest_import_screen_country ? `Lowest historical import screen: ${esc(t.lowest_import_screen_country)}.` : ''}
      ${t.preferred_premium_vs_lowest_import_pct != null ? `Chosen-country screening premium: ${esc(t.preferred_premium_vs_lowest_import_pct)}%.` : ''}</p>
      ${preferredGap ? '<p class="outlook-alert">No supplier for this material in your preferred country is verified in the research catalogue. The top three below are clearly marked alternatives.</p>' : ''}</div>`;
    const forecast = o.forecast.map(r => `<article class="outlook-card"><span class="outlook-pill">${esc(r.calendar_days)} days · ${esc(r.target_date)}</span>
      <strong class="big">${esc(money(r.p50))}</strong><p>Median scenario / tonne</p>
      <p>80% model range: ${esc(money(r.p10))} – ${esc(money(r.p90))}</p>
      <p>Chance below ${demo ? 'simulated' : 'current'} price: <strong>${pct(r.probability_cheaper_than_now)}</strong></p>
      <p>Median price saving: ${esc(r.median_saving_vs_now_pct)}%<br>Buy now + assumed carrying cost: ${esc(money(r.buy_now_plus_carry_per_t))}/t</p>
      <p>${r.waiting_feasible_on_known_stock ? 'Known stock supports waiting under stated assumptions.' : `Waiting requires ${esc(r.bridge_supply_needed_t)}t of dated bridge supply.`}</p>
      <p class="outlook-muted">Backtest interval coverage: ${pct(r.calibration.empirical_coverage)} across ${r.calibration.nonoverlapping_outcome_windows} non-overlapping outcomes. Sparse evidence.</p></article>`).join('');
    const rows = o.countries.map(c => `<tr><td><strong>${esc(c.name)}</strong>${c.preferred ? ' · preferred' : ''}</td>
      <td>${esc(money(c.screen_cost_local))}<br><small>${esc(c.price_basis.replaceAll('_',' '))}</small></td>
      <td>${c.duty_pct == null ? 'Unavailable' : `${esc(c.duty_pct)}%`}<br>${esc(c.duty_basis)}<br><small>${c.duty_verified ? 'Tariff layer permits screening use; confirm origin.' : 'Customs sign-off / quote required'}</small></td>
      <td>${c.freight_estimate_usd_per_t == null ? 'Quote required' : `USD ${esc(c.freight_estimate_usd_per_t)}/t estimate`}<br><small>Already included in historical CIF; not added twice.</small></td>
      <td>${c.future_screen_scenarios.map(f => `${f.calendar_days}d median: ${esc(money(f.p50))}`).join('<br>') || 'No price scenario'}</td>
      <td><details><summary>Source &amp; assumptions</summary><p>${esc(c.source)}</p>${c.surcharges.map(s => `<p>${esc(s.kind)}: ${esc(s.pct)}% · ${esc(s.from)} to ${esc(s.to)}</p>`).join('')}${c.notes.map(n => `<p>${esc(n)}</p>`).join('')}</details></td></tr>`).join('');
    const suppliers = o.top_suppliers.map(s => `<article class="outlook-card"><span class="outlook-pill">#${s.rank} · ${esc(s.country_name)}${s.preferred_country_match ? ' · preferred' : ' · alternative'}</span>
      <h3>${esc(s.name)}</h3><p>${esc(s.evidence)}</p><p class="outlook-muted">${esc(s.rank_reason)}</p>
      <p><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">Supplier product evidence ↗</a> · Reviewed ${esc(s.reviewed_at)}</p>
      <p><strong>Supplier price &amp; delivery:</strong> RFQ required.<br><strong>Quantity discount:</strong> ${esc(s.quantity_discount.note)}</p>
      <p>Ask for price breaks at ${s.quantity_discount.rfq_tiers_t.map(v => `${esc(v)}t`).join(' / ') || 'the required quantity'}. These are requested tiers, not supplier offers.</p>
      ${s.quantity_discount.historical_lead ? `<p class="outlook-alert"><strong>Historical discount lead · ${esc(s.quantity_discount.historical_lead.date)}</strong><br>${esc(s.quantity_discount.historical_lead.summary)} <a href="${esc(s.quantity_discount.historical_lead.url)}" target="_blank" rel="noopener noreferrer">Dated circular ↗</a><br>Not applied to costs or supplier ranking.</p>` : ''}
      <p class="outlook-muted">${esc(s.approval_status)}</p><details><summary>Negotiation checklist</summary><p>${esc(s.quantity_discount.request)}</p></details></article>`).join('');
    results.innerHTML = `${headline}<h3>Latest-data forecast · now versus the next three months</h3>
      <p class="outlook-muted">Live reference: ${esc(money(o.live_reference_price))}/t. Source: ${esc(o.price_observation.source)} · observed ${esc(o.price_observation.observed_at)} · quality ${esc(o.price_observation.quality)}. History through ${esc(o.price_observation.history_observed_at || o.price_observation.observed_at)}. Forecast refreshed ${esc(o.generated_at)}.</p>
      ${o.market_data_stale ? '<p class="outlook-alert">Benchmark or history is stale/undated. These ranges cannot support a current market-timing decision.</p>' : ''}
      <p class="outlook-alert">${esc(o.series_basis)}</p><div class="outlook-grid">${forecast}</div>
      <h3>Source countries · prices, duties and import assumptions</h3><p class="outlook-muted">Historical import screens and domestic benchmarks have different cost bases. Obtain comparable delivered quotes. Future country costs are benchmark-scaled scenarios with unchanged FX and duties.</p>
      <div class="outlook-table"><table><thead><tr><th>Country</th><th>Current reference / t</th><th>Duty &amp; tax basis</th><th>Freight</th><th>2–3 month scenario / t</th><th>Evidence</th></tr></thead><tbody>${rows}</tbody></table></div>
      <h3>Top three suppliers to approach</h3><p class="outlook-muted">A researched RFQ shortlist. Preference first, then comparable country screening costs. Supplier prices and discounts are not yet verified.</p><div class="outlook-grid">${suppliers}</div>
      <details><summary>Planning assumptions and research limits</summary>${o.assumptions.map(a => `<p>${esc(a)}</p>`).join('')}<p>${esc(o.supplier_research_note)}</p></details>`;
    results.hidden = false;
  };
})();
