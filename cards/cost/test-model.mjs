import test from 'node:test';
import assert from 'node:assert/strict';
import {dayBounds, ledger, partialLedger, patchCost, stepPrices, priceEnergy, project, combine, parseTime} from './energy-compass-cost-model.js';
const m=60_000;
const row=(start,sum,state=sum)=>({start,end:start+5*m,sum,state});
const hist=(lu,s,reset='a')=>({lu:lu/1000,s:String(s),a:{last_reset:reset}});
const plan=(start,end,imp,exp,buy=1,sell=.5)=>({start:new Date(start).toISOString(),end:new Date(end).toISOString(),grid_import_kwh:imp,grid_export_kwh:exp,buy_per_kwh:buy,sell_per_kwh:sell});

test('Warsaw local midnight and DST days use civil-day boundaries',()=>{
 assert.deepEqual(dayBounds(Date.parse('2026-09-19T19:00Z'),'Europe/Warsaw'),{start:Date.parse('2026-09-18T22:00Z'),end:Date.parse('2026-09-19T22:00Z')});
 for(const [date,hours] of [['2026-03-29T12:00Z',23],['2026-10-25T12:00Z',25]]){const b=dayBounds(Date.parse(date),'Europe/Warsaw');assert.equal((b.end-b.start)/m/60,hours);}
});
test('compact HA price timestamps retain UTC',()=>assert.equal(parseTime('20260919T2215Z'),Date.parse('2026-09-19T22:15Z')));
test('statistical sum survives raw counter restart and adds only the unrecorded tail',()=>{
 const r=ledger([row(-5*m,100,8),row(0,102,10),row(5*m,103,1)],[hist(9*m,1,'b'),hist(12*m,1.5,'b')],0,15*m);
 assert.equal(r.reduce((s,r)=>s+r.amount,0),3.5);
 assert.equal(r[0].start,0);assert.equal(r.at(-1).end,15*m);
});
test('history tail handles reset even when new value exceeds previous value',()=>{
 const r=ledger([row(-5*m,100,1)],[hist(-m,1),hist(m,2,'b')],0,2*m);
 assert.equal(r.reduce((s,r)=>s+r.amount,0),2);
});
test('missing midnight baseline, statistic gap, unavailable history never become zero',()=>{
 assert.throws(()=>ledger([row(0,2)],[],0,10*m),/baseline/);
 assert.throws(()=>ledger([row(-5*m,0),row(5*m,2)],[],0,10*m),/gap/);
 assert.throws(()=>ledger([row(-5*m,0)],[hist(-m,0),hist(m,'unavailable')],0,2*m),/history/);
});
test('missing or mismatched live-tail anchor is rejected',()=>{
 assert.throws(()=>ledger([row(-5*m,100,1)],[],0,m),/history/);
 assert.throws(()=>ledger([row(-5*m,100,1)],[hist(-m,2)],0,m),/anchor/);
});
test('null monetary value cannot be interpreted as zero',()=>{
 assert.throws(()=>ledger([row(-5*m,0),row(0,null)],[],0,5*m),/statistic/);
});
test('export uses normalized quarter-hour prices exactly once',()=>{
 const priced=priceEnergy([{start:0,end:30*m,amount:2}],[{start:0,end:15*m,price:.123},{start:15*m,end:30*m,price:.246}]);
 assert.ok(Math.abs(priced.reduce((s,r)=>s+r.amount,0)-.369)<1e-10);
});
test('missing price rejects nonzero export but zero export needs no price',()=>{
 assert.throws(()=>priceEnergy([{start:0,end:30*m,amount:2}],[{start:0,end:15*m,price:1}]),/price/);
 assert.deepEqual(priceEnergy([{start:0,end:30*m,amount:0}],[]),[{start:0,end:30*m,amount:0}]);
});
test('forecast clips both the first interval and midnight without double counting',()=>{
 const p=project([plan(0,60*m,4,2),plan(60*m,120*m,6,0)],30*m,90*m);
 assert.equal(p.importCost,5);assert.equal(p.exportValue,.5);assert.equal(p.importKwh,5);assert.equal(p.complete,true);
 assert.equal(p.rows[0].start,30*m);assert.equal(p.rows.at(-1).end,90*m);
});
test('incomplete forecasts retain partial values but cannot claim full-day total',()=>{
 const p=project([plan(0,60*m,4,0)],30*m,90*m);
 assert.equal(p.complete,false);assert.equal(p.importCost,null);assert.equal(p.partialImportCost,2);
});
test('forecast gaps, overlap and invalid fields cannot be added as complete plan',()=>{
 for(const rows of [[plan(0,20*m,1,0),plan(30*m,60*m,1,0)],[plan(0,40*m,1,0),plan(30*m,60*m,1,0)],[{...plan(0,60*m,1,0),buy_per_kwh:null}]])assert.throws(()=>project(rows,0,60*m));
});
test('zero-import full forecast is valid zero and tiny solver residues do not create income',()=>{
 const p=project([plan(0,60*m,-1e-15,0)],0,60*m);assert.equal(p.importCost,0);assert.equal(p.complete,true);
});
test('hourly actual and future slices reconcile to separate and full totals',()=>{
 const p=project([plan(30*m,120*m,3,1.5,2,1)],30*m,120*m);
 const result=combine([{start:0,end:30*m,amount:1}],[{start:0,end:30*m,amount:.25}],p,0,120*m,30*m);
 assert.equal(result.actualImport,1);assert.equal(result.totalImport,7);assert.equal(result.totalExport,1.75);
 assert.equal(result.hours[0].importCost,3);assert.equal(result.hours[1].importCost,4);
 assert.equal(result.actualCurve.at(-1)[1],1);assert.equal(result.forecastCurve.at(-1)[1],7);
});

