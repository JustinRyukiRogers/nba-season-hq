'use strict';
// Season HQ: renders the refresh's data (site/data.json, encrypted as data.enc when hosted) in eight
// tabs. Plain DOM, no framework. Everything shown comes from ESPN via seasonkit/build.py.

const S = {
  data: null, key: null, tab: 'today', wtab: 'adds', rtab: 'values', pos: '', instant: false, q: '', who: 'all',
  prev: null, changes: [],
  st: { range: 'week', who: 'mine', per: 'game', view: 'min', onTeam: true, sort: 'fp', dir: -1, from: '', to: '' },
};
const TZ = 'America/New_York';
const IS_LOCAL = ['localhost', '127.0.0.1'].includes(location.hostname);
const SLOT_ORDER = ['PG', 'SG', 'SF', 'PF', 'C', 'G', 'F', 'UT', 'BE', 'IR'];
const STALE_MIN = 90;
const $ = (sel, el = document) => el.querySelector(sel);

// ---------- storage (best effort: private windows can refuse it) ----------
function getItem(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function setItem(k, v) { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) { /* ignore */ } }

// ---------- to-dos hidden for the day (this browser only; back the next day if still there) ----------
function actKey(a) {
  return [a.kind, a.player, a.drop, a.other, a.give, a.get, a.start, a.sit].map(x => [].concat(x ?? '').join('+')).join('|');
}
function hiddenActs() {
  try { return JSON.parse(getItem('hq.hidden') || '{}'); } catch (e) { return {}; }
}
function setHidden(key, on) {
  const today = S.data.calendar.today, h = hiddenActs();
  for (const k of Object.keys(h)) if (h[k] !== today) delete h[k];
  if (on) h[key] = today; else if (key) delete h[key]; else for (const k of Object.keys(h)) delete h[k];
  setItem('hq.hidden', Object.keys(h).length ? JSON.stringify(h) : null);
}
function visibleActs() {
  const today = S.data.calendar.today, h = hiddenActs();
  return S.data.actions.filter(a => h[actKey(a)] !== today);
}
function getJSON(k) { try { return JSON.parse(getItem(k) || 'null'); } catch (e) { return null; } }

// ---------- formatting ----------
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function n0(x) { return x == null ? '–' : Math.round(x).toLocaleString('en-US'); }
function n1(x) { return x == null ? '–' : (Math.round(x * 10) / 10).toFixed(1); }
function signed(x) { return x == null ? '–' : (x > 0.5 ? '+' : x < -0.5 ? '−' : '') + n0(Math.abs(x)); }
function signed1(x) { return x == null ? '–' : (x > 0.05 ? '+' : x < -0.05 ? '−' : '') + Math.abs(x).toFixed(1); }
function isoDate(iso) { const [y, m, d] = iso.split('-').map(Number); return new Date(Date.UTC(y, m - 1, d, 12)); }
function dayLabel(iso) { return isoDate(iso).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' }); }
function dayShort(iso) { return isoDate(iso).toLocaleDateString('en-US', { weekday: 'short', month: 'numeric', day: 'numeric', timeZone: 'UTC' }); }
function timeLabel(ts) { return new Date(ts * 1000).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: TZ }); }
function whenLabel(ts) { return new Date(ts * 1000).toLocaleString('en-US', { weekday: 'short', hour: 'numeric', minute: '2-digit', timeZone: TZ }); }
function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 90) return 'just now';
  if (s < 3600) return Math.round(s / 60) + ' min ago';
  if (s < 2 * 86400) return Math.round(s / 3600) + ' h ago';
  return Math.round(s / 86400) + ' days ago';
}
function plural(n, w) { return n + ' ' + w + (n === 1 ? '' : 's'); }

// ---------- data helpers ----------
function P(id) { return S.data.players[String(id)]; }
function team(tid) { return S.data.league.teams[String(tid)]; }
function me() { return S.data.league.me; }
function abbr(tid) { return tid ? ((team(tid) || {}).abbrev || '?') : 'FA'; }
function myRoster() { return team(me()).roster; }
function bySlot(a, b) { return SLOT_ORDER.indexOf(a.slot) - SLOT_ORDER.indexOf(b.slot); }

function statusChip(p) {
  const inj = p.inj || {};
  if (inj.fantasy === 'OFS') return '<span class="chip out" title="Out for the season">OFS</span>';
  if (p.status === 'OUT') return '<span class="chip out">OUT</span>';
  if (p.status === 'SUSPENSION') return '<span class="chip out">SUSP</span>';
  if (p.status === 'DAY_TO_DAY') return '<span class="chip dtd">DTD</span>';
  if (inj.status === 'Out') return '<span class="chip out" title="Out on the injury report; ESPN fantasy has not tagged him yet, so he is not IR-eligible">OUT*</span>';
  return '';
}
function faChip(p) {
  if (p.owner || S.data.waivers.all_on_waivers) return '';   // the banner says it once instead
  if (p.fa === 'WAIVERS') {
    const t = p.waiver_until ? whenLabel(p.waiver_until) : '';
    return `<span class="chip wv" title="On waivers: claims process ${esc(t)}">W ${esc(t)}</span>`;
  }
  return '';
}
function meta(p) { return esc(p.team) + ' · ' + esc(p.pos.join('/') || '–') + depthLabel(p); }
// his place on ESPN's depth chart (p.depth), counting past teammates who are Out
function depthLabel(p) {
  const dp = p.depth;
  if (!dp) return '';
  const ord = n => n + (n % 100 >= 11 && n % 100 <= 13 ? 'th' : ['th', 'st', 'nd', 'rd'][n % 10] || 'th');
  const what = dp.now === 1 ? 'starting ' + dp.pos : ord(dp.now) + ' string ' + dp.pos;
  const why = dp.out.length ? ` (${dp.out.join(', ')} out)` : '';
  return ` · <span class="depth d${Math.min(dp.now, 4)}" title="ESPN depth chart: listed ${ord(dp.rank)} at ${esc(dp.pos)}">${esc(what + why)}</span>`;
}
function nextGame(p) {
  const g = (p.next || [])[0];
  if (!g) return 'no games this week';
  const [d, opp, home, tip] = g;
  return `${dayShort(d)} ${home ? 'vs' : '@'} ${opp} ${timeLabel(tip)}`;
}
function ownerLabel(p) {
  if (!p.owner) return 'free agent';
  if (p.owner === me()) return 'yours';
  return abbr(p.owner);
}
function isNew(p) {
  const since = S.prev && S.prev.at;
  return !!(since && (p.news || []).some(n => Date.parse(n.at) / 1000 > since));
}

function row(p, nums, opts = {}) {
  if (!p) return '';
  const cls = 'row' + (p.owner === me() && !opts.plain ? ' mine' : '') + (opts.cls ? ' ' + opts.cls : '');
  const badge = (opts.badge || '') + (isNew(p) ? ' <span class="chip new" title="News since you last looked">NEW</span>' : '');
  const left = `<div class="who"><div class="nm">${opts.prefix || ''}${esc(p.name)} ${statusChip(p)} ${faChip(p)}${badge}</div>`
    + `<div class="meta">${opts.meta || meta(p)}</div></div>`;
  const right = `<div class="nums">${nums.map(([label, value, c, optional]) =>
    `<div class="num${optional ? ' opt' : ''}"><b class="${c || ''}">${value}</b><span>${label}</span></div>`).join('')}</div>`;
  return `<div class="${cls}" data-pid="${p.id}">${left}${right}${opts.note ? `<div class="note">${opts.note}</div>` : ''}</div>`;
}

function injuryNote(p, long) {
  const i = p.inj;
  const bits = [];
  if (i) {
    const what = [i.side, i.type, i.detail].filter(Boolean).join(' ');
    if (what) bits.push(esc(what));
  }
  if (p.ros.out && p.ros.back) bits.push(p.ros.season_over ? 'out for the season' : 'back ~' + dayLabel(p.ros.back));
  if (i && i.short) bits.push(esc(long ? i.short : clip(i.short, 140)));
  return bits.join(' · ');
}
function clip(s, n) { return s.length > n ? s.slice(0, n - 1) + '…' : s; }

// ---------- rendering ----------
function render() {
  const d = S.data;
  $('#title').textContent = d.league.name + ' · ' + abbr(me());
  freshness();
  const urgent = visibleActs().some(a => urgencyOf(a) === 'now');
  for (const b of document.querySelectorAll('#tabs button')) {
    b.classList.toggle('on', b.dataset.tab === S.tab);
    b.setAttribute('aria-selected', b.dataset.tab === S.tab);
    if (b.dataset.tab === 'today') b.innerHTML = 'Today' + (urgent ? '<span class="dot" title="Something needs doing"></span>' : '');
  }
  const views = { today: viewToday, week: viewWeek, roster: viewRoster, waivers: viewWaivers, trades: viewTrades, stats: viewStats, injuries: viewInjuries, league: viewLeague };
  $('#view').innerHTML = views[S.tab]();
}

