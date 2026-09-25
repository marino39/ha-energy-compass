import {dayBounds, periodBounds, periodRows, perKwh, balancePerKwh, mergeChanges, ledger, partialLedger, patchCost, stepPrices, priceEnergy, project, combine, planUsable} from './energy-compass-cost-model.js';

const DEFAULTS = {
  cost_entity: 'sensor.inverter_deye_total_energy_import_cost',
  export_entity: 'sensor.inverter_deye_total_energy_export',
  import_entity: 'sensor.inverter_deye_total_energy_import',
  import_price_entity: 'sensor.cena_pse_kupna_energii',
  plan_entity: 'sensor.energy_compass_home_pilot_plan',
  valid_entity: 'binary_sensor.energy_compass_home_pilot_poprawna_prognoza',
  export_prices_entity: 'sensor.energy_compass_rce_export_forecast',
  runtime_entity: 'sensor.energy_compass_deye_runtime',
  mode_entity: 'input_select.energy_compass_deye_mode',
  deposit_entity: 'sensor.pv_depozyt',
  deposit_backfill: 'pv_costs:depozyt_backfill',
};
const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const moneyFormat = new Intl.NumberFormat('pl-PL', {style:'currency',currency:'PLN'});
const numberFormat = new Intl.NumberFormat('pl-PL', {maximumFractionDigits:2});
const money = value => Number.isFinite(value) ? moneyFormat.format(Math.abs(value) < .005 ? 0 : value) : 'Brak danych';
const sum = rows => rows.reduce((s,r) => s+r.amount,0);
const PERIODS = {day:'Dziś',month:'Miesiąc',year:'Rok'};
const GRAIN = {day:'hour',month:'day',year:'month'};
const unitFormat = new Intl.NumberFormat('pl-PL',{minimumFractionDigits:2,maximumFractionDigits:2});
const unit = value => Number.isFinite(value) ? unitFormat.format(value)+' zł/kWh' : '— zł/kWh';

