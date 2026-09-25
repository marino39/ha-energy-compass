// Monetary totals use recorder sums. Raw history supplies only the unrecorded tail.
const MINUTE = 60_000;
const EPS = 1e-7;
const finite = value => typeof value === 'number' && Number.isFinite(value);
const total = rows => rows.reduce((sum, row) => sum + row.amount, 0);
const duration = row => row.end - row.start;

export function planUsable(plan, validity, now) {
  if (!plan || ['unknown', 'unavailable'].includes(plan.state) || validity !== 'on') return false;
  const deadline = Date.parse(plan.attributes?.valid_until);
  const grace = plan.attributes?.refreshing === true ? 120_000 : 0;
  return Number.isFinite(deadline) && now < deadline + grace;
}

export function parseTime(value) {
  if (finite(value)) return value;
  if (typeof value !== 'string') return NaN;
  const compact = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})Z$/.exec(value);
  return Date.parse(compact ? `${compact[1]}-${compact[2]}-${compact[3]}T${compact[4]}:${compact[5]}:00Z` : value);
}

function civil(now, zone) {
  const formatter = new Intl.DateTimeFormat('en-GB', {
    timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  });
  return t => Object.fromEntries(formatter.formatToParts(t).map(p => [p.type, p.value]));
}

// Instant of local midnight for a civil date given as a UTC-midnight timestamp.
function zoneMidnight(wall, parts) {
  let instant = wall;
  for (let i = 0; i < 4; i++) {
    const q = parts(instant);
    const displayed = Date.UTC(+q.year, +q.month - 1, +q.day, +q.hour, +q.minute, +q.second);
    instant += wall - displayed;
  }
  return instant;
}

export function dayBounds(now, zone = 'Europe/Warsaw') {
  const parts = civil(now, zone), p = parts(now);
  const day = Date.UTC(+p.year, +p.month - 1, +p.day);
  return {start: zoneMidnight(day, parts), end: zoneMidnight(day + 86_400_000, parts)};
}

// Calendar day/month/year containing `now`, shifted by `offset` periods (negative = past).
export function periodBounds(now, zone = 'Europe/Warsaw', kind = 'month', offset = 0) {
  const parts = civil(now, zone), p = parts(now);
  const y = +p.year, m = +p.month - 1, d = +p.day;
  const [from, to] = kind === 'day' ? [Date.UTC(y, m, d + offset), Date.UTC(y, m, d + offset + 1)]
    : kind === 'year' ? [Date.UTC(y + offset, 0, 1), Date.UTC(y + offset + 1, 0, 1)]
    : [Date.UTC(y, m + offset, 1), Date.UTC(y, m + offset + 1, 1)];
  return {start: zoneMidnight(from, parts), end: zoneMidnight(to, parts)};
}

// Statistics covering disjoint time (historical backfill + live counter): add changes per bucket.
export function mergeChanges(...series) {
  const byStart = new Map();
  for (const rows of series) for (const r of rows || []) {
    const prior = byStart.get(r.start);
    const change = finite(r.change) ? (finite(prior?.change) ? prior.change : 0) + r.change : prior?.change ?? null;
    byStart.set(r.start, {...prior, ...r, change});
  }
  return [...byStart.values()].sort((x, y) => x.start - y.start);
}

export function perKwh(amount, kwh, minimum = .01) {
  return finite(amount) && finite(kwh) && kwh >= minimum ? amount / kwh : null;
}
// Balance per imported kWh is meaningless for near-zero import (e.g. -112 zł/kWh at 0.2 kWh).
export const balancePerKwh = (balance, importKwh) => perKwh(balance, importKwh, 1);

