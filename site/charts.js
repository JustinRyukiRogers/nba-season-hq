// Small charts for Season HQ, drawn as HTML/SVG strings (the page renders by innerHTML).
// One look everywhere: thin bars (<= 14px, 4px rounded data end, square at the baseline), hairline axes,
// values in text ink beside the marks, never in the series color. Colors by job: --c-1 is the one series
// (or "you" against gray "everyone else", --c-dim); diverging values run --c-1 (up) and --c-neg (down)
// from a gray zero line. Every mark carries data-tip (shown by the hover layer below); every chart sits
// next to a list or table with the same numbers, so the tooltip never gates a value.
// Labels come from ESPN (player and team names): always escaped before they go into markup.

const Charts = (() => {
  const e = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // Horizontal bars, one row each: label | bar | value. rows: [{label, value, text?, tip?, cls?: 'mine' | 'dim', pid?}].
  // diverging: bars grow left (negative) or right (positive) of a zero line, colored by sign.
  function hbars(rows, { diverging = false, fmt = v => Math.round(v), max = null, labelWidth = 150 } = {}) {
    if (!rows.length) return '';
    const top = max ?? Math.max(1e-9, ...rows.map(r => Math.abs(r.value)));
    const body = rows.map(r => {
      const w = Math.min(100, 100 * Math.abs(r.value) / top);
      const neg = r.value < 0;
      const kind = diverging ? (neg ? 'neg' : 'pos') : (r.cls === 'dim' ? 'dim' : 'pos');
      const bar = diverging
        ? `<div class="hb-half l">${neg ? `<i class="hb-bar ${kind} rev" style="width:${w}%"></i>` : ''}</div><div class="hb-half r">${neg ? '' : `<i class="hb-bar ${kind}" style="width:${w}%"></i>`}</div>`
        : `<div class="hb-half r full"><i class="hb-bar ${kind}" style="width:${w}%"></i></div>`;
      return `<div class="hb-row${r.cls === 'mine' ? ' mine' : ''}${r.pid ? ' click' : ''}" ${r.pid ? `data-pid="${e(r.pid)}"` : ''} data-tip="${e(r.tip || `${r.label}: ${r.text ?? fmt(r.value)}`)}" tabindex="0">
        <span class="hb-label">${e(r.label)}</span><div class="hb-track${diverging ? ' div' : ''}">${bar}</div><span class="hb-val">${e(r.text ?? fmt(r.value))}</span></div>`;
    }).join('');
    return `<div class="hbars" style="--hb-label:${labelWidth}px">${body}</div>`;
  }

  // Columns over days (or any short ordered set): [{label, sub?, value, tip?, cls?: 'dim' | 'today'}]. Values on the caps.
  function columns(cols, { height = 120, fmt = v => Math.round(v) } = {}) {
    if (!cols.length) return '';
    const top = Math.max(1e-9, ...cols.map(c => c.value));
    const body = cols.map(c => {
      const h = Math.max(c.value > 0 ? 2 : 0, Math.round(height * c.value / top));
      return `<div class="col${c.cls ? ' ' + c.cls : ''}" data-tip="${e(c.tip || `${c.label}: ${fmt(c.value)}`)}" tabindex="0">
        <span class="col-val">${e(fmt(c.value))}</span><i class="col-bar" style="height:${h}px"></i>
        <span class="col-label">${e(c.label)}</span>${c.sub ? `<span class="col-sub">${e(c.sub)}</span>` : ''}</div>`;
    }).join('');
    return `<div class="cols" style="--col-h:${height}px">${body}</div>`;
  }

  // A race between two sides: each row a bar of points scored (solid) and points still projected (lighter),
  // on one scale. rows: [{label, done, rest, cls?}]
  function race(rows, { fmt = v => Math.round(v) } = {}) {
    const top = Math.max(1e-9, ...rows.map(r => r.done + r.rest));
    const body = rows.map(r => {
      const a = 100 * r.done / top, b = 100 * r.rest / top;
      return `<div class="race-row${r.cls ? ' ' + r.cls : ''}" data-tip="${e(`${r.label}: ${fmt(r.done)} scored, ${fmt(r.rest)} more projected, ${fmt(r.done + r.rest)} in all`)}" tabindex="0">
        <span class="hb-label">${e(r.label)}</span>
        <div class="race-track"><i class="race-done" style="width:${a}%"></i><i class="race-rest" style="width:${b}%"></i></div>
        <span class="hb-val"><b>${e(fmt(r.done + r.rest))}</b> <span class="muted">(${e(fmt(r.done))} + ${e(fmt(r.rest))})</span></span></div>`;
    }).join('');
    return `<div class="race">${body}</div>
      <div class="legend"><span><i class="key race-done"></i>Scored</span><span><i class="key race-rest"></i>Projected rest of week</span></div>`;
  }

  // The hover layer: one tooltip for every [data-tip], on pointer and keyboard focus. Text only (textContent).
  function install() {
    if (document.getElementById('tip')) return;
    const tip = document.createElement('div');
    tip.id = 'tip';
    tip.setAttribute('role', 'tooltip');
    tip.hidden = true;
    document.body.appendChild(tip);
    const show = (el, x, y) => {
      tip.textContent = el.getAttribute('data-tip');
      tip.hidden = false;
      const r = tip.getBoundingClientRect();
      const left = Math.min(window.innerWidth - r.width - 8, Math.max(8, x + 12));
      const top = y + r.height + 20 > window.innerHeight ? y - r.height - 12 : y + 16;
      tip.style.left = left + 'px';
      tip.style.top = top + 'px';
    };
    document.addEventListener('pointermove', ev => {
      const el = ev.target.closest && ev.target.closest('[data-tip]');
      if (!el) { tip.hidden = true; return; }
      show(el, ev.clientX, ev.clientY);
    });
    document.addEventListener('focusin', ev => {
      const el = ev.target.closest && ev.target.closest('[data-tip]');
      if (!el) { tip.hidden = true; return; }
      const r = el.getBoundingClientRect();
      show(el, r.left + r.width / 2, r.bottom - 8);
    });
    document.addEventListener('focusout', () => { tip.hidden = true; });
    window.addEventListener('scroll', () => { tip.hidden = true; }, { passive: true });
  }

  return { hbars, columns, race, install };
})();