test('missing export history does not hide valid future-hour export or purchase totals',()=>{
 const p=project([plan(60*m,120*m,2,1)],60*m,120*m);
 const r=combine([{start:0,end:60*m,amount:1}],null,p,0,120*m,60*m);
 assert.equal(r.totalImport,3);assert.equal(r.totalExport,.5);assert.equal(r.hours[0].exportValue,0);assert.equal(r.hours[0].net,1);assert.equal(r.hours[1].exportValue,.5);
});

test('an unexplained counter decrease is not mistaken for an entire meter reset',()=>{
 assert.throws(()=>ledger([row(-5*m,100,4477.8)],[hist(-m,4477.8),hist(m,4477.7)],0,2*m),/decrease/);
 assert.throws(()=>ledger([row(-5*m,100,.625)],[hist(-m,.625),hist(m,.60)],0,2*m),/decrease/);
});

test('recalculation grace cannot keep an expired plan alive indefinitely',async()=>{
 const {planUsable}=await import('./energy-compass-cost-model.js');
 assert.equal(typeof planUsable,'function');
 const p={state:'2026-09-19T00:00:00Z',attributes:{valid_until:'2026-09-19T00:15:00Z',refreshing:true}};
 assert.equal(planUsable(p,'on',Date.parse('2026-09-19T00:16:00Z')),true);
 assert.equal(planUsable(p,'on',Date.parse('2026-09-19T00:17:01Z')),false);
 assert.equal(planUsable(p,'off',Date.parse('2026-09-19T00:14:00Z')),false);
 p.attributes.refreshing=false;
 assert.equal(planUsable(p,'on',Date.parse('2026-09-19T00:16:00Z')),false);
});

test('deposit curves join at measured value and finish at full-day deposit',()=>{
 const p=project([plan(30*m,120*m,3,1.5,2,1)],30*m,120*m);
 const r=combine([{start:0,end:30*m,amount:1}],[{start:0,end:15*m,amount:.10},{start:15*m,end:30*m,amount:.15}],p,0,120*m,30*m);
 assert.deepEqual(r.actualExportCurve,[[0,0],[15*m,.10],[30*m,.25]]);
 assert.deepEqual(r.forecastExportCurve,[[30*m,.25],[120*m,1.75]]);
 assert.equal(r.forecastExportCurve.at(-1)[1],r.totalExport);
});
test('missing deposit uses zero through cutoff and keeps projected export',()=>{
 const p=project([plan(30*m,120*m,3,1.5)],30*m,120*m);
 const r=combine([{start:0,end:30*m,amount:1}],null,p,0,120*m,30*m);
 assert.deepEqual(r.actualExportCurve,[[0,0],[30*m,0]]);assert.deepEqual(r.forecastExportCurve,[[30*m,0],[120*m,.75]]);
 assert.equal(r.forecastCurve.at(-1)[1],4);
});
test('deposit fallback does not fill missing purchase or missing future plan',()=>{
 const r=combine(null,null,null,0,120*m,30*m);
 assert.equal(r.actualExport,0);assert.equal(r.actualImport,null);
 assert.equal(r.totalImport,null);assert.equal(r.totalExport,null);
 assert.equal(r.hours[0].net,null);assert.deepEqual(r.forecastExportCurve,[]);
});
test('known deposit remains visible without valid future plan',()=>{
 const r=combine(null,[{start:0,end:30*m,amount:2}],null,0,120*m,30*m);
 assert.deepEqual(r.actualExportCurve,[[0,0],[30*m,2]]);
 assert.deepEqual(r.forecastExportCurve,[]);assert.deepEqual(r.actualCurve,[]);
});