// Completed buckets come from recorder `change`; today's live values are added to `todayBucket`.
// Past periods pass `today = null`. A missing statistic row stays null (not zero), so the card can flag it.
export function periodRows(stats, today, todayBucket = Infinity) {
  const keys = ['cost', 'deposit', 'importKwh', 'exportKwh'];
  const byStart = new Map();
  for (const key of keys) for (const r of stats[key] || []) {
    if (!finite(r.start)) continue;
    const row = byStart.get(r.start) ?? Object.fromEntries([['start', r.start], ...keys.map(k => [k, null])]);
    row[key] = finite(r.change) ? r.change : null;
    byStart.set(r.start, row);
  }
  if (today) {
    const row = byStart.get(todayBucket) ?? Object.fromEntries([['start', todayBucket], ...keys.map(k => [k, null])]);
    // Missing live values keep earlier days of the bucket and mark today as incomplete.
    row.todayMissing = keys.filter(key => !finite(today[key]));
    for (const key of keys) if (finite(today[key])) row[key] = (row[key] ?? 0) + today[key];
    // Valued-energy totals of an aggregated bucket; its own statistics must not also exist.
    for (const key of ['costedImportKwh', 'valuedExportKwh']) if (finite(today[key])) row[key] = today[key];
    row.today = true;
    byStart.set(todayBucket, row);
  }
  const rows = [...byStart.values()].filter(r => r.start <= todayBucket).sort((a, b) => a.start - b.start);
  const add = key => rows.reduce((s, r) => s + (r[key] ?? 0), 0);
  const purchase = add('cost'), deposit = add('deposit'), importKwh = add('importKwh');
  // Unit prices use only the energy that has a matching money value.
  // An aggregated bucket (current month in the year view) may carry its own valued-energy totals.
  const costedImport = rows.reduce((s, r) => s + (r.costedImportKwh ?? (r.cost === null ? 0 : r.importKwh ?? 0)), 0);
  const valuedExport = rows.reduce((s, r) => s + (r.valuedExportKwh ?? (r.deposit === null ? 0 : r.exportKwh ?? 0)), 0);
  return {rows, purchase, deposit, balance: purchase - deposit, importKwh, exportKwh: add('exportKwh'),
    costedImportKwh: costedImport, valuedExportKwh: valuedExport,
    purchasePerKwh: perKwh(purchase, costedImport), depositPerKwh: perKwh(deposit, valuedExport),
    balancePerKwh: balancePerKwh(purchase - deposit, costedImport),
    missingCost: rows.filter(r => r.cost === null), missingDeposit: rows.filter(r => r.deposit === null),
    depositFrom: rows.find(r => r.deposit !== null)?.start ?? null};
}

// Segments are appended as they are validated, so a caller may keep the valid prefix after a failure.
export function ledger(statistics, history, start, end, segments = []) {
  const stats = [...statistics].filter(r => r.end <= end).sort((a, b) => a.end - b.end);
  const baseline = stats.find(r => r.end === start);
  if (!baseline) throw new Error('baseline: missing midnight statistic');
  const validStat = r => finite(r.sum) && finite(r.state) && finite(r.start) && finite(r.end);
  if (!validStat(baseline)) throw new Error('statistic: invalid baseline');
  let prev = baseline;
  for (const r of stats.filter(r => r.end > start)) {
    if (!validStat(r) || r.sum < prev.sum - EPS) throw new Error('statistic: invalid monetary/energy sum');
    if (r.start !== prev.end || r.end - r.start !== 5 * MINUTE) throw new Error('gap: missing statistics');
    segments.push({start: prev.end, end: r.end, amount: Math.max(0, r.sum - prev.sum)});
    prev = r;
  }
  if (prev.end === end) return segments;
  const rows = history.map(r => ({
    time: finite(r.lu) ? r.lu * 1000 : parseTime(r.last_updated),
    value: r.s ?? r.state, attrs: r.a ?? r.attributes ?? {},
  })).filter(r => r.time <= end).sort((a, b) => a.time - b.time);
  const numeric = raw => typeof raw === 'number' ? finite(raw) : typeof raw === 'string' && raw.trim() !== '' && Number.isFinite(Number(raw));
  const anchor = rows.filter(r => r.time <= prev.end).at(-1);
  if (!anchor || !numeric(anchor.value)) throw new Error('history: missing tail baseline');
  if (Math.abs(Number(anchor.value) - prev.state) > EPS) throw new Error('anchor: statistics/history mismatch');
  let value = prev.state, time = prev.end, reset = anchor.attrs.last_reset;
  for (const r of rows.filter(r => r.time > prev.end)) {
    if (!numeric(r.value) || Number(r.value) < 0) throw new Error('history: unavailable counter');
    const next = Number(r.value);
    const changedReset = r.attrs.last_reset != null && reset !== r.attrs.last_reset;
    if (next < value - EPS && !changedReset) throw new Error('history: unexplained counter decrease');
    const delta = changedReset ? next : Math.max(0, next - value);
    if (r.time <= time) throw new Error('history: duplicate time');
    segments.push({start: time, end: r.time, amount: delta});
    value = next; time = r.time; reset = r.attrs.last_reset;
  }
  if (time < end) segments.push({start: time, end, amount: 0});
  return segments;
}