function freshness() {
  const d = S.data;
  const el = $('#fresh');
  const mins = (Date.now() / 1000 - d.generated) / 60;
  el.textContent = 'Updated ' + timeLabel(d.generated) + ' · ' + ago(d.generated);
  el.classList.toggle('stale', mins > STALE_MIN);
  el.title = mins > STALE_MIN ? 'Over ' + STALE_MIN + ' minutes since the last refresh. GitHub may be skipping scheduled runs, or ESPN failed (see the Actions tab of the repo).' : '';
}

// ----- Today -----
function viewToday() {
  const d = S.data, cal = d.calendar, out = [];
  if (d.problems && d.problems.length) out.push(`<div class="banner warn small">${d.problems.map(esc).join('<br>')}</div>`);
  if (cal.today < cal.start) {
    out.push(`<div class="banner">The season starts <b>${dayLabel(cal.start)}</b>. Lineup checks begin that day.</div>`);
  }
  if (S.changes.length) {
    out.push(`<h2>Since you last looked <span class="sub">${S.prev ? 'data from ' + esc(whenLabel(S.prev.at)) : ''}</span></h2>`
      + `<div class="card">${S.changes.map(c => `<div>${c}</div>`).join('')}</div>`);
  }
  out.push('<h2>To do</h2>');
  const acts = visibleActs(), hidden = d.actions.length - acts.length;
  if (!acts.length) out.push(`<div class="card muted">${hidden ? 'Nothing else needs doing today.' : 'Nothing needs doing right now.'}</div>`);
  for (const a of acts) out.push(actionCard(a));
  if (hidden) out.push(`<p class="small muted">${plural(hidden, 'to-do')} hidden until tomorrow. <a href="#" data-unhide="1">Show</a></p>`);
  if (acts.length) {
    out.push(`<p class="small muted">${URGENCY.now}: it costs points or blocks you today. ${URGENCY.soon}: it has a deadline, or someone may get there first.
      ${URGENCY.optional}: a small gain, or no one is racing you.</p>`);
  }
  out.push(matchupCard());
  out.push(lineupCard());
  return out.join('');
}

const PILL_LABELS = {
  ir_blocked: ['activate', 'drop'], to_ir: ['to IR'], ir_swap: ['to IR'], stream: ['add', 'drop'],
  upgrade: ['add', 'drop'], stash: ['add', 'drop if needed'],
};
const URGENCY = { now: 'Do now', soon: 'Soon', optional: 'Optional', fyi: 'FYI' };
const WHY = { now: 'Why now', soon: 'Why soon', optional: 'Why it can wait', fyi: 'Note' };
function urgencyOf(a) {
  // data from before urgency levels existed carried an importance level instead
  return a.urgency || { critical: 'now', high: 'now', medium: 'soon', low: 'optional', info: 'fyi' }[a.level] || 'fyi';
}
function actionCard(a) {
  const u = urgencyOf(a);
  const [l1, l2] = PILL_LABELS[a.kind] || ['', 'drop'];
  const pills = [];
  if (a.player) pills.push(pill(a.player, l1));
  if (a.other) pills.push(pill(a.other, 'activate'));
  if (a.drop) pills.push(pill(a.drop, l2));
  for (const id of a.start || []) pills.push(pill(id, 'start'));
  for (const id of a.sit || []) pills.push(pill(id, 'bench'));
  for (const id of a.give || []) pills.push(pill(id, 'give'));
  for (const id of a.get || []) pills.push(pill(id, 'get'));
  const chip = `<span class="urg ${u}">${URGENCY[u]}${a.by ? ' · by ' + esc(whenLabel(a.by)) : ''}</span>`;
  const x = `<button class="dismiss" data-dismiss="${esc(actKey(a))}" title="Done or not doing it: hide until tomorrow" aria-label="Hide until tomorrow">×</button>`;
  return `<div class="card act ${u}">${x}<div class="t">${chip}${esc(a.title)}</div>`
    + (a.detail ? `<div class="d">${esc(a.detail)}</div>` : '')
    + (a.steps && a.steps.length ? `<ol class="steps">${a.steps.map(x => `<li>${esc(x)}</li>`).join('')}</ol>` : '')
    + (pills.length ? `<div class="who">${pills.join('')}</div>` : '')
    + (a.why ? `<div class="why"><b>${WHY[u]}:</b> ${esc(a.why)}</div>` : '') + '</div>';
}
function pill(id, label) {
  const p = P(id);
  if (!p) return '';
  return `<span class="pill" data-pid="${p.id}">${label ? `<span class="lbl">${esc(label)}</span>` : ''}${esc(p.name)} ${statusChip(p)}</span>`;
}

function matchupCard() {
  const m = S.data.matchup;
  if (!m || !m.me || !m.them) return '';
  const opp = team(m.opp);
  const total = (m.me.projected + m.them.projected) || 1;
  const days = m.me.by_day.map((r, i) => {
    const t = m.them.by_day[i] || [r[0], 0, 0];
    return `<tr><td>${dayShort(r[0])}</td><td class="r">${n0(r[2])}</td><td class="r">${n0(r[1])}</td><td class="r">${n0(t[2])}</td><td class="r">${n0(t[1])}</td></tr>`;
  }).join('');
  return `<h2>Week ${m.period} <span class="sub">${dayLabel(m.dates[0])} – ${dayLabel(m.dates[1])}${m.playoffs ? ' · playoffs' : ''}</span></h2>
  <div class="card">
    <div class="vs">
      <div class="side"><div class="tm">${esc(abbr(me()))}</div><div class="sc">${n0(m.me.score)}</div><div class="pj">projected ${n0(m.me.projected)}</div></div>
      <div class="mid">vs</div>
      <div class="side"><div class="tm">${esc(opp.abbrev)}</div><div class="sc">${n0(m.them.score)}</div><div class="pj">projected ${n0(m.them.projected)}</div></div>
    </div>
    <div class="bar-split"><i class="a" style="width:${(100 * m.me.projected / total).toFixed(1)}%"></i><i class="b" style="width:${(100 * m.them.projected / total).toFixed(1)}%"></i></div>
    <div class="small muted">${esc(opp.name)}. Expected starts left: ${n0(m.me.starts)} for you, ${n0(m.them.starts)} for them.
      Projected from each side's best daily lineups from here on, with only starters scoring: who plays is simulated, and a bench player
      fills in when a starter sits. Your adds for the week are in the <a href="#" data-goto="week">Week</a> tab.</div>
    ${days ? `<div class="scroll-x"><table class="tbl" style="margin-top:8px"><thead><tr><th>Day</th><th class="r">Your starts</th><th class="r">Points</th><th class="r">Their starts</th><th class="r">Points</th></tr></thead><tbody>${days}</tbody></table></div>` : ''}
  </div>`;
}

function lineupCard() {
  const L = S.data.lineup;
  if (!L || L.state !== 'season') return '';
  const start = new Set(L.start), sit = new Set(L.sit);
  const rows = [...L.rows].sort(bySlot).map(r => {
    const p = P(r.id);
    const game = r.tip ? `${r.home ? 'vs' : '@'} ${r.opp} ${timeLabel(r.tip)}${r.locked ? ' · locked' : ''}` : 'no game';
    const flag = start.has(r.id) ? ' <span class="chip new">START</span>' : sit.has(r.id) ? ' <span class="chip">BENCH</span>' : '';
    return row(p, [['exp pts', r.tip && !r.locked ? n1(r.pts) : '–']],
      { prefix: `<span class="chip slot">${esc(r.slot)}</span> `, meta: esc(game), badge: flag, plain: true });
  }).join('');
  const verdict = L.gain >= 0.5 ? `yours ${n1(L.current)}, best ${n1(L.best)}` : 'yours is already the best';
  return `<h2>Today's lineup <span class="sub">${plural(L.with_games, 'player')} with a game · ${verdict}</span></h2>
    <div class="rows">${rows}</div>
    <p class="small muted">As ESPN had it at the last refresh. Lineups lock at each game's tip-off; check ESPN before you rely on this.</p>`;
}

// ----- Week: when to move, what to add, each day's lineup -----
function gameOn(p, day) {
  const g = (p.next || []).find(x => x[0] === day);
  return g ? `${g[2] ? 'vs' : '@'} ${g[1]} ${timeLabel(g[3])}` : '';
}