test('unknown cost sensor after restart keeps the verified purchase prefix',()=>{
 const p=partialLedger([row(-5*m,100,1),row(0,101,2),row(5*m,103,4)],[hist(9*m,'unknown',null)],0,20*m);
 assert.equal(p.until,10*m);assert.match(p.error,/history/);
 assert.equal(p.segments.reduce((s,r)=>s+r.amount,0),3);
 assert.equal(partialLedger([row(0,2)],[],0,10*m).until,0);
 assert.equal(partialLedger([row(-5*m,100,1)],[hist(-m,1)],0,m).until,m);
});
test('missing purchase tail is priced from import energy and the G12 price history',()=>{
 const partial={segments:[{start:0,end:10*m,amount:3}],until:10*m};
 const importEnergy=[{start:0,end:10*m,amount:5},{start:10*m,end:30*m,amount:2}];
 const prices=stepPrices([{lu:0,s:'1.25'},{lu:20*60,s:'0.5'}],30*m);
 const r=patchCost(partial,importEnergy,prices,30*m);
 assert.equal(r.estimated,true);assert.equal(r.from,10*m);
 assert.ok(Math.abs(r.segments.reduce((s,x)=>s+x.amount,0)-(3+1*1.25+1*.5))<1e-10);
});
test('stalled import needs no price; impossible estimate falls back to labelled zero',()=>{
 const partial={segments:[{start:0,end:10*m,amount:3}],until:10*m};
 assert.equal(patchCost(partial,[{start:0,end:30*m,amount:0}],[],30*m).estimated,true);
 for(const [imp,prices] of [[null,[]],[[{start:0,end:30*m,amount:3}],stepPrices([{lu:15*60,s:'1'}],30*m)]]){
  const r=patchCost(partial,imp,prices,30*m);
  assert.equal(r.estimated,false);assert.equal(r.from,10*m);assert.equal(r.segments.reduce((s,x)=>s+x.amount,0),3);assert.equal(r.segments.at(-1).end,30*m);
 }
 assert.deepEqual(patchCost({segments:[],until:30*m},null,[],30*m),{segments:[],from:null,estimated:false});
});
test('unavailable or empty price states are not read as zero price',()=>{
 assert.deepEqual(stepPrices([{lu:0,s:'1'},{lu:60,s:'unavailable'},{lu:120,s:''},{lu:180,s:'2'}],4*m).map(r=>r.price),[1,2]);
});

test('month and year bounds follow Warsaw civil calendar, including DST months',async()=>{
 const {periodBounds}=await import('./energy-compass-cost-model.js');
 assert.deepEqual(periodBounds(Date.parse('2026-09-25T08:00Z'),'Europe/Warsaw','month'),{start:Date.parse('2026-08-31T22:00Z'),end:Date.parse('2026-09-30T22:00Z')});
 assert.deepEqual(periodBounds(Date.parse('2026-10-31T23:30Z'),'Europe/Warsaw','month'),{start:Date.parse('2026-10-31T23:00Z'),end:Date.parse('2026-11-30T23:00Z')});
 assert.deepEqual(periodBounds(Date.parse('2026-09-25T08:00Z'),'Europe/Warsaw','year'),{start:Date.parse('2025-12-31T23:00Z'),end:Date.parse('2026-12-31T23:00Z')});
});
test('period totals add live today, keep missing statistics null and report deposit start',async()=>{
 const {periodRows}=await import('./energy-compass-cost-model.js');
 const d=86_400_000;
 const r=periodRows({cost:[{start:0,change:2},{start:d,change:3}],importKwh:[{start:0,change:4},{start:d,change:5}],deposit:[{start:d,change:1}],exportKwh:[{start:0,change:0},{start:d,change:2}]},
  {cost:1,deposit:.5,importKwh:1,exportKwh:1},2*d);
 assert.equal(r.rows.length,3);assert.equal(r.purchase,6);assert.equal(r.deposit,1.5);assert.equal(r.balance,4.5);
 assert.equal(r.rows[0].deposit,null);assert.equal(r.depositFrom,d);assert.deepEqual(r.missingDeposit.map(x=>x.start),[0]);
 assert.equal(r.rows[2].today,true);assert.deepEqual(r.rows[2].todayMissing,[]);
});
test('year bucket keeps month-so-far when today is unavailable and flags it',async()=>{
 const {periodRows}=await import('./energy-compass-cost-model.js');
 const r=periodRows({cost:[{start:0,change:10}],deposit:[{start:0,change:4}]},{cost:2,deposit:null,importKwh:null,exportKwh:null},0);
 assert.equal(r.rows.length,1);assert.equal(r.rows[0].cost,12);assert.equal(r.rows[0].deposit,4);
 assert.deepEqual(r.rows[0].todayMissing,['deposit','importKwh','exportKwh']);
 assert.deepEqual(r.missingCost,[]);
});