export function partialLedger(statistics, history, start, end) {
  const segments = [];
  try {
    ledger(statistics, history, start, end, segments);
    return {segments, until: end, error: null};
  } catch (e) {
    return {segments, until: segments.at(-1)?.end ?? start, error: e.message};
  }
}

export function slice(segments, left, right) {
  return segments.filter(r => r.end > left && r.start < right).map(r => {
    const a = Math.max(left, r.start), b = Math.min(right, r.end);
    return {start: a, end: b, amount: r.amount * (b - a) / duration(r)};
  });
}

// Price history is a step function: each state holds until the next recorded state.
export function stepPrices(history, end) {
  const rows = history.map(r => ({
    time: finite(r.lu) ? r.lu * 1000 : parseTime(r.last_updated ?? r.last_changed),
    value: r.s ?? r.state,
  })).sort((a, b) => a.time - b.time);
  const price = raw => typeof raw === 'number' || (typeof raw === 'string' && raw.trim() !== '') ? Number(raw) : NaN;
  return rows.map((r, i) => ({start: r.time, end: Math.min(rows[i + 1]?.time ?? end, end), price: price(r.value)}))
    .filter(r => r.end > r.start && finite(r.price));
}

// Keep the verified purchase prefix; price the rest from import energy, or assume zero if that is impossible.
export function patchCost(partial, importEnergy, prices, end) {
  if (partial.until >= end) return {segments: partial.segments, from: null, estimated: false};
  let tail;
  try {
    if (!importEnergy) throw new Error('import: missing');
    tail = priceEnergy(slice(importEnergy, partial.until, end), prices);
  } catch (e) {
    return {segments: [...partial.segments, {start: partial.until, end, amount: 0}], from: partial.until, estimated: false};
  }
  return {segments: [...partial.segments, ...tail], from: partial.until, estimated: true};
}

// Normalized prices already contain the zero floor and the 1.23 deposit uplift.
export function priceEnergy(energy, prices) {
  const tariff = prices.map(r => ({start: parseTime(r.start), end: parseTime(r.end), price: r.price})).sort((a, b) => a.start - b.start);
  const result = [];
  for (const segment of energy) {
    if (!finite(segment.amount) || segment.amount < 0 || !(duration(segment) > 0)) throw new Error('price: invalid energy');
    if (segment.amount === 0) { result.push({...segment}); continue; }
    let cursor = segment.start;
    for (const r of tariff) {
      if (r.end <= cursor || r.start >= segment.end) continue;
      if (!finite(r.price) || r.price < 0 || r.start > cursor || r.end <= r.start) throw new Error('price: missing or invalid tariff');
      const right = Math.min(segment.end, r.end);
      result.push({start: cursor, end: right, amount: segment.amount * (right - cursor) / duration(segment) * r.price});
      cursor = right;
      if (cursor === segment.end) break;
    }
    if (cursor < segment.end) throw new Error('price: incomplete tariff coverage');
  }
  return result;
}