class PVCostCard extends HTMLElement {
  constructor() { super(); this.attachShadow({mode:'open'}); this.period='day'; this.offset=0; }
  setConfig(config) {
    this.configGeneration=(this.configGeneration||0)+1;
    this.config = {...DEFAULTS,...config}; this.report=null; this.error=null; this.periodData=null;
    this.render(); this.refresh();
  }
  getCardSize() { return 15; }
  getGridOptions() { return {columns:'full',rows:'auto',min_columns:6}; }
  connectedCallback() {
    clearInterval(this.timer);
    this.timer = setInterval(() => this.refresh(), 60_000);
    this.resize = new ResizeObserver(entries => {
      const width = entries[0].contentRect.width;
      if (Math.abs(width-(this.width||0))>5) { this.width=width; if(this.report)this.render(); }
    });
    this.resize.observe(this);
    this.refresh();
  }
  disconnectedCallback() { clearInterval(this.timer); this.timer = null; this.resize?.disconnect(); }
  set hass(value) {
    this._hass = value;
    if (!this.config || !this.isConnected) return;
    if (!this.report && !this.loading) this.refresh();
    else if (this.report) {
      const plan=value.states[this.config.plan_entity];
      const runtime=value.states[this.config.runtime_entity]?.attributes.runtime||{};
      const signature=JSON.stringify([plan?.state,plan?.attributes.refreshing,plan?.attributes.valid_until,
        value.states[this.config.valid_entity]?.state,value.states[this.config.mode_entity]?.state,
        runtime.code,runtime.uncertain,runtime.original_deadline]);
      if(Date.parse(plan?.attributes.generated_at)>this.report.now) this.refresh();
      else if(signature!==this.signature) this.render();
      this.signature=signature;
    }
  }
  clock(time) { return new Intl.DateTimeFormat('pl-PL',{timeZone:this.zone,hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(time); }
  async refresh() {
    if (!this._hass || !this.config || !this.isConnected || this.loading) return;
    this.loading = true;
    const now = Date.now();
    this.zone = this._hass.config.time_zone || 'Europe/Warsaw';
    const bounds = dayBounds(now,this.zone);
    const cfg = this.config;
    const generation = this.configGeneration, period = this.period, offset = this.offset;
    const ids = [cfg.cost_entity,cfg.export_entity,cfg.import_entity];
    try {
      const statistics = await this._hass.callWS({
        type:'recorder/statistics_during_period',
        start_time:new Date(bounds.start-5*60_000).toISOString(),end_time:new Date(now).toISOString(),
        statistic_ids:ids,period:'5minute',types:['sum','state'],
      });
      const ends = ids.map(id => (statistics[id] || []).filter(r=>r.end<=now).at(-1)?.end);
      // Include the preceding bucket so history supplies the state at each ledger boundary.
      const historyStart = Math.min(...ends.filter(Number.isFinite),now)-5*60_000;
      const history = await this._hass.callWS({
        type:'history/history_during_period',start_time:new Date(historyStart).toISOString(),
        end_time:new Date(now).toISOString(),entity_ids:ids,
        minimal_response:false,no_attributes:false,significant_changes_only:false,
      });
      if(generation!==this.configGeneration)return;
      const issues = [];
      const read = (id,label) => {
        try { return ledger(statistics[id]||[],history[id]||[],bounds.start,now); }
        catch(e) { issues.push(`${label}: niepełna historia pomiarów.`); console.warn('PV cost ledger',id,e.message); return null; }
      };
      const exportEnergy = read(cfg.export_entity,'Eksport');
      const importEnergy = read(cfg.import_entity,'Import');
      // A restarted HA cost sensor stays unknown until import changes; keep the verified part.
      const costPartial = partialLedger(statistics[cfg.cost_entity]||[],history[cfg.cost_entity]||[],bounds.start,now);
      let priceHistory = [];
      if (costPartial.until < now) {
        console.warn('PV cost ledger',cfg.cost_entity,costPartial.error);
        try {
          const prices = await this._hass.callWS({
            type:'history/history_during_period',start_time:new Date(costPartial.until).toISOString(),
            end_time:new Date(now).toISOString(),entity_ids:[cfg.import_price_entity],
            minimal_response:false,no_attributes:true,significant_changes_only:false,
          });
          if(generation!==this.configGeneration)return;
          priceHistory = prices[cfg.import_price_entity]||[];
        } catch(e) { console.warn('PV cost price history',e.message); }
      }
      const patched = patchCost(costPartial,importEnergy,stepPrices(priceHistory,now),now);
      const actualCost = patched.segments;
      if (patched.from !== null) {
        const from = patched.from===bounds.start ? '00:00' : this.clock(patched.from);
        issues.push(patched.estimated
          ? `Koszt zakupu od ${from} oszacowany: import × cena G12 (brak zapisanego kosztu, np. po restarcie HA).`
          : `Koszt zakupu: brak danych od ${from} — dla tego okresu przyjęto 0 zł.`);
      }
      let actualExport = null;
      if (exportEnergy) {
        try { actualExport = priceEnergy(exportEnergy,this._hass.states[cfg.export_prices_entity]?.attributes.prices || []); }
        catch(e) { issues.push('Depozyt: brakuje cen RCE dla części dzisiejszego eksportu.'); console.warn('PV cost export',e.message); }
      }
      this.report = {now,bounds,actualCost,actualExport,
        importKwh:importEnergy===null?null:sum(importEnergy),exportKwh:exportEnergy===null?null:sum(exportEnergy),issues};
      this.error = null;
      if (this.period !== 'day' || this.offset !== 0) await this.loadPeriod(generation);
    } catch(e) {
      if(generation!==this.configGeneration)return;
      this.error = 'Nie udało się pobrać historii. Ponów odczyt.';
      console.warn('PV cost history',e.message);
    } finally {
      this.loading = false;
      // A tab switch during this read is loaded right away instead of waiting for the timer.
      if(generation!==this.configGeneration||period!==this.period||offset!==this.offset)this.refresh();
      else this.render();
    }
  }
  // Month/year: recorder day/month `change` before today, plus today's live day-view values.
  async loadPeriod(generation) {
    const r=this.report, cfg=this.config, now=r.now, zone=this.zone, period=this.period, offset=this.offset;
    const month=periodBounds(now,zone,'month');
    const ids={cost:cfg.cost_entity,deposit:cfg.deposit_entity,importKwh:cfg.import_entity,exportKwh:cfg.export_entity};
    const fetch=async (kind,start,end)=>{
      if(end<=start)return {};
      const res=await this._hass.callWS({type:'recorder/statistics_during_period',start_time:new Date(start).toISOString(),
        end_time:new Date(end).toISOString(),statistic_ids:[...Object.values(ids),cfg.deposit_backfill],period:kind,types:['change']});
      const out=Object.fromEntries(Object.entries(ids).map(([k,id])=>[k,(res[id]||[]).filter(x=>x.start<end)]));
      // Backfill (until the counter started) and the live counter cover disjoint time.
      out.deposit=mergeChanges((res[cfg.deposit_backfill]||[]).filter(x=>x.start<end),out.deposit);
      return out;
    };
    const today={cost:r.actualCost?sum(r.actualCost):null,deposit:r.actualExport?sum(r.actualExport):null,importKwh:r.importKwh,exportKwh:r.exportKwh};
    try {
      if(offset!==0){
        // Past period: statistics only, no live values and no forecast.
        const bounds=periodBounds(now,zone,period,offset);
        const data=periodRows(await fetch(GRAIN[period],bounds.start,bounds.end),null);
        if(generation!==this.configGeneration||period!==this.period||offset!==this.offset)return;
        this.periodData={period,offset,bounds,data,now};
        return;
      }
      const monthData=periodRows(await fetch('day',month.start,r.bounds.start),today,r.bounds.start);
      let data=monthData, bounds=month;
      if(period==='year'){
        bounds=periodBounds(now,zone,'year');
        const whole=(key,total)=>monthData.rows.every(x=>x[key]===null)?null:total;
        data=periodRows(await fetch('month',bounds.start,month.start),{cost:whole('cost',monthData.purchase),
          deposit:whole('deposit',monthData.deposit),importKwh:whole('importKwh',monthData.importKwh),exportKwh:whole('exportKwh',monthData.exportKwh),
          costedImportKwh:monthData.costedImportKwh,valuedExportKwh:monthData.valuedExportKwh},month.start);
        const cur=data.rows.at(-1);
        cur.todayMissing=[...new Set([...(cur.todayMissing||[]),...monthData.rows.at(-1).todayMissing])];
        cur.partialDays=monthData.missingCost.filter(x=>!x.today).length;
        // Report the exact first valued day, not just the month bucket.
        if(data.depositFrom===month.start)data.depositFrom=monthData.depositFrom;
      }
      if(generation!==this.configGeneration||period!==this.period||offset!==this.offset)return;
      this.periodData={period,offset,bounds,data,now};
    } catch(e) {
      if(generation!==this.configGeneration)return;
      this.periodData=null;
      this.error='Nie udało się pobrać statystyk okresu. Ponów odczyt.';
      console.warn('PV cost period',e.message);
    }
  }
  forecast(cutoff, end) {
    const plan = this._hass.states[this.config.plan_entity];
    const attrs = plan?.attributes;
    if (!planUsable(plan,this._hass.states[this.config.valid_entity]?.state,Date.now())) return {plan:null,note:'Prognoza niedostępna lub wygasła.'};
    try {
      const p = project(attrs.intervals||[],cutoff,end);
      return {plan:p,note:!p.complete ? `Plan obejmuje czas tylko do ${this.clock(p.end)}. Pełna suma dnia niedostępna.` : attrs.refreshing ? 'Trwa aktualizacja planu; widoczna ostatnia poprawna prognoza.' : 'Prognoza zakłada wykonanie aktualnego planu.'};
    } catch(e) {
      console.warn('PV cost forecast',e.message);
      return {plan:null,note:'Oczekiwanie na wspólny zakres pomiarów i planu.'};
    }
  }
  chart(data,bounds,cutoff) {
    const W=Math.max(320,Math.min(1000,(this.width||this.clientWidth||1048)-48)),H=270,L=48,R=20,T=18,B=38;
    const points=[...data.actualCurve,...data.forecastCurve,...data.actualExportCurve,...data.forecastExportCurve];
    if (!points.length) return '<p class="notice">Wykres pojawi się po odczytaniu historii zakupu lub depozytu.</p>';
    const top=Math.max(1,Math.ceil(Math.max(...points.map(p=>p[1]))*1.15));
    const x=t=>L+(t-bounds.start)/(bounds.end-bounds.start)*(W-L-R);
    const y=v=>H-B-v/top*(H-T-B);
    const path=ps=>ps.map(([t,v],i)=>`${i?'L':'M'}${x(t).toFixed(2)},${y(v).toFixed(2)}`).join(' ');
    const grids=Array.from({length:5},(_,i)=>{
      const v=top*i/4;return `<line x1="${L}" y1="${y(v)}" x2="${W-R}" y2="${y(v)}" class="grid"/><text x="${L-12}" y="${y(v)+5}" text-anchor="end">${numberFormat.format(v)}</text>`;
    }).join('');
    const ticks=[];
    for(let t=bounds.start;t<bounds.end;t+=(W<500?6:4)*3600_000) ticks.push(`<text x="${x(t)}" y="${H-10}" text-anchor="middle">${this.clock(t)}</text>`);
    ticks.push(`<text x="${x(bounds.end)}" y="${H-10}" text-anchor="end">24:00</text>`);
    const marker=x(cutoff);
    const labelX=Math.min(W-R-65,Math.max(L+65,marker));
    return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Narastający koszt zakupu i depozyt: wykonanie do ${this.clock(cutoff)}, prognoza do końca dnia"><title>Koszt zakupu i depozyt w złotych</title>
      ${grids}${ticks.join('')}<text x="${L}" y="12">zł</text>
      <path d="${path(data.actualCurve)}" class="actual-line"/>
      <path d="${path(data.forecastCurve)}" class="forecast-line"/>
      <path d="${path(data.actualExportCurve)}" class="export-line"/>
      <path d="${path(data.forecastExportCurve)}" class="export-forecast-line"/>
      <line x1="${marker}" y1="${T}" x2="${marker}" y2="${H-B}" class="marker"/>
      <text x="${labelX}" y="${T+11}" text-anchor="middle" class="marker-label">Pomiar ${this.clock(cutoff)}</text>
      ${data.actualCurve.length?`<circle cx="${marker}" cy="${y(data.actualImport)}" r="4.5" class="dot"/>`:''}
      ${data.actualExportCurve.length?`<circle cx="${marker}" cy="${y(data.actualExport)}" r="4.5" class="export-dot"/>`:''}
    </svg>`;
  }
  render() {
    if (!this.config) return;
    const r=this.report;
    const zone=this.zone||'Europe/Warsaw';
    const date=new Intl.DateTimeFormat('pl-PL',{timeZone:zone,weekday:'long',day:'numeric',month:'long'}).format(r?.now||Date.now());
    const style=`
      :host{display:block;color:var(--primary-text-color);font-family:var(--primary-font-family,Roboto,sans-serif);--purchase:#42a5f5;--projection:#ffb74d;--export:#66bb6a}
      ha-card{padding:24px;background:var(--ha-card-background,var(--card-background-color));overflow:hidden}
      header{display:flex;justify-content:space-between;align-items:start;gap:12px}h1{font-size:26px;line-height:1.2;font-weight:500;margin:0 0 6px}h2{font-size:18px;font-weight:500;margin:0 0 6px}p{margin:0}small,.muted{color:var(--secondary-text-color);font-size:13px;line-height:1.5}
      button{font:inherit;cursor:pointer;color:var(--primary-text-color);background:transparent;border:1px solid var(--divider-color);border-radius:10px;padding:8px 12px;white-space:nowrap}button:hover{background:var(--secondary-background-color)}button:focus-visible,summary:focus-visible{outline:2px solid var(--purchase);outline-offset:3px}button:disabled{opacity:.5;cursor:wait}
      .kpis{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.25fr) minmax(0,1fr);gap:0;margin:24px 0 18px;border:1px solid var(--divider-color);border-radius:12px}
      .kpi{padding:18px 22px;border-right:1px solid var(--divider-color);min-width:0}.kpi:last-child{border:0}.label{font-size:14px;line-height:1.5;color:var(--secondary-text-color)}.value{font-size:34px;font-weight:500;font-variant-numeric:tabular-nums;line-height:1.6;white-space:nowrap}.kpi.featured{background:var(--secondary-background-color)}.kpi.featured .label{color:var(--primary-text-color);font-weight:500}.kpi.featured .value{font-size:42px;font-weight:600}.kpi small{display:block}.kpi .effect{font-weight:500;color:var(--primary-text-color)}.kpi.improvement .value{color:var(--export)}.kpi.deterioration .value{color:var(--projection)}.value.missing,.kpi.featured .value.missing{font-size:21px;padding:10px 0}
      .chart{margin:24px 0 22px}.chart svg{width:100%;height:auto;min-height:180px;display:block;margin-top:12px}svg text{fill:var(--secondary-text-color);font-size:13px;font-family:inherit}.grid{stroke:var(--divider-color);stroke-dasharray:3 4;stroke-width:1}.actual-line{fill:none;stroke:var(--purchase);stroke-width:3}.forecast-line{fill:none;stroke:var(--purchase);stroke-width:3;stroke-dasharray:7 5}.export-line,.export-forecast-line{fill:none;stroke:var(--export);stroke-width:3}.export-forecast-line{stroke-dasharray:7 5}.export-dot{fill:var(--export)}.marker{stroke:var(--secondary-text-color);stroke-dasharray:3 5;opacity:.7}.marker-label{font-size:12px}.dot{fill:var(--purchase)}
      .legend{display:flex;gap:24px;flex-wrap:wrap;font-size:13px;color:var(--secondary-text-color);margin-top:10px}.legend span{display:flex;gap:8px;align-items:center}.legend i{width:25px;height:0;border-top:3px solid var(--purchase)}.legend .future{border-top-style:dashed}.legend .export{border-color:var(--export)}
      .secondary{display:grid;grid-template-columns:1fr 1fr;gap:24px;padding-top:22px;border-top:1px solid var(--divider-color)}.amounts{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:12px 0}.amounts strong{display:block;font-size:24px;line-height:1.5;font-weight:500;font-variant-numeric:tabular-nums}.deposit strong{color:var(--export)}
      .notice{padding:12px 14px;border-left:3px solid var(--projection);background:var(--secondary-background-color);font-size:13px;line-height:1.5;margin:12px 0}.status{margin-top:20px;line-height:1.7;font-size:13px;color:var(--secondary-text-color)}.status strong{color:var(--primary-text-color);font-weight:500}
      details{margin-top:22px;border-top:1px solid var(--divider-color);padding-top:16px}summary{cursor:pointer;font-size:16px;font-weight:500;padding:4px 0}table{border-collapse:collapse;width:100%;font-size:14px;margin-top:12px;font-variant-numeric:tabular-nums}th,td{padding:10px 12px;text-align:right;border-bottom:1px solid var(--divider-color)}th:first-child,td:first-child{text-align:left}th{font-size:12px;color:var(--secondary-text-color);font-weight:500}.kind{display:block;font-size:11px;color:var(--secondary-text-color)}tr.future-row td{color:var(--projection)}tr.mixed-row td{font-style:italic}.table-wrap{overflow-x:auto}.footnote{margin-top:18px;font-size:12px;color:var(--secondary-text-color);line-height:1.6}
      .actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end}.tabs{display:flex;border:1px solid var(--divider-color);border-radius:10px;overflow:hidden}.tabs button{border:0;border-radius:0;padding:8px 12px}.tabs button+button{border-left:1px solid var(--divider-color)}.tabs button.active{background:var(--secondary-background-color);font-weight:600}
      .bar-cost{fill:var(--purchase)}.bar-deposit{fill:var(--export)}.bar-today{opacity:.6}.legend .box{width:12px;height:12px;border:0;background:var(--purchase)}.legend .box.export{background:var(--export)}
      @media(max-width:650px){header{flex-wrap:wrap}.actions{justify-content:flex-start}ha-card{padding:16px}h1{font-size:23px}.kpis{grid-template-columns:1fr;margin-top:18px}.kpi{padding:12px 14px;border-right:0;border-bottom:1px solid var(--divider-color);display:grid;grid-template-columns:1fr auto;align-items:center;gap:0 12px}.kpi .value{grid-column:2;grid-row:1/3;font-size:29px}.kpi small{grid-column:1/-1}.kpi.featured{display:block;padding:18px 14px}.kpi.featured .value{font-size:40px}.kpi .value.missing{font-size:21px}.secondary{grid-template-columns:1fr;gap:18px}.amounts strong{font-size:23px}.chart svg{min-height:150px}.chart{margin-top:20px}th,td{padding:10px 8px;font-size:12px}svg text{font-size:16px}.marker-label{font-size:14px}}
    `;
    const live=this.offset===0;
    const shown=periodBounds(r?.now||Date.now(),zone,this.period,this.offset).start;
    const periodName=new Intl.DateTimeFormat('pl-PL',{timeZone:zone,...{day:{weekday:'long',day:'numeric',month:'long',year:'numeric'},month:{month:'long',year:'numeric'},year:{year:'numeric'}}[this.period]}).format(shown);
    const title=live?{day:'Prąd dzisiaj',month:'Prąd w tym miesiącu',year:'Prąd w tym roku'}[this.period]:`Prąd — ${periodName}`;
    const subtitle=live?(this.period==='day'?date:periodName):'Zakończony okres · statystyki HA';
    const tabs=`<div class="tabs" role="tablist">${Object.entries(PERIODS).map(([k,l])=>`<button role="tab" data-period="${k}" aria-selected="${k===this.period}" class="${k===this.period?'active':''}">${l}</button>`).join('')}</div>`;
    const step={day:'dzień',month:'miesiąc',year:'rok'}[this.period];
    const nav=`<div class="tabs nav"><button data-shift="-1" aria-label="Poprzedni ${step}">‹</button><button data-shift="1" aria-label="Następny ${step}" ${live?'disabled':''}>›</button></div>`;
    const head=`<header><div><h1>${escape(title)}</h1><p class="muted">${escape(subtitle)} · PGE G12</p></div><div class="actions">${tabs}${nav}<button id="refresh" ${this.loading?'disabled':''} aria-label="Odśwież koszty">${this.loading?'Odczyt…':'Odśwież'}</button></div></header>`;
    const pd=this.periodData;
    if (!r || ((this.period!=='day'||!live) && (pd?.period!==this.period||pd?.offset!==this.offset))) {
      this.shadowRoot.innerHTML=`<style>${style}</style><ha-card>${head}<p class="notice">${this.error||'Pobieranie dzisiejszych kosztów i planu…'}</p></ha-card>`;
      this.bind();return;
    }
    if (this.period!=='day'||!live) { this.renderPeriod(style,head); return; }
    const {plan,note}=this.forecast(r.now,r.bounds.end);
    const data=combine(r.actualCost,r.actualExport,plan,r.bounds.start,r.bounds.end,r.now);
    const stale=Date.now()-r.now>150_000;
    const wrongDay=dayBounds(Date.now(),zone).start!==r.bounds.start;
    // Do not carry yesterday's totals across midnight while the first query runs.
    if (wrongDay) {this.report=null;this.render(); if(!this.loading)this.refresh();return;}
    const value=v=>`<div class="value ${v===null?'missing':''}">${money(v)}</div>`;
    const actualNet=data.actualImport===null||data.actualExport===null?null:data.actualImport-data.actualExport;
    const totalNet=data.totalImport===null||data.totalExport===null?null:data.totalImport-data.totalExport;
    const remainingNet=Number.isFinite(plan?.importCost)&&Number.isFinite(plan?.exportValue)?plan.importCost-plan.exportValue:null;
    const impact=remainingNet===null?'unknown':Math.abs(remainingNet)<.005?'neutral':remainingNet<0?'improvement':'deterioration';
    const impactLabel={unknown:'Pełny plan niedostępny',neutral:'Bez zmiany bilansu',improvement:'Poprawa bilansu',deterioration:'Pogorszenie bilansu'}[impact];
    const breakdown=(purchase,deposit)=>`Zakup ${money(purchase)} − depozyt ${money(deposit)}`;
    const addKwh=(a,b)=>Number.isFinite(a)&&Number.isFinite(b)?a+b:null;
    // Balance per kWh is net cost per kWh drawn from the grid.
    const prices=(purchase,deposit,imp,exp)=>`Za kWh: zakup ${unit(perKwh(purchase,imp))} · depozyt ${unit(perKwh(deposit,exp))} · bilans ${unit(balancePerKwh(purchase===null||deposit===null?null:purchase-deposit,imp))}`;
    const mode=this._hass.states[this.config.mode_entity]?.state;
    const runtime=this._hass.states[this.config.runtime_entity]?.attributes.runtime||{};
    const uncertain=Array.isArray(runtime.uncertain)?runtime.uncertain.length>0:Boolean(runtime.uncertain);
    const followed=mode==='Auto'&&runtime.code==='ok'&&!uncertain&&Date.parse(runtime.original_deadline)>Date.now();
    const issues=[...r.issues];
    if(r.actualExport===null)issues.push('Brak danych o depozycie — przyjęto 0 zł.');
    if(this.error)issues.push(this.error);
    if(stale)issues.push(`Dane nie odświeżały się od ${this.clock(r.now)}. Widoczne ostatnie odczytane wartości.`);
    if(!followed)issues.push('Wykonanie planu nie jest potwierdzone. Prognoza pozostaje scenariuszem według Energy Compass.');
    const opened=this.shadowRoot.querySelector('details')?.open;
    const table=data.hours.map(h=>`<tr class="${h.kind==='forecast'?'future-row':h.kind==='mixed'?'mixed-row':''}"><td>${this.clock(h.start)}–${h.end===r.bounds.end?'24:00':this.clock(h.end)}<span class="kind">${h.kind==='actual'?'Pomiar':h.kind==='forecast'?'Plan':'Pomiar + plan'}</span></td><td>${money(h.importCost)}</td><td>${money(h.exportValue)}</td><td>${money(h.net)}</td></tr>`).join('');
    const generation=Date.parse(this._hass.states[this.config.plan_entity]?.attributes.generated_at);
    this.shadowRoot.innerHTML=`<style>${style}</style><ha-card>${head}
      <div class="kpis">
        <div class="kpi"><div class="label">Bilans dzisiaj</div>${value(actualNet)}<small>${breakdown(data.actualImport,data.actualExport)}<br>${prices(data.actualImport,data.actualExport,r.importKwh,r.exportKwh)}<br>Od 00:00 do ${this.clock(r.now)}</small></div>
        <div class="kpi featured"><div class="label">Przewidywany bilans dnia</div>${value(totalNet)}<small>${breakdown(data.totalImport,data.totalExport)}<br>${prices(data.totalImport,data.totalExport,addKwh(r.importKwh,plan?.importKwh),addKwh(r.exportKwh,plan?.exportKwh))}<br>Pomiar + plan do 24:00</small></div>
        <div class="kpi ${impact}"><div class="label">Wpływ dalszego planu</div>${value(remainingNet===null?null:Math.abs(remainingNet))}<small><span class="effect">${impactLabel}</span><br>Od ${this.clock(r.now)} do 24:00</small></div>
      </div>
      <p class="muted">Bilans = zakup − depozyt. Wartość ujemna oznacza przewagę depozytu; nie jest wypłatą ani kwotą faktury.</p>
      ${issues.map(s=>`<p class="notice">${escape(s)}</p>`).join('')}
      <p class="muted">${escape(note)}</p>
      <section class="chart"><h2>Koszt zakupu i depozyt — narastająco</h2><p class="muted">Dzisiejsze pomiary i dalszy plan do północy</p>${this.chart(data,r.bounds,r.now)}
        <div class="legend"><span><i></i>Zakup · wykonanie</span><span><i class="future"></i>Zakup · prognoza</span><span><i class="export"></i>Depozyt · wykonanie</span><span><i class="export future"></i>Depozyt · prognoza</span></div>
      </section>
      <section class="secondary">
        <div class="deposit"><h2>Wartość eksportu</h2><p class="muted">Szacowany przyrost depozytu prosumenckiego</p><div class="amounts"><div><span class="label">Dotychczas</span><strong>${money(data.actualExport)}</strong></div><div><span class="label">Cały dzień z planem</span><strong>${money(data.totalExport)}</strong></div></div><p class="muted">Jeszcze według planu: ${money(plan?.exportValue??null)}</p></div>
        <div><h2>Koszt zakupu</h2><p class="muted">Energia z sieci według taryfy PGE G12</p><div class="amounts"><div><span class="label">Dotychczas</span><strong>${money(data.actualImport)}</strong></div><div><span class="label">Cały dzień z planem</span><strong>${money(data.totalImport)}</strong></div></div><p class="muted">Jeszcze według planu: ${money(plan?.importCost??null)}</p></div>
      </section>
      <p class="status">Pobrano z sieci: <strong>${r.importKwh===null?'Brak danych':numberFormat.format(r.importKwh)+' kWh'}</strong> · Oddano: <strong>${r.exportKwh===null?'Brak danych':numberFormat.format(r.exportKwh)+' kWh'}</strong><br>Odczyt ${this.clock(r.now)} · Plan ${Number.isFinite(generation)?this.clock(generation):'niedostępny'} · Aktualizacja co minutę</p>
      <details ${opened?'open':''}><summary>Rozbicie godzinowe</summary><div class="table-wrap"><table><thead><tr><th>Godzina</th><th>Zakup</th><th>Depozyt</th><th>Bilans</th></tr></thead><tbody>${table}</tbody></table></div></details>
      <p class="footnote">Kwoty w zł. Zakup według zapisanych kosztów PGE G12, z VAT i opłatami zmiennymi. Eksport: ceny RCE zgodne z planem, minimum 0 i mnożnik depozytu 1,23. Rozkład eksportu oszacowany z przyrostów licznika w przedziałach pomiarowych. Bez opłat stałych i kosztu zużycia baterii. Bilans nie jest kwotą faktury ani wypłatą gotówki.</p>
    </ha-card>`;
    this.bind();
  }
  renderPeriod(style,head) {
    const {period,offset,bounds,data,now}=this.periodData, zone=this.zone||'Europe/Warsaw', live=offset===0;
    const grain=GRAIN[period];
    const label=t=>grain==='hour'?this.clock(t):grain==='day'?new Intl.DateTimeFormat('pl-PL',{timeZone:zone,day:'numeric',month:'short'}).format(t):new Intl.DateTimeFormat('pl-PL',{timeZone:zone,month:'long'}).format(t);
    const dateLabel=t=>new Intl.DateTimeFormat('pl-PL',{timeZone:zone,day:'numeric',month:'long',year:'numeric'}).format(t);
    const value=v=>`<div class="value ${v===null?'missing':''}">${money(v)}</div>`;
    const issues=[];
    if(data.depositFrom===null)issues.push('Brak wyceny depozytu w tym okresie (licznik sensor.pv_depozyt dopiero zbiera dane) — przyjęto 0 zł, bilans zawyżony.');
    else if(data.missingDeposit.length)issues.push(`Depozyt wyceniony od ${grain==='hour'?this.clock(data.depositFrom):dateLabel(data.depositFrom)}; ${{hour:'wcześniejsze godziny',day:'wcześniejsze dni',month:'wcześniejsze miesiące'}[grain]} bez wyceny — przyjęto 0 zł, bilans zawyżony.`);
    if(!data.rows.length)issues.push('Brak statystyk dla tego okresu.');
    const noCost=data.missingCost.filter(x=>!x.today);
    if(noCost.length)issues.push(`Brak zapisanego kosztu zakupu: ${noCost.map(x=>label(x.start)).join(', ')} — pominięto.`);
    const partial=data.rows.at(-1)?.partialDays;
    if(partial)issues.push(`Bieżący miesiąc: ${partial} dni bez zapisanego kosztu zakupu.`);
    const names={cost:'koszt zakupu',deposit:'depozyt',importKwh:'import',exportKwh:'eksport'};
    const todayMissing=data.rows.at(-1)?.todayMissing||[];
    if(todayMissing.length)issues.push(`Dzisiejsze dane niepełne (${todayMissing.map(k=>names[k]).join(', ')}) — widoczne pozostałe dni.`);
    if(live)for(const s of this.report.issues)issues.push(`Dziś: ${s}`);
    if(this.error)issues.push(this.error);
    const rows=data.rows.map(x=>({...x,net:x.cost===null?null:x.cost-(x.deposit??0)}));
    const kwh=v=>v===null?'—':numberFormat.format(v);
    const table=rows.map(x=>`<tr class="${x.today?'mixed-row':''}"><td>${escape(label(x.start))}${x.today?'<span class="kind">do '+this.clock(now)+'</span>':''}</td><td>${money(x.cost)}</td><td>${x.deposit===null?'brak wyceny':money(x.deposit)}</td><td>${money(x.net)}</td><td>${kwh(x.importKwh)}</td><td>${kwh(x.exportKwh)}</td></tr>`).join('');
    const opened=this.shadowRoot.querySelector('details')?.open;
    const scope={day:'dnia',month:'miesiąca',year:'roku'}[period];
    const range=live?`Od ${escape(dateLabel(bounds.start))} do ${this.clock(now)}`:grain==='hour'?'Cały dzień':`Od ${escape(dateLabel(bounds.start))} do ${escape(dateLabel(bounds.end-1))}`;
    this.shadowRoot.innerHTML=`<style>${style}</style><ha-card>${head}
      <div class="kpis">
        <div class="kpi"><div class="label">Zakup</div>${value(data.purchase)}<small>${kwh(data.importKwh)} kWh z sieci<br><span class="effect">${unit(data.purchasePerKwh)}</span></small></div>
        <div class="kpi featured"><div class="label">Bilans ${scope}</div>${value(data.balance)}<small>Zakup ${money(data.purchase)} − depozyt ${money(data.deposit)}<br><span class="effect">${unit(data.balancePerKwh)}</span> pobranej z sieci<br>${range}</small></div>
        <div class="kpi"><div class="label">Depozyt</div>${value(data.deposit)}<small>${kwh(data.exportKwh)} kWh oddane<br><span class="effect">${unit(data.depositPerKwh)}</span></small></div>
      </div>
      <p class="muted">Bilans = zakup − depozyt. Tylko pomiary, bez prognozy. Cena bilansu za kWh = bilans / kWh pobrane z sieci. Wartość ujemna oznacza przewagę depozytu; nie jest kwotą faktury.</p>
      ${issues.map(s=>`<p class="notice">${escape(s)}</p>`).join('')}
      <section class="chart"><h2>Zakup i depozyt — ${{hour:'godzinowo',day:'dziennie',month:'miesięcznie'}[grain]}</h2>${this.bars(rows,label)}
        <div class="legend"><span><i class="box"></i>Zakup</span><span><i class="box export"></i>Depozyt</span></div>
      </section>
      <details ${opened?'open':''}><summary>Rozbicie ${{hour:'godzinowe',day:'dzienne',month:'miesięczne'}[grain]}</summary><div class="table-wrap"><table><thead><tr><th>${{hour:'Godzina',day:'Dzień',month:'Miesiąc'}[grain]}</th><th>Zakup</th><th>Depozyt</th><th>Bilans</th><th>Import kWh</th><th>Eksport kWh</th></tr></thead><tbody>${table}</tbody></table></div></details>
      <p class="footnote">Kwoty w zł. Zakup według zapisanych kosztów PGE G12 (statystyki HA). Depozyt: ceny RCE z minimum 0 i mnożnikiem 1,23. Od 25.09.2026 10:00 licznik sensor.pv_depozyt wycenia każdy przyrost eksportu ceną bieżącego kwadransu. Wcześniej jest to szacunek z eksportu godzinowego: od 15.09.2026 wyceniany kwadransowo, przed tą datą średnią ceną godziny (dane PSE). ${live?'Dziś liczone jak w widoku „Dziś”. ':''}Bez opłat stałych.</p>
    </ha-card>`;
    this.bind();
  }
  bars(rows,label) {
    if(!rows.length)return '<p class="notice">Brak danych do wykresu.</p>';
    const W=Math.max(320,Math.min(1000,(this.width||this.clientWidth||1048)-48)),H=240,L=48,R=12,T=14,B=34;
    const top=Math.max(1,Math.ceil(Math.max(...rows.flatMap(r=>[r.cost??0,r.deposit??0]))*1.15));
    const slot=(W-L-R)/rows.length, bw=Math.max(2,Math.min(22,slot*.38));
    const y=v=>H-B-v/top*(H-T-B);
    const grids=Array.from({length:5},(_,i)=>{const v=top*i/4;return `<line x1="${L}" y1="${y(v)}" x2="${W-R}" y2="${y(v)}" class="grid"/><text x="${L-10}" y="${y(v)+5}" text-anchor="end">${numberFormat.format(v)}</text>`;}).join('');
    const every=Math.ceil(rows.length/(W<500?6:12));
    const bars=rows.map((r,i)=>{
      const cx=L+slot*(i+.5), cls=r.today?' bar-today':'';
      const bar=(v,x,c)=>v===null?'':`<rect x="${x.toFixed(1)}" y="${y(v).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(0,H-B-y(v)).toFixed(1)}" class="${c}${cls}"><title>${escape(label(r.start))}: ${money(v)}</title></rect>`;
      const tick=i%every===0?`<text x="${cx}" y="${H-10}" text-anchor="middle">${escape(label(r.start))}</text>`:'';
      return bar(r.cost,cx-bw-1,'bar-cost')+bar(r.deposit,cx+1,'bar-deposit')+tick;
    }).join('');
    return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Zakup i depozyt w kolejnych okresach"><text x="${L}" y="11">zł</text>${grids}${bars}</svg>`;
  }
  bind(){
    this.shadowRoot.querySelector('#refresh')?.addEventListener('click',()=>this.refresh());
    this.shadowRoot.querySelectorAll('[data-period]').forEach(b=>b.addEventListener('click',()=>{
      if(b.dataset.period===this.period&&this.offset===0)return;
      this.period=b.dataset.period; this.offset=0; this.render(); this.refresh();
    }));
    this.shadowRoot.querySelectorAll('[data-shift]').forEach(b=>b.addEventListener('click',()=>{
      this.offset=Math.min(0,this.offset+Number(b.dataset.shift)); this.render(); this.refresh();
    }));
  }
}
if(!customElements.get('pv-cost-card'))customElements.define('pv-cost-card',PVCostCard);