function viewWeek() {
  const d = S.data, W = d.plan;
  if (!W) return '<div class="empty">This view fills in after the next refresh.</div>';
  if (!W.days.length) return '<div class="empty">No days left in this matchup week.</div>';
  const out = [];
  const games = W.lineups.reduce((a, x) => a + x.games, 0), starts = W.lineups.reduce((a, x) => a + x.starts, 0);
  const gain = W.points - W.points_now;
  out.push(`<div class="tiles">
    <div class="tile"><b>${n0(W.points)}</b><span>points from your starters over the rest of the week${W.moves.length ? ` with the adds below (${signed(gain)})` : ''}</span></div>
    <div class="tile"><b>${n0(starts)} of ${games}</b><span>expected starts of your players' games left; the rest sit on the bench</span></div>
    <div class="tile"><b>${W.move_days.length}</b><span>${W.move_days.length === 1 ? 'day' : 'days'} left on which an add still counts this week</span></div>
  </div>`);

  out.push('<h2>Adds this week</h2>');
  if (W.moves.length) {
    for (const m of W.moves) {
      const p = P(m.id);
      if (m.kind === 'stash') {
        out.push(`<div class="card act soon">
          <div class="t">${esc(dayLabel(m.day))}${m.by ? ` · before ${esc(timeLabel(m.by))}` : ''}: add ${esc(p.name)} and move him to IR${m.drop ? `, dropping ${esc(P(m.drop).name)}` : ''}</div>
          <div class="d">The IR stash from the Today tab. It takes this day's add, so streaming starts the next day${m.drop ? `, in the spot ${esc(P(m.drop).name)} leaves` : ''}.</div>
          <div class="who">${pill(m.id, 'add, then IR')}${m.drop ? pill(m.drop, 'drop') : ''}</div></div>`);
        continue;
      }
      out.push(`<div class="card act ${m === W.moves[0] && m.by && m.by - Date.now() / 1000 <= 86400 ? 'now' : 'soon'}">
        <div class="t">${esc(dayLabel(m.day))}${m.by ? ` · before ${esc(timeLabel(m.by))}` : ''}: add ${esc(p.name)}, ${m.drop ? 'drop ' + esc(P(m.drop).name) : 'into the open spot'}</div>
        <div class="d">${signed(m.gain)} points in that day's lineup${m.ros_cost >= 1 ? `, less ${n0(m.ros_cost)} of rest-of-season value given up` : ''}${m.spot ? ' · streaming spot' : ''}.</div>
        <div class="who">${pill(m.id, 'add')}${m.drop ? pill(m.drop, 'drop') : ''}</div></div>`);
    }
  } else {
    const s = d.streams && d.streams.list[0];
    out.push(`<div class="card muted">No add is worth making for the rest of this week.</div>`);
  }
  const spot = [...(W.spot || []).map(id => esc(P(id).name)), ...(W.open ? ['an empty roster spot'] : [])].join(', ');
  out.push(`<p class="small muted"><b>Streaming spot${spot ? ': ' + spot : ''}.</b> Your lowest-valued healthy player's roster spot goes to a new
    free agent most days: whoever adds most to that day's lineup. Tested over the whole season with this roster, that beat holding him by
    15 to 47 points a week, even assuming other teams take the best free agents and free agents score 15% under projection.
    One add a day; an add counts from the next day whose first game has not tipped off (ESPN locks rosters at the day's first game), so each add
    above has to be made before that day's first tip. Re-planned on every refresh.
    ${W.preseason_add ? 'ESPN may count the days before the opener as one day for the one-add limit, so an add made before the opener can use up opening day\'s.' : ''}</p>`);

  for (const day of W.lineups) {
    const moves = day.moves.map(m => `<div class="small"><b>Before ${esc(day.first_tip ? timeLabel(day.first_tip) : 'the first game')}:</b> add ${esc(P(m.id).name)}${
      m.kind === 'stash' ? ' and move him to IR' + (m.drop ? `, dropping ${esc(P(m.drop).name)}` : '') : m.drop ? ', drop ' + esc(P(m.drop).name) : ' into the open spot'}</div>`).join('');
    const filled = day.slots.filter(r => r[1]);
    const empty = day.slots.length - filled.length;
    const rows = filled.map(([slot, id, prob, pts, locked]) => {
      const p = P(id);
      return row(p, [['plays', locked ? '–' : Math.round(100 * prob) + '%', '', true], ['exp pts', locked ? 'locked' : n1(pts)]],
        { prefix: `<span class="chip slot">${esc(slot)}</span> `, meta: esc(gameOn(p, day.date) || (locked ? 'playing' : '')), plain: true });
    }).join('');
    const bench = day.bench.map(([id, prob, start]) => {
      const p = P(id);
      return row(p, [['plays', Math.round(100 * prob) + '%', '', true], ['starts', Math.round(100 * start) + '%']],
        { prefix: '<span class="chip slot">BE</span> ', meta: esc(gameOn(p, day.date)) + (prob ? ' · starts only if a starter sits' : ''), plain: true, cls: 'benched' });
    }).join('');
    const idle = day.idle.map(id => esc(P(id).name)).join(', ');
    out.push(`<h2>${esc(dayLabel(day.date))} <span class="sub">${plural(day.games, 'game')} · ${n0(day.points)} expected points${day.date === d.calendar.today ? ' · today' : ''}</span></h2>
      ${moves}<div class="rows">${rows}${bench}</div>
      <p class="small muted">${empty ? `${plural(empty, 'slot')} empty. ` : ''}${idle ? 'No game: ' + idle + '.' : ''}</p>`);
  }
  out.push(`<p class="small muted">Each day's lineup is the best on expected points with the roster you will have that day, adds included.
    <b>Plays</b>: his chance of playing. <b>Starts</b>, for bench players: how often he gets into the lineup when you swap in for a starter who sits.
    Expected points simulate who plays, so they allow for those swaps.</p>`);
  return out.join('');
}

// ----- Trades -----
function viewTrades() {
  const d = S.data, T = d.trades;
  if (!T) return '<div class="empty">This view fills in after the next refresh.</div>';
  const out = [`<p class="lead small">Trades that add to your daily lineups over the rest of the season (playoff weeks count 1.5×), where the other team
    comes out ahead on <b>ESPN's projections</b>, the numbers ESPN's trade screen shows them, counted above a waiver pickup. 1-for-1, 2-for-1 and 1-for-2:
    a trade that opens a roster spot fills it with the best free agent, and one that leaves a player too many drops your lowest-valued one.</p>`];
  out.push(`<div class="banner warn small">These ideas come from where this dashboard and ESPN disagree about a player. The dashboard's numbers start from your
    draft board, which beat ESPN's list in replays of past drafts but is not yet tested in season. Until a few weeks of games are in, read them as the
    board's opinion against ESPN's.</div>`);
  if (!T.list.length) {
    out.push('<div class="card muted">No trade found that helps you and that the other team should accept.</div>');
    return out.join('');
  }
  const weeks = Math.max(1, T.weeks_left);
  for (const t of T.list) {
    const other = team(t.team);
    const winwin = t.their_gain >= 0;
    const pills = [...t.give.map(id => pill(id, 'give')), ...t.get.map(id => pill(id, 'get'))];
    if (t.my_drop) pills.push(pill(t.my_drop, 'then drop'));
    if (t.my_add) pills.push(pill(t.my_add, 'then add'));
    out.push(`<div class="card">
      <div class="t"><b>${esc(other.abbrev)}</b> <span class="muted">${esc(other.name)}</span>${winwin ? ' <span class="chip up" title="They gain on the dashboard\'s numbers too">BOTH GAIN</span>' : ''}</div>
      <div class="who" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">${pills.join('')}</div>
      <div class="tiles">
        <div class="tile"><b class="pos">${signed(t.gain)}</b><span>season points for you, about ${signed(t.gain / weeks)} a week</span></div>
        <div class="tile"><b>${signed(t.their_espn)}</b><span>for them on ESPN's projections</span></div>
        <div class="tile"><b class="${t.their_gain >= 0 ? '' : 'down'}">${signed(t.their_gain)}</b><span>for them on the dashboard's numbers</span></div>
      </div></div>`);
  }
  out.push(`<p class="small muted">Your gain is the change in points from your best daily lineups for the rest of the season, simulated as on the Week tab.
    No one is racing you for a trade, so these are optional; every week you wait is a week of the gain gone${T.deadline ? `. Trade deadline: ${esc(new Date(T.deadline * 1000).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: TZ }))} ET` : ''}.
    One player of yours appears in at most three ideas.</p>`);
  return out.join('');
}

// ----- Stats -----
// Every finished game this season (d.gamelog: [days since opening night, fantasy points, minutes])
// against the projection the dashboard started from: PROJ = projected points a game times games,
// xMIN = projected minutes times games, xFP = minutes played times projected points a minute.
// The shots view (games that also carry [shooting points, what the attempts were worth at his own make
// rates, what the minutes were worth at his own rate for everything else, FGA, 3PA, FTA]) splits FP
// against his own standards: sxFP = attempts at his rates + minutes at his other rate.
function saveStats() {
  const { range, who, per, view, onTeam, from, to } = S.st;
  setItem('hq.stats', JSON.stringify({ range, who, per, view, onTeam, from, to }));
}
function dayIndex(iso) { return Math.round((isoDate(iso) - isoDate(S.data.calendar.start)) / 86400000); }
function dayIso(i) { return new Date(isoDate(S.data.calendar.start).getTime() + i * 86400000).toISOString().slice(0, 10); }
function etIso(ts) { return new Date(ts * 1000).toLocaleDateString('en-CA', { timeZone: TZ }); }

function statsRange() {
  const c = S.data.calendar, t = dayIndex(c.today), wk = c.week ?? 0;
  const week = i => c.weeks[i] ? [dayIndex(c.weeks[i][0]), dayIndex(c.weeks[i][1])] : [0, -1];
  switch (S.st.range) {
    case 'week': return week(wk);
    case 'lastweek': return week(wk - 1);
    case '7': return [t - 7, t];
    case '15': return [t - 15, t];
    case '30': return [t - 30, t];
    case 'custom': return [S.st.from ? dayIndex(S.st.from) : 0, S.st.to ? dayIndex(S.st.to) : t];
    default: return [0, t];
  }
}