export function project(intervals, start, end) {
  let cursor = start;
  const rows = [];
  const intervalsSorted = intervals.map(r => ({...r, start: parseTime(r.start), end: parseTime(r.end)})).sort((a, b) => a.start - b.start);
  let priorEnd = null;
  for (const r of intervalsSorted) {
    if (!finite(r.start) || !finite(r.end) || r.end <= r.start) throw new Error('forecast: invalid timestamp');
    if (r.end <= start || r.start >= end) continue;
    if (r.start > cursor || (priorEnd !== null && r.start < priorEnd)) throw new Error('forecast: gap or overlap');
    for (const field of ['grid_import_kwh', 'grid_export_kwh', 'buy_per_kwh', 'sell_per_kwh']) {
      if (!finite(r[field]) || r[field] < -EPS) throw new Error('forecast: invalid energy or price');
    }
    const left = Math.max(start, r.start), right = Math.min(end, r.end), ratio = (right - left) / duration(r);
    const imp = Math.max(0, r.grid_import_kwh) * ratio, exp = Math.max(0, r.grid_export_kwh) * ratio;
    rows.push({start: left, end: right, importKwh: imp, exportKwh: exp,
      importCost: imp * Math.max(0, r.buy_per_kwh), exportValue: exp * Math.max(0, r.sell_per_kwh)});
    cursor = right; priorEnd = r.end;
  }
  const sum = key => rows.reduce((s, r) => s + r[key], 0);
  const complete = cursor === end;
  return {rows, complete, end: cursor, importCost: complete ? sum('importCost') : null,
    exportValue: complete ? sum('exportValue') : null, importKwh: complete ? sum('importKwh') : null,
    exportKwh: complete ? sum('exportKwh') : null, partialImportCost: sum('importCost'), partialExportValue: sum('exportValue')};
}

export function combine(actualCost, actualExport, forecast, start, end, cutoff) {
  // Missing measured deposit is assumed to be zero for this dashboard.
  if (actualExport === null) actualExport = cutoff > start ? [{start, end: cutoff, amount: 0}] : [];
  const actualImport = actualCost === null ? null : total(actualCost);
  const exportValue = actualExport === null ? null : total(actualExport);
  const add = (a, b) => a === null || b === null ? null : a + b;
  const sliceTotal = (rows, left, right) => rows === null ? null : rows.reduce((s, r) => s + r.amount * Math.max(0, Math.min(right, r.end) - Math.max(left, r.start)) / duration(r), 0);
  const projectedCost = forecast ? forecast.rows.map(r => ({...r, amount: r.importCost})) : null;
  const projectedExport = forecast ? forecast.rows.map(r => ({...r, amount: r.exportValue})) : null;
  const hours = [];
  for (let left = start; left < end; left += 60 * MINUTE) {
    const right = Math.min(left + 60 * MINUTE, end);
    const futureValid = right <= cutoff || (forecast && forecast.end >= right);
    const cost = futureValid ? add(sliceTotal(left >= cutoff ? [] : actualCost, left, right), sliceTotal(projectedCost ?? (right <= cutoff ? [] : null), left, right)) : null;
    const exp = futureValid ? add(sliceTotal(left >= cutoff ? [] : actualExport, left, right), sliceTotal(projectedExport ?? (right <= cutoff ? [] : null), left, right)) : null;
    hours.push({start: left, end: right, importCost: cost, exportValue: exp,
      net: cost === null || exp === null ? null : cost - exp,
      kind: right <= cutoff ? 'actual' : left >= cutoff ? 'forecast' : 'mixed'});
  }
  function curve(rows, initial, from) {
    if (rows === null || initial === null) return [];
    let value = initial;
    return [[from, value], ...rows.map(r => [r.end, value += r.amount])];
  }
  return {actualImport, actualExport: exportValue,
    totalImport: add(actualImport, forecast?.importCost ?? null),
    totalExport: add(exportValue, forecast?.exportValue ?? null), hours,
    actualCurve: curve(actualCost, 0, start),
    forecastCurve: curve(projectedCost, actualImport, cutoff),
    actualExportCurve: curve(actualExport, 0, start),
    forecastExportCurve: curve(projectedExport, exportValue, cutoff)};
}
