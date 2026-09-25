function (chart) {
  // ApexCharts Card 2.2.3 keeps the latest HA state on its shadow-root host.
  const hass = chart.el.getRootNode().host?._hass;
  for (const id of chart.__energyCompassAnnotationIds || []) chart.removeAnnotation(id);
  chart.__energyCompassAnnotationIds = [];
  const plan = hass?.states['__PLAN__'];
  const valid = hass?.states['__VALID__'];
  const now = Date.now();
  if (!plan || ['unknown', 'unavailable'].includes(plan.state)) return;
  if (plan.attributes.refreshing !== true &&
      (valid?.state !== 'on' || !(Date.parse(plan.attributes.valid_until) > now))) return;

  const min = Math.max(now, chart.w.globals.minX);
  const max = chart.w.globals.maxX;
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return;
  const stateColors = {
    CHARGE_PV: '#fdd835',
    CHARGE_GRID: '#ef5350',
    DISCHARGE_GRID: '#ab47bc',
    SELF_CONSUME: '#66bb6a',
  };
  const intervals = (plan.attributes.intervals || []).map(row => ({
    start: Math.max(min, Date.parse(row.start)), end: Math.min(max, Date.parse(row.end)),
    state: row.state,
  })).filter(row => Number.isFinite(row.start) && row.end > row.start)
    .sort((a, b) => a.start - b.start);
  const runs = [];
  for (const interval of intervals) {
    const lastRun = runs[runs.length - 1];
    if (lastRun && lastRun.state === interval.state && lastRun.end === interval.start) {
      lastRun.end = interval.end;
    } else runs.push({...interval});
  }

  const add = (annotation) => {
    chart.addXaxisAnnotation(annotation, false);
    chart.__energyCompassAnnotationIds.push(annotation.id);
  };
  runs.forEach((band, index) => add({
    id: 'ec-background-' + index,
    x: band.start, x2: band.end,
    fillColor: stateColors[band.state] || '#78909c', opacity: 0.14,
    borderColor: 'transparent', strokeDashArray: 0,
  }));

  runs.forEach((run, index) => {
    const width = (run.end - run.start) / (max - chart.w.globals.minX) * chart.w.globals.gridWidth;
    const label = width >= 13 ? {
      text: run.state, position: 'top', orientation: 'vertical', offsetY: 8, offsetX: width / 2,
      borderWidth: 0,
      style: {background: 'transparent', color: hass?.themes?.darkMode === false ? '#616161' : '#bdbdbd', fontSize: '9px', fontWeight: 700,
        padding: {left: 1, right: 1, top: 1, bottom: 1}},
    } : {text: ''};
    add({id: 'ec-state-' + index, x: run.start, x2: run.end,
      fillColor: 'transparent', opacity: 0, borderColor: '#616161', strokeDashArray: 3, label});
  });

  // Native SVG titles expose even intervals too narrow for a visible label.
  for (const [index, run] of runs.entries()) {
    const time = stamp => new Date(stamp).toLocaleTimeString('__LOCALE__', {hour: '2-digit', minute: '2-digit'});
    const elements = chart.el.querySelectorAll('.ec-state-' + index);
    for (const element of elements) {
      const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      title.textContent = `${run.state} · ${time(run.start)}–${time(run.end)}`;
      element.appendChild(title);
    }
  }
}