function statsPlayers() {
  const d = S.data, who = S.st.who;
  const onTeam = id => (team(id) || { roster: [] }).roster.map(e => [e.id, e]);
  if (who === 'mine') return onTeam(me());
  if (who === 'opp') return d.matchup ? onTeam(d.matchup.opp) : [];
  if (who.startsWith('t')) return onTeam(Number(who.slice(1)));
  const ps = Object.values(d.players).filter(p => who === 'all' || (who === 'fa' ? !p.owner : !!p.owner));
  return ps.map(p => [p.id, null]);
}

function statsRows() {
  const d = S.data, [lo, hi] = statsRange(), log = d.gamelog || {};
  const rows = [];
  for (const [id, entry] of statsPlayers()) {
    const p = P(id);
    if (!p) continue;
    let first = lo;
    if (entry && S.st.onTeam && entry.acquired !== 'DRAFT' && entry.since) first = Math.max(lo, dayIndex(etIso(entry.since)) + 1);
    const games = (log[String(id)] || []).filter(g => g[0] >= first && g[0] <= hi);
    const proj = p.ros.prior, pm = p.proj_mpg;
    const shots = games.every(g => g.length > 3);
    const r = { p, gp: games.length, min: 0, fp: 0, proj: proj * games.length, xmin: pm ? pm * games.length : null, xfp: pm ? 0 : null,
      fga: shots ? 0 : null, tpa: shots ? 0 : null, fta: shots ? 0 : null, sxfp: shots ? 0 : null, shoot: shots ? 0 : null, other: shots ? 0 : null };
    for (const [, fp, min, sf, sx, ox, fga, tpa, fta] of games) {
      r.fp += fp; r.min += min;
      if (pm) r.xfp += min * proj / pm;
      if (shots) { r.fga += fga; r.tpa += tpa; r.fta += fta; r.sxfp += sx + ox; r.shoot += sf - sx; r.other += fp - sf - ox; }
    }
    r.dfp = r.xfp == null ? null : r.fp - r.xfp;
    r.dsfp = r.sxfp == null ? null : r.fp - r.sxfp;
    rows.push(r);
  }
  return rows;
}

function viewStats() {
  const d = S.data, c = d.calendar, wk = c.week ?? 0;
  const ranges = [['week', 'This matchup'], ['lastweek', 'Last matchup'], ['7', '7 days'], ['15', '15 days'], ['30', '30 days'], ['season', 'Season']]
    .filter(([k]) => k !== 'lastweek' || wk > 0)
    .map(([k, l]) => `<button data-srange="${k}" class="${S.st.range === k ? 'on' : ''}">${l}</button>`).join('');
  const per = [['game', 'Per game'], ['total', 'Totals']]
    .map(([k, l]) => `<button data-sper="${k}" class="${S.st.per === k ? 'on' : ''}">${l}</button>`).join('');
  const views = [['min', 'Minutes'], ['shots', 'Shots']]
    .map(([k, l]) => `<button data-sview="${k}" class="${S.st.view === k ? 'on' : ''}">${l}</button>`).join('');
  const opp = d.matchup ? team(d.matchup.opp) : null;
  const teams = Object.values(d.league.teams).filter(t => t.id !== me() && (!opp || t.id !== opp.id))
    .sort((a, b) => a.abbrev.localeCompare(b.abbrev));
  const opt = (v, l) => `<option value="${v}"${S.st.who === v ? ' selected' : ''}>${esc(l)}</option>`;
  const whoSel = `<select id="swho">${opt('mine', 'My roster')}${opp ? opt('opp', 'Opponent: ' + opp.abbrev) : ''}${opt('rostered', 'All rostered')}${opt('fa', 'Free agents')}${opt('all', 'Everyone')}
    ${teams.map(t => opt('t' + t.id, t.abbrev + ' · ' + t.name)).join('')}</select>`;
  const pos = ['', 'PG', 'SG', 'SF', 'PF', 'C']
    .map(k => `<button data-pos="${k}" class="${S.pos === k ? 'on' : ''}">${k || 'All'}</button>`).join('');
  const [lo, hi] = statsRange();
  const isTeam = ['mine', 'opp'].includes(S.st.who) || S.st.who.startsWith('t');
  return `<div class="toolbar"><div class="seg">${ranges}</div></div>
    <div class="toolbar">${whoSel}<div class="seg">${views}</div><div class="seg">${per}</div>
      <label class="check small">From <input type="date" id="sfrom" value="${dayIso(Math.max(0, lo))}" min="${c.start}" max="${c.today}"></label>
      <label class="check small">to <input type="date" id="sto" value="${dayIso(Math.min(hi, dayIndex(c.today)))}" min="${c.start}" max="${c.today}"></label></div>
    <div class="toolbar"><div class="seg">${pos}</div>
      ${isTeam ? `<label class="check"><input type="checkbox" id="sonteam" ${S.st.onTeam ? 'checked' : ''}> Only games while on this team</label>` : ''}
      <input type="search" id="sq" placeholder="Search players or teams" value="${esc(S.q)}" autocomplete="off" spellcheck="false"></div>
    <div id="slist">${statsTable()}</div>
    <p class="small muted">${S.st.view === 'shots' ? `<b>xFP</b> here is his game at his own standards: the twos, threes and free throws he
    took at his own make rates, plus his minutes at his own rate for rebounds, assists, steals, blocks and turnovers. His standards are
    last season's totals (with ESPN's projection added for players with under 500 minutes last season), each make rate pulled toward the
    league's as if he had taken another 150 twos, 200 threes and 50 free throws at the league average. <b>Shooting</b> = his shooting
    points minus what those attempts were worth: making more or fewer than usual. <b>Other</b> = his other points minus what his minutes
    were worth. <b>vs xFP</b> = Shooting + Other. Shot volume shows in FGA, 3PA and FTA; ESPN gives no shot locations, so a two is a two.
    Shooting swings a lot from game to game; read it over weeks.</p><p class="small muted">` : ''}<b>PROJ</b>: his projected points a game (the preseason number the dashboard starts from) times games played.
    <b>xMIN</b>: projected minutes times games. <b>xFP</b>: the minutes he actually played times his projected points a minute, what the
    projection says those minutes were worth. So <b>xFP − PROJ</b> is the role (more or fewer minutes than expected) and <b>FP − xFP</b>
    (the <b>vs xFP</b> column) is what he did with the minutes. A game counts once it has finished (3½ hours after tip-off), and every game
    he played is counted, started or benched, so a team's FP here is not its matchup score.
    ${isTeam ? 'Players acquired by trade or pickup count from the day after they joined when the box is ticked (ESPN does not say which game an add first counted for).' : ''}</p>`;
}

function statsTable() {
  const per = S.st.per === 'game';
  const rows = statsRows().filter(r => passes(r.p) && (r.gp || ['mine', 'opp'].includes(S.st.who) || S.st.who.startsWith('t')));
  const val = (r, k) => k === 'name' ? r.p.name : k === 'gp' ? r.gp
    : r[k] == null ? null : per && k !== 'gp' ? (r.gp ? r[k] / r.gp : null) : r[k];
  const { sort, dir } = S.st;
  rows.sort((a, b) => {
    const x = val(a, sort), y = val(b, sort);
    if (x == null || y == null) return (x == null) - (y == null);
    return (sort === 'name' ? x.localeCompare(y) : x - y) * dir || b.gp - a.gp;
  });
  const shown = rows.slice(0, 200);
  const cols = S.st.view === 'shots'
    ? [['gp', 'GP'], ['min', 'MIN'], ['fga', 'FGA'], ['tpa', '3PA'], ['fta', 'FTA'], ['fp', 'FP'], ['sxfp', 'xFP'], ['shoot', 'Shooting'], ['other', 'Other'], ['dsfp', 'vs xFP']]
    : [['gp', 'GP'], ['min', 'MIN'], ['xmin', 'xMIN'], ['fp', 'FP'], ['proj', 'PROJ'], ['xfp', 'xFP'], ['dfp', 'vs xFP']];
  const fmt = (k, x) => x == null ? '–' : k === 'gp' ? x : ['dfp', 'dsfp', 'shoot', 'other'].includes(k) ? `<span class="${x > 0.5 ? 'pos' : x < -0.5 ? 'neg' : ''}">${per ? signed1(x) : signed(x)}</span>` : per ? n1(x) : n0(x);
  const head = `<tr><th data-ssort="name" class="sortable">Player${sort === 'name' ? (dir > 0 ? ' ▲' : ' ▼') : ''}</th>${cols.map(([k, l]) =>
    `<th class="r sortable" data-ssort="${k}">${l}${sort === k ? (dir > 0 ? ' ▲' : ' ▼') : ''}</th>`).join('')}</tr>`;
  const body = shown.map(r => `<tr data-pid="${r.p.id}" class="${r.p.owner === me() ? 'mine' : ''}"><td><div class="nm">${esc(r.p.name)} ${statusChip(r.p)}</div>
    <div class="meta">${meta(r.p)} · ${esc(ownerLabel(r.p))}</div></td>${cols.map(([k]) => `<td class="r">${fmt(k, val(r, k))}</td>`).join('')}</tr>`).join('');
  const t = { gp: 0, min: 0, fp: 0, proj: 0, xmin: 0, xfp: 0, fga: 0, tpa: 0, fta: 0, sxfp: 0, shoot: 0, other: 0 };
  for (const r of rows) for (const k of Object.keys(t)) t[k] += r[k] || 0;
  t.dfp = t.fp - t.xfp;
  t.dsfp = t.shoot + t.other;
  const tv = k => k === 'gp' ? t.gp : per ? (t.gp ? t[k] / t.gp : null) : t[k];
  const foot = rows.length > 1 && t.gp ? `<tr class="total"><td><b>All ${rows.length}</b>${per ? '<div class="meta">per game, across their games</div>' : ''}</td>${cols.map(([k]) => `<td class="r"><b>${fmt(k, tv(k))}</b></td>`).join('')}</tr>` : '';
  if (!rows.length) return '<div class="empty">No finished games in this range.</div>';
  return `<div class="card scroll-x"><table class="tbl stats"><thead>${head}</thead><tbody>${body}${foot}</tbody></table></div>
    ${rows.length > shown.length ? `<p class="small muted">Showing the first ${shown.length} of ${rows.length}; sort or filter to see others.</p>` : ''}`;
}