test('period offsets move whole calendar days, months and years, across DST and year ends',async()=>{
 const {periodBounds}=await import('./energy-compass-cost-model.js');
 const now=Date.parse('2026-09-25T08:00Z');
 assert.deepEqual(periodBounds(now,'Europe/Warsaw','day',-1),{start:Date.parse('2026-09-23T22:00Z'),end:Date.parse('2026-09-24T22:00Z')});
 assert.equal((b=>b.end-b.start)(periodBounds(Date.parse('2026-10-26T08:00Z'),'Europe/Warsaw','day',-1))/3600_000,25);
 assert.deepEqual(periodBounds(now,'Europe/Warsaw','month',-9),{start:Date.parse('2025-11-30T23:00Z'),end:Date.parse('2025-12-31T23:00Z')});
 assert.deepEqual(periodBounds(now,'Europe/Warsaw','year',-1),{start:Date.parse('2024-12-31T23:00Z'),end:Date.parse('2025-12-31T23:00Z')});
 assert.deepEqual(periodBounds(now,'Europe/Warsaw','month',0),periodBounds(now,'Europe/Warsaw','month'));
});
test('unit prices use only energy that has a money value',async()=>{
 const {periodRows,perKwh}=await import('./energy-compass-cost-model.js');
 const r=periodRows({cost:[{start:0,change:6},{start:1,change:null}],importKwh:[{start:0,change:10},{start:1,change:5}],
  deposit:[{start:1,change:2}],exportKwh:[{start:0,change:8},{start:1,change:4}]},null);
 assert.equal(r.rows.length,2);assert.equal(r.purchasePerKwh,.6);assert.equal(r.depositPerKwh,.5);assert.equal(r.balancePerKwh,.4);
 assert.equal(perKwh(1,0),null);assert.equal(perKwh(null,5),null);
});
test('aggregated bucket unit price uses its own valued export, not the whole month export',async()=>{
 const {periodRows}=await import('./energy-compass-cost-model.js');
 const month=periodRows({exportKwh:[{start:0,change:200}],cost:[{start:0,change:10}],importKwh:[{start:0,change:20}]},{cost:0,deposit:1,importKwh:0,exportKwh:2},1);
 assert.equal(month.valuedExportKwh,2);assert.equal(month.depositPerKwh,.5);
 const year=periodRows({cost:[{start:-1,change:5}],importKwh:[{start:-1,change:5}],exportKwh:[{start:-1,change:50}]},
  {cost:month.purchase,deposit:month.deposit,importKwh:month.importKwh,exportKwh:month.exportKwh,costedImportKwh:month.costedImportKwh,valuedExportKwh:month.valuedExportKwh},0);
 assert.equal(year.depositPerKwh,.5);assert.equal(year.purchasePerKwh,15/25);
});
test('backfill and live counter changes add per bucket without turning missing into zero',async()=>{
 const {mergeChanges}=await import('./energy-compass-cost-model.js');
 assert.deepEqual(mergeChanges([{start:0,change:2},{start:1,change:3}],[{start:1,change:.5},{start:2,change:1}]).map(r=>r.change),[2,3.5,1]);
 assert.deepEqual(mergeChanges([{start:0,change:null}],[]).map(r=>r.change),[null]);
 assert.deepEqual(mergeChanges([{start:0,change:null}],[{start:0,change:1}]).map(r=>r.change),[1]);
});
test('balance per kWh needs at least 1 kWh imported',async()=>{
 const {balancePerKwh}=await import('./energy-compass-cost-model.js');
 assert.equal(balancePerKwh(-22.4,.2),null);assert.equal(balancePerKwh(-3,2),-1.5);
});