// ----- Roster -----
function viewRoster() {
  const seg = [['values', 'Values'], ['vs', 'Actual vs projected'], ['news', 'News']]
    .map(([k, l]) => `<button data-rtab="${k}" class="${S.rtab === k ? 'on' : ''}">${l}</button>`).join('');
  const body = S.rtab === 'vs' ? viewVsProjection() : S.rtab === 'news' ? viewTeamNews() : viewValues();
  return `<div class="toolbar"><div class="seg">${seg}</div></div>${body}`;
}

function viewValues() {
  const d = S.data;
  const entries = [...myRoster()].filter(e => P(e.id)).sort((a, b) => bySlot(a, b) || P(b.id).ros.value - P(a.id).ros.value);
  const ir = entries.filter(e => e.slot === 'IR').length;
  const rows = entries.map(e => {
    const p = P(e.id);
    const lu = p.lineup;
    return row(p, [['ROS', n1(p.ros.fppg)], ['wk starts', lu ? `${n1(lu.week_starts)}/${lu.week_games}` : '–', '', true],
      ['starts', lu ? n0(lu.starts) : '–', '', true], ['value', n0(p.ros.value)]],
      { prefix: `<span class="chip slot${e.slot === 'IR' ? ' ir' : ''}">${esc(e.slot)}</span> `, meta: meta(p) + ' · ' + esc(nextGame(p)),
        note: injuryNote(p), plain: true });
  }).join('');
  const drops = d.drop_order.slice(0, 4).map(id => esc(P(id).name)).join(', ');
  return `<h2>Your roster <span class="sub">${entries.length} players · IR ${ir}/${d.league.ir_slots}</span></h2>
    <div class="rows">${rows}</div>
    ${lineupTotals(entries)}
    <p class="small muted"><b>ROS</b>: projected points per game for the rest of the season. <b>wk starts</b>: expected starts in your lineup over
    the rest of this matchup week, out of his games left. <b>Starts</b>: expected starts from here to the end of the season if you hold this roster;
    only starters score, so this is his games less the ones he would sit on your bench. Players in IR are left out.
    <b>Value</b>: points above a waiver pickup over the rest of the season, playoff weeks counting 1.5×.
    Lowest value first: ${drops}.</p>`;
}

function lineupTotals(entries) {
  const t = team(me()).outlook;
  if (!t) return '';
  let games = 0, starts = 0, wg = 0, ws = 0;
  for (const e of entries) {
    const p = P(e.id), lu = p.lineup;
    if (!lu) continue;
    games += p.ros.games; starts += lu.starts; wg += lu.week_games; ws += lu.week_starts;
  }
  return `<div class="tiles">
    <div class="tile"><b>${n0(t.points)}</b><span>points from your starters over the rest of the season, about ${n0(t.per_week)} a week</span></div>
    <div class="tile"><b>${n0(starts)} of ${n0(games)}</b><span>expected starts of expected games; the rest sit on your bench</span></div>
    <div class="tile"><b>${n1(ws)} of ${wg}</b><span>starts of games left this week</span></div>
  </div>`;
}

const FLAGS = {
  hot: ['HOT', 'up', 'At least 8 games, 15% or more above his projection'],
  cold: ['COLD', 'down', 'At least 8 games, 15% or more below his projection'],
  min_up: ['MIN ▲', 'up', 'At least 3 games, 4 or more minutes a game above his projection'],
  min_down: ['MIN ▼', 'down', 'At least 3 games, 4 or more minutes a game below his projection'],
};

function viewVsProjection() {
  const d = S.data, R = d.team_report;
  if (!R) return '<div class="empty">This view fills in after the next refresh.</div>';
  const t = R.team, chk = R.league, out = [];
  if (!t.games) {
    out.push(`<div class="banner">No games yet: the season starts <b>${dayLabel(d.calendar.start)}</b>. Actual numbers fill in as games are played,
      with every refresh. Until then this shows the projections the dashboard is working from.</div>`);
  } else {
    const diff = t.points - t.projected, pct = t.projected ? 100 * diff / t.projected : 0;
    out.push(`<div class="tiles">
      <div class="tile"><b class="${diff >= 0 ? 'pos' : 'down'}">${signed(diff)}</b><span>points against projection over ${plural(t.games, 'game')} (${pct >= 0 ? '+' : '−'}${Math.abs(pct).toFixed(1)}%)</span></div>
      <div class="tile"><b>${n1(t.mpg)}</b><span>minutes a game${t.proj_mpg ? ', projected ' + n1(t.proj_mpg) : ''}</span></div>
      <div class="tile"><b>${t.games} of ${t.team_games}</b><span>team games played</span></div>
    </div>`);
  }
  const rows = R.rows.map(r => {
    const p = P(r.id);
    if (!p) return '';
    const flags = r.flags.map(f => FLAGS[f] ? `<span class="chip ${FLAGS[f][1]}" title="${esc(FLAGS[f][2])}">${FLAGS[f][0]}</span>` : '').join(' ');
    const bits = [r.gp ? `${r.gp} of ${r.team_gp} games` : esc(nextGame(p))];
    if (r.proj_mpg) bits.push(r.gp ? `${n1(r.mpg)} min (proj ${n1(r.proj_mpg)})` : `proj ${n1(r.proj_mpg)} min`);
    if (r.gp && p.stats.l7) bits.push(`last 7 days ${n1(p.stats.l7[2])}`);
    if (r.espn) bits.push(`ESPN proj ${n1(r.espn)}`);
    const latest = (p.news || [])[0];
    const when = latest && Date.parse(latest.at) / 1000;
    const note = latest ? `<span class="muted">${esc(when ? ago(when) : '')}:</span> ${esc(latest.headline)}` : '';
    return row(p, [['proj', n1(r.proj)], ['actual', r.gp ? n1(r.fppg) : '–'],
      ['gap', r.gp ? signed1(r.gap) : '–', r.gap == null ? '' : r.gap >= 0 ? 'pos' : 'down'], ['now', n1(r.now), '', true]],
      { prefix: `<span class="chip slot${r.slot === 'IR' ? ' ir' : ''}">${esc(r.slot)}</span> `,
        meta: (flags ? flags + ' ' : '') + bits.join(' · '), note, plain: true, cls: 'wrap' });
  }).join('');
  out.push(`<div class="rows">${rows}</div>`);
  if (chk && chk.games) {
    out.push(`<p class="small">Across the ${plural(chk.players, 'rostered player')} the draft board valued (${plural(chk.games, 'game')} so far), the board's
      projection misses by <b>${n1(chk.board_miss)}</b> points a game on average and ESPN's by <b>${n1(chk.espn_miss)}</b>; players are
      ${chk.board_bias >= 0 ? 'beating' : 'falling short of'} the board by ${n1(Math.abs(chk.board_bias))} a game. Over the first few weeks most of
      either miss is game-to-game noise.</p>`);
  }
  out.push(`<p class="small muted"><b>Proj</b>: the draft board's points per game for him (frozen at the draft${d.model.priors_from ? ', ' + esc(d.model.priors_from) : ''}).
    <b>Actual</b>: this season's fantasy points per game. <b>Gap</b>: actual minus projected; with few games it is mostly noise.
    <b>Now</b>: the per-game number the dashboard values him at: the projection moved toward his actual average as games accrue,
    the two counting equally after ${n0(d.model.prior_games)} games. <b>HOT/COLD</b>: 8+ games and 15% off projection.
    <b>MIN ▲/▼</b>: 3+ games and 4+ minutes a game off projection; minutes settle faster than scoring, so they are the first sign of a role change.</p>`);
  return out.join('');
}

function viewTeamNews() {
  const items = [];
  for (const e of myRoster()) {
    const p = P(e.id);
    for (const n of (p && p.news) || []) items.push([Date.parse(n.at) / 1000 || 0, p, n]);
  }
  items.sort((a, b) => b[0] - a[0]);
  if (!items.length) return '<div class="empty">No news on your players yet.</div>';
  const since = S.prev && S.prev.at;
  return `<p class="lead small">ESPN's fantasy news on your players, newest first. Tap one for his card.</p>
    <div class="card">${items.slice(0, 60).map(([ts, p, n]) => `<div class="news" data-pid="${p.id}">
      <div class="when"><b>${esc(p.name)}</b> ${statusChip(p)} · ${esc(ts ? whenLabel(ts) + ' · ' + ago(ts) : '')}${n.kind === 'article' ? ' · ESPN article' : ''}${since && ts > since ? ' <span class="chip new">NEW</span>' : ''}</div>
      <div class="hl">${esc(n.headline)}</div>${n.story ? `<div class="st">${esc(n.story)}</div>` : ''}</div>`).join('')}</div>`;
}

// ----- Waivers -----
function viewWaivers() {
  const seg = [['adds', 'Best adds'], ['stash', 'IR stash'], ['streams', 'Streams'], ['trending', 'Trending']]
    .map(([k, l]) => `<button data-wtab="${k}" class="${S.wtab === k ? 'on' : ''}">${l}</button>`).join('');
  const pos = ['', 'PG', 'SG', 'SF', 'PF', 'C']
    .map(k => `<button data-pos="${k}" class="${S.pos === k ? 'on' : ''}">${k || 'All'}</button>`).join('');
  return `<div class="toolbar"><div class="seg">${seg}</div></div>
    <div class="toolbar"><div class="seg">${pos}</div>
      <label class="check"><input type="checkbox" id="instant" ${S.instant ? 'checked' : ''}> Free agents only (no waivers)</label>
      <input type="search" id="q" placeholder="Search players or teams" value="${esc(S.q)}" autocomplete="off" spellcheck="false"></div>
    <div id="wlist">${waiverList()}</div>`;
}

function passes(p) {
  if (S.pos && !p.pos.includes(S.pos)) return false;
  if (S.instant && p.fa !== 'FREEAGENT') return false;
  if (S.q) {
    const q = S.q.toLowerCase();
    if (!p.name.toLowerCase().includes(q) && p.team.toLowerCase() !== q) return false;
  }
  return true;
}
function freeAgents() { return Object.values(S.data.players).filter(p => !p.owner); }

function waiverList() {
  const d = S.data;
  const dropId = d.upgrades.drop;
  const drop = dropId ? P(dropId) : null;
  const floor = drop ? drop.ros.value : 0;
  const waiverNote = d.waivers.all_on_waivers
    ? `<div class="banner small">Every free agent is on waivers until <b>${esc(d.waivers.clears_label)}</b>; claims then process in waiver order, and you are #${d.waivers.my_rank} of ${d.league.size}.</div>` : '';
  if (S.wtab === 'adds') {
    const list = freeAgents().filter(p => !p.ros.out && passes(p)).sort((a, b) => b.ros.value - a.ros.value).slice(0, 60);
    return waiverNote + `<p class="lead small">Rest-of-season value against your weakest player${drop ? `, <b>${esc(drop.name)}</b> (value ${n0(floor)})` : ''}.
      Replacement level is ${n1(d.replacement)} points per game.</p>
      <div class="rows">${list.map(p => row(p, [['ROS', n1(p.ros.fppg)], ['wk G', p.week.games, '', true], ['games', n1(p.ros.games), '', true],
        ['value', n0(p.ros.value)], ['vs drop', signed(p.ros.value - floor), p.ros.value > floor ? 'pos' : 'neg']],
        { meta: meta(p) + ' · ' + esc(nextGame(p)) + ` · ${n1(p.own)}% owned` })).join('') || '<div class="empty">No one matches.</div>'}</div>`;
  }
  if (S.wtab === 'stash') {
    const list = freeAgents().filter(p => p.status === 'OUT' && !p.ros.season_over && passes(p)).sort((a, b) => b.ros.value - a.ros.value).slice(0, 40);
    const free = d.stash.free_ir;
    return waiverNote + `<p class="lead small">Free agents tagged Out, so they can sit in an IR slot, ranked by what they add once back. You have <b>${plural(free, 'free IR slot')}</b>.
      Holding one costs nothing while he is Out, but ESPN may want a free roster spot for the add itself; a player still in IR after he is healthy blocks all your adds until you activate him.</p>
      <div class="rows">${list.map(p => row(p, [['back', p.ros.back ? dayShort(p.ros.back) : '?'], ['ROS', n1(p.ros.fppg)], ['games', n1(p.ros.games), '', true], ['value', n0(p.ros.value)]],
        { meta: meta(p) + ' · ' + esc([p.inj && p.inj.type, p.inj && p.inj.detail].filter(Boolean).join(' ') || 'injury'), note: p.inj && p.inj.short ? esc(clip(p.inj.short, 160)) : '' })).join('') || '<div class="empty">No one matches.</div>'}</div>`;
  }
  if (S.wtab === 'streams') {
    const st = d.streams;
    if (!st.days.length) return `<div class="empty">No days left in this matchup week.</div>`;
    const list = st.list.filter(r => passes(P(r.id)));
    return `<p class="lead small">Best pickups for the next day an add counts (${st.days.map(dayShort).join(', ')}): what he adds to that day's lineup, in place of your
      streaming spot${(st.spot || []).length ? ` (${st.spot.map(id => esc(P(id).name)).join(', ')})` : ''} or one of your lowest-valued players. <b>Cost</b> is rest-of-season value given up
      when the player dropped is a real one worth more; for the streaming spot it is half the season value given up, and negative when the add is worth more.
      Only free agents you can add right away are listed.</p>
      <div class="rows">${list.map(r => { const p = P(r.id); return row(p, [['games', r.games], ['starts', n1(r.starts), '', true], ['+pts', n0(r.gain), 'pos'], ['cost', n0(r.ros_cost), '', true], ['net', signed(r.net), r.net > 0 ? 'pos' : 'neg']],
        { meta: meta(p) + ' · drop ' + esc(P(r.drop).name) }); }).join('') || `<div class="empty">${d.waivers.all_on_waivers ? 'Nobody can be added outright until waivers clear.' : 'No pickup improves this week.'}</div>`}</div>
      ${hunchList()}`;
  }
  // trending
  const list = freeAgents().filter(p => passes(p) && p.own_chg).sort((a, b) => b.own_chg - a.own_chg).slice(0, 40);
  return `<p class="lead small">Free agents being added fastest across all ESPN leagues (change in % owned).</p>
    <div class="rows">${list.map(p => row(p, [['owned', n1(p.own) + '%', '', true], ['change', (p.own_chg > 0 ? '+' : '') + n1(p.own_chg), p.own_chg > 0 ? 'pos' : 'neg'], ['ROS', n1(p.ros.fppg)], ['value', n0(p.ros.value)]],
      { meta: meta(p) + ' · ' + esc(nextGame(p)) })).join('') || '<div class="empty">No one matches.</div>'}</div>`;
}

function hunchList() {
  const H = S.data.hunches;
  if (!H || !H.days.length) return '';
  const list = H.list.filter(r => passes(P(r.id)));
  return `<h2>On a hunch <span class="sub">${H.days.map(dayShort).join(', ')}</span></h2>
    <p class="lead small">Free agents playing that day whose role may be bigger than the projections know: minutes over their last few games
      well above ESPN's projection, a teammate at their position newly ruled out, or a jump in adds across ESPN. The points are still the model's,
      which does not read those signs; <b>vs top</b> is what the hunch costs against the best stream above if he only plays to projection.
      A stream lasts a day, so a wrong hunch is cheap.</p>
    <div class="rows">${list.map(r => { const p = P(r.id); return row(p, [['starts', n1(r.starts), '', true], ['net', signed(r.net), r.net > 0 ? 'pos' : 'neg'], ['vs top', signed(r.vs_top), r.vs_top >= 0 ? 'pos' : 'neg']],
      { meta: meta(p) + ' · ' + esc(nextGame(p)) + (r.drop ? ' · drop ' + esc(P(r.drop).name) : ''), note: r.signs.map(esc).join('<br>') }); }).join('')
      || '<div class="empty">No free agent playing that day shows a sign.</div>'}</div>`;
}

// ----- Injuries -----
function viewInjuries() {
  const seg = [['all', 'All'], ['mine', 'Yours'], ['opp', 'Opponent'], ['fa', 'Free agents'], ['league', 'Other teams']]
    .map(([k, l]) => `<button data-who="${k}" class="${S.who === k ? 'on' : ''}">${l}</button>`).join('');
  const rows = S.data.injuries.filter(r => S.who === 'all' || r.who === S.who).map(r => {
    const p = P(r.id);
    const ben = (r.beneficiaries || []).map(id => esc(P(id).name)).join(', ');
    const note = [injuryNote(p), ben ? `<span class="muted">On waivers at his positions:</span> ${ben}` : ''].filter(Boolean).join('<br>');
    return row(p, [['back', p.ros.out ? (p.ros.season_over ? 'season' : p.ros.back ? dayShort(p.ros.back) : '?') : '–'], ['value', n0(p.ros.value)]],
      { meta: meta(p) + ' · ' + esc(ownerLabel(p)), note });
  }).join('');
  return `<div class="toolbar"><div class="seg scroll-x">${seg}</div></div>
    <p class="lead small">Everyone hurt whom a team holds or might add, from ESPN's injury report. Return dates are ESPN's estimates; long layoffs often run later.</p>
    <div class="rows">${rows || '<div class="empty">No injuries here.</div>'}</div>`;
}

// ----- League -----
function viewLeague() {
  const d = S.data, lg = d.league;
  const teams = Object.values(lg.teams).sort((a, b) => (b.record.w - a.record.w) || (b.record.pf - a.record.pf));
  const standings = teams.map((t, i) => `<tr class="${t.id === me() ? 'mine' : ''}"><td>${i + 1}</td><td><b>${esc(t.abbrev)}</b> <span class="muted">${esc(t.name)}</span></td>
    <td class="r">${t.record.w}-${t.record.l}${t.record.t ? '-' + t.record.t : ''}</td><td class="r">${n0(t.record.pf)}</td><td class="r">${n0(t.record.pa)}</td><td class="r">${t.outlook ? n0(t.outlook.per_week) : '–'}</td><td class="r">${t.waiver_rank || '–'}</td></tr>`).join('');
  const week = d.calendar.week;
  const games = lg.matchups.filter(m => week != null && m.period === week + 1).map(m =>
    `<tr class="${[m.home, m.away].includes(me()) ? 'mine' : ''}"><td>${esc(abbr(m.home))}</td><td class="r">${n0(m.home_pts)}</td><td class="r">${n0(m.away_pts)}</td><td>${esc(abbr(m.away))}</td></tr>`).join('');
  const tx = d.transactions.map(t => {
    const what = [];
    if (t.adds.length) what.push('added ' + t.adds.map(id => esc((P(id) || { name: '#' + id }).name)).join(', '));
    if (t.drops.length) what.push('dropped ' + t.drops.map(id => esc((P(id) || { name: '#' + id }).name)).join(', '));
    if (t.trade.length) what.push('trade: ' + t.trade.map(([id, f, to]) => esc((P(id) || { name: '#' + id }).name) + ' to ' + esc(abbr(to))).join(', '));
    return `<tr class="${t.team === me() ? 'mine' : ''}"><td class="muted">${esc(whenLabel(t.at))}</td><td><b>${esc(abbr(t.team))}</b> ${what.join('; ')}</td></tr>`;
  }).join('');
  return `<h2>Standings</h2><div class="card scroll-x"><table class="tbl"><thead><tr><th>#</th><th>Team</th><th class="r">W-L</th><th class="r">PF</th><th class="r">PA</th><th class="r" title="Points a week from each roster's best daily lineups over the rest of the season, as it stands">Proj/wk</th><th class="r">Waiver</th></tr></thead><tbody>${standings}</tbody></table></div>
    <p class="small muted"><b>Proj/wk</b>: points a week each roster's starters should score over the rest of the season if it is held as is (players in IR left out).</p>
    ${games ? `<h2>This week</h2><div class="card scroll-x"><table class="tbl"><tbody>${games}</tbody></table></div>` : ''}
    <h2>Recent moves <span class="sub">last four days</span></h2><div class="card scroll-x">${tx ? `<table class="tbl"><tbody>${tx}</tbody></table>` : '<div class="muted">No adds, drops or trades yet.</div>'}</div>
    <p class="small muted" style="margin-top:20px">Values start from the draft board (${esc(d.model.priors_from || '?')}) and move toward each player's actual scoring as games come in. Refreshed every 20 minutes from ESPN.
    <a href="#" id="lock-now">Forget the passphrase on this device</a></p>`;
}

// ----- player card -----
function openPlayer(id) {
  const p = P(id);
  if (!p) return;
  const r = p.ros, i = p.inj;
  const statRows = [['This season', 'season'], ['Last 7 days', 'l7'], ['Last 15 days', 'l15'], ['Last 30 days', 'l30'], ['Last season', 'last'], ['ESPN projection', 'proj']]
    .filter(([, k]) => p.stats[k]).map(([l, k]) => { const s = p.stats[k]; return `<tr><td>${l}</td><td class="r">${s[0]}</td><td class="r">${n1(s[1])}</td><td class="r">${n1(s[2])}</td></tr>`; }).join('');
  const start = S.data.calendar.start;
  const recent = (p.recent || []).filter(g => g[0] >= 1).map(g => {
    const day = new Date(isoDate(start).getTime() + (g[0] - 1) * 86400000).toISOString().slice(0, 10);
    return `<tr><td>${dayShort(day)}</td><td class="r">${g[2]}</td><td class="r">${n1(g[1])}</td></tr>`;
  }).join('');
  const sched = (p.next || []).map(([d, opp, home, tip]) => `<tr><td>${dayShort(d)}</td><td>${home ? 'vs' : '@'} ${esc(opp)}</td><td>${timeLabel(tip)}</td><td class="r">${p.odds && p.odds[d] != null ? Math.round(100 * p.odds[d]) + '%' : ''}</td></tr>`).join('');
  const news = (p.news || []).map(n => `<div class="news"><div class="when">${esc(n.at ? whenLabel(Date.parse(n.at) / 1000) + ' · ' + ago(Date.parse(n.at) / 1000) : '')}${n.kind === 'article' ? ' · ESPN article' : ''}</div>
    <div class="hl">${esc(n.headline)}</div>${n.story ? `<div class="st">${esc(n.story)}</div>` : ''}</div>`).join('');
  const prior = r.mpg != null
    ? `${n1(r.mpg)} min × ${r.rate.toFixed(2)} a minute · ${p.stats.season ? `his ${n1(p.stats.season[1])} min over ${plural(p.stats.season[0], 'game')} (recent ones first) blended with the ${esc(r.prior_from)}'s ${n1(p.proj_mpg)} (${Math.round(100 * (1 - r.prior_weight))}% weight on this season); preseason ${n1(r.prior)} a game` : `the ${esc(r.prior_from)}'s preseason minutes and rate (no games yet this season)`}`
    : r.prior_weight < 1
    ? `${n1(r.prior)} from the ${esc(r.prior_from)}, blended with ${p.stats.season ? n1(p.stats.season[2]) + ' over ' + plural(p.stats.season[0], 'game') : 'this season'} (${Math.round(100 * (1 - r.prior_weight))}% weight on this season)`
    : `${n1(r.prior)} from the ${esc(r.prior_from)} (no games yet this season)`;
  $('#drawer').innerHTML = `<button class="close" id="drawer-close" aria-label="Close">×</button>
    <h3>${esc(p.name)} ${statusChip(p)}</h3>
    <div class="muted">${esc(p.team)} · ${esc(p.pos.join('/'))} · ${esc(ownerLabel(p))}${p.owner ? '' : (p.fa === 'WAIVERS' && p.waiver_until ? ' · on waivers until ' + esc(whenLabel(p.waiver_until)) : ' · can be added now')} · ${n1(p.own)}% owned on ESPN</div>
    ${p.note ? `<div class="banner small" style="margin-top:10px">${esc(p.note)}</div>` : ''}
    ${i || p.status !== 'ACTIVE' ? `<div class="sec"><div class="h">Injury</div><dl class="kv">
      <dt>ESPN tag</dt><dd>${esc(p.status.replace(/_/g, ' ').toLowerCase())}${p.status === 'OUT' ? ' (can go in an IR slot)' : ''}</dd>
      ${i ? `<dt>Report</dt><dd>${esc([i.status, i.side, i.type, i.detail].filter(Boolean).join(' · '))}</dd>
      <dt>Expected back</dt><dd>${i.return ? esc(dayLabel(i.return)) + ' (ESPN estimate)' : '–'}</dd>
      <dt>Updated</dt><dd>${i.updated ? esc(whenLabel(Date.parse(i.updated) / 1000)) : '–'}</dd>` : ''}</dl>
      ${i && i.short ? `<p class="small">${esc(i.short)}</p>` : ''}${i && i.long && i.long !== i.short ? `<p class="small muted">${esc(i.long)}</p>` : ''}</div>` : ''}
    <div class="sec"><div class="h">Rest of season</div><dl class="kv">
      <dt>Per game</dt><dd><b>${n1(r.fppg)}</b> · ${prior}</dd>
      <dt>Games</dt><dd>${n1(r.games)} expected of ${r.team_games} left${r.absent ? ` · ${n1(r.absent)} missed while Out` : ''} · ${n1(r.playoff_games)} in the playoff weeks</dd>
      ${p.lineup ? `<dt>Starts</dt><dd><b>${n0(p.lineup.starts)}</b> expected in your lineup, ${n0(p.lineup.points)} points from them</dd>` : ''}
      <dt>Value</dt><dd><b>${n0(r.value)}</b> points above a waiver pickup${r.bar ? ` (break-even ${n1(r.bar)} per game)` : ''}</dd>
      <dt>This week</dt><dd>${p.lineup ? `${plural(p.lineup.week_games, 'game')}, ${n1(p.lineup.week_starts)} expected starts, ${n1(p.lineup.week_points)} points from them`
        : `${plural(p.week.games, 'game')}, ${n1(p.week.points)} expected points if he starts them all`}</dd></dl></div>
    ${sched ? `<div class="sec"><div class="h">Next games</div><table class="tbl"><thead><tr><th>Day</th><th>Game</th><th>Tip</th><th class="r">Plays</th></tr></thead><tbody>${sched}</tbody></table></div>` : ''}
    ${statRows ? `<div class="sec"><div class="h">Fantasy points</div><table class="tbl"><thead><tr><th></th><th class="r">GP</th><th class="r">Min</th><th class="r">Per game</th></tr></thead><tbody>${statRows}</tbody></table></div>` : ''}
    ${recent ? `<div class="sec"><div class="h">Latest games</div><table class="tbl"><thead><tr><th>Day</th><th class="r">Min</th><th class="r">Points</th></tr></thead><tbody>${recent}</tbody></table></div>` : ''}
    ${news ? `<div class="sec"><div class="h">News</div>${news}</div>` : ''}`;
  $('#drawer').hidden = false;
  $('#scrim').hidden = false;
  $('#drawer').scrollTop = 0;
  $('#drawer-close').focus();
}
function closeDrawer() { $('#drawer').hidden = true; $('#scrim').hidden = true; }

// ---------- what changed since the last visit ----------
function computeChanges() {
  const d = S.data;
  const watch = new Set(myRoster().map(e => e.id));
  for (const p of freeAgents().sort((a, b) => b.ros.value - a.ros.value).slice(0, 40)) watch.add(p.id);
  for (const r of d.stash.list) watch.add(r.id);
  const snap = {};
  for (const id of watch) { const p = P(id); if (p) snap[id] = [p.status, p.owner]; }
  const prev = S.prev, out = [];
  const label = s => ({ ACTIVE: 'healthy', DAY_TO_DAY: 'day-to-day', OUT: 'Out', SUSPENSION: 'suspended' }[s] || s);
  if (prev && prev.snap) {
    for (const [id, [status, owner]] of Object.entries(snap)) {
      const was = prev.snap[id], p = P(id);
      if (!was) continue;
      const mine = p.owner === me() ? ' <span class="muted">(yours)</span>' : '';
      if (was[0] !== status) out.push(`<b>${esc(p.name)}</b>${mine}: ${label(was[0])} → <b>${label(status)}</b>`);
      if (was[1] === 0 && owner && owner !== me()) out.push(`<b>${esc(p.name)}</b> was picked up by ${esc(abbr(owner))}`);
    }
  }
  S.changes = out;
  setItem('hq.seen', JSON.stringify({ at: d.generated, snap }));
}

// ---------- loading ----------
async function fetchData() {
  if (IS_LOCAL) {
    try {
      const r = await fetch('data.json?t=' + Date.now(), { cache: 'no-store' });
      if (r.ok) return await r.json();
    } catch (e) { /* fall through to the encrypted copy */ }
  }
  if (!S.key) { const e = new Error('no passphrase'); e.code = 'nokey'; throw e; }
  const r = await fetch('data.enc?t=' + Date.now(), { cache: 'no-store' });
  if (!r.ok) throw new Error('could not load data.enc (HTTP ' + r.status + ')');
  return JSON.parse(await hqDecrypt(await r.arrayBuffer(), S.key));
}

let loading = false;
async function refresh(first) {
  if (loading) return;
  loading = true;
  $('#btn-reload').classList.add('spin');
  try {
    const d = await fetchData();
    const changed = !S.data || d.generated !== S.data.generated;
    S.data = d;
    if (changed) {
      if (first) S.prev = getJSON('hq.seen');
      computeChanges();
      showApp();
      render();
    } else {
      freshness();
    }
  } catch (e) {
    if (e.code === 'nokey' || e.code === 'badkey') {
      showLock(e.code === 'badkey' ? 'That passphrase did not open the data.' : '');
    } else if (!S.data) {
      $('#view').innerHTML = `<div class="banner bad">Could not load the data: ${esc(e.message)}. Try again in a minute.</div>`;
    }
  } finally {
    loading = false;
    $('#btn-reload').classList.remove('spin');
  }
}

function showLock(msg) {
  S.data = null;
  $('#lock').hidden = false;
  $('#view').innerHTML = '';
  $('#tabs').hidden = true;
  $('#btn-reload').hidden = true;
  $('#lock-error').hidden = !msg;
  $('#lock-error').textContent = msg || '';
  $('#lock-input').focus();
}
function showApp() {
  $('#lock').hidden = true;
  $('#tabs').hidden = false;
  $('#btn-reload').hidden = false;
}

function boot() {
  if (location.hash.startsWith('#k=')) {
    S.key = decodeURIComponent(location.hash.slice(3));
    setItem('hq.key', S.key);
    history.replaceState(null, '', location.pathname + location.search);
  } else {
    S.key = getItem('hq.key');
  }
  S.tab = getItem('hq.tab') || 'today';
  S.rtab = getItem('hq.rtab') || 'values';
  Object.assign(S.st, getJSON('hq.stats') || {});

  $('#tabs').addEventListener('click', e => {
    const b = e.target.closest('button[data-tab]');
    if (!b) return;
    S.tab = b.dataset.tab;
    setItem('hq.tab', S.tab);
    render();
    window.scrollTo(0, 0);
  });
  $('#btn-reload').addEventListener('click', () => refresh(false));
  $('#view').addEventListener('click', e => {
    const t = e.target.closest('[data-wtab],[data-rtab],[data-pos],[data-who],[data-srange],[data-sper],[data-sview],[data-ssort],[data-pid],[data-goto],[data-dismiss],[data-unhide],#lock-now');
    if (!t) return;
    if (t.dataset.srange != null) { S.st.range = t.dataset.srange; saveStats(); render(); return; }
    if (t.dataset.sper != null) { S.st.per = t.dataset.sper; saveStats(); render(); return; }
    if (t.dataset.sview != null) { S.st.view = t.dataset.sview; saveStats(); render(); return; }
    if (t.dataset.ssort != null) {
      S.st.dir = S.st.sort === t.dataset.ssort ? -S.st.dir : (t.dataset.ssort === 'name' ? 1 : -1);
      S.st.sort = t.dataset.ssort; render(); return;
    }
    if (t.dataset.dismiss != null) { setHidden(t.dataset.dismiss, true); render(); return; }
    if (t.dataset.unhide) { e.preventDefault(); setHidden(null, false); render(); return; }
    if (t.dataset.goto) { e.preventDefault(); S.tab = t.dataset.goto; setItem('hq.tab', S.tab); render(); window.scrollTo(0, 0); return; }
    if (t.id === 'lock-now') { e.preventDefault(); setItem('hq.key', null); S.key = null; showLock(''); return; }
    if (t.dataset.wtab != null) { S.wtab = t.dataset.wtab; render(); return; }
    if (t.dataset.rtab != null) { S.rtab = t.dataset.rtab; setItem('hq.rtab', S.rtab); render(); return; }
    if (t.dataset.pos != null) { S.pos = t.dataset.pos; render(); return; }
    if (t.dataset.who != null) { S.who = t.dataset.who; render(); return; }
    if (t.dataset.pid) openPlayer(t.dataset.pid);
  });
  $('#view').addEventListener('input', e => {
    if (e.target.id === 'q') { S.q = e.target.value.trim(); $('#wlist').innerHTML = waiverList(); }
    if (e.target.id === 'sq') { S.q = e.target.value.trim(); $('#slist').innerHTML = statsTable(); }
  });
  $('#view').addEventListener('change', e => {
    if (e.target.id === 'instant') { S.instant = e.target.checked; $('#wlist').innerHTML = waiverList(); }
    if (e.target.id === 'swho') { S.st.who = e.target.value; saveStats(); render(); }
    if (e.target.id === 'sonteam') { S.st.onTeam = e.target.checked; saveStats(); $('#slist').innerHTML = statsTable(); }
    if (e.target.id === 'sfrom' || e.target.id === 'sto') {
      S.st[e.target.id === 'sfrom' ? 'from' : 'to'] = e.target.value; S.st.range = 'custom'; saveStats(); render();
    }
  });
  $('#drawer').addEventListener('click', e => { if (e.target.id === 'drawer-close') closeDrawer(); });
  $('#scrim').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });
  $('#lock-form').addEventListener('submit', e => {
    e.preventDefault();
    S.key = $('#lock-input').value.trim();
    if ($('#lock-remember').checked) setItem('hq.key', S.key);
    $('#lock-input').value = '';
    refresh(true);
  });
  refresh(true);
  setInterval(() => refresh(false), 5 * 60 * 1000);
  setInterval(() => { if (S.data) freshness(); }, 60 * 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(false); });
}

boot();
