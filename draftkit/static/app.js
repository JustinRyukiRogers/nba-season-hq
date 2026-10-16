"use strict";
// Draft board front end. /api/board is loaded once (the player pool and its valuations);
// /api/state is polled for everything that moves during the draft.

let B = null;            // board payload
let S = null;            // latest state payload
let P = {};              // player id -> player
const ui = { search: "", pos: "", showTaken: false, sort: "rank", dir: 1, selected: null, view: "players", resetArmed: false, busy: false };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 0) => (v == null || isNaN(v) ? "" : Number(v).toFixed(d));
const pct = (v) => (v == null ? "" : Math.round(100 * v) + "%");
const SPECIFIC = ["PG", "SG", "SF", "PF", "C", "G", "F"];
// research/roster_fit.py: up to five players who can only play C cost nothing; past that they lose games
const C_ONLY_LIMIT = 5;
const cOnly = (p) => p.pos.length === 1 && p.pos[0] === "C";
const myCOnly = () => S.picks.filter((k) => k.team === B.league.my_team && P[k.player_id] && cOnly(P[k.player_id])).length;
const cCrowd = (n) => `You have ${n} players who can only play C, and they share ${(B.league.slots.C || 0) + (B.league.slots.UT || 0)} starting slots (C and UT). Past ${C_ONLY_LIMIT}, some of their games sit on the bench on busy nights.`;

const team = (id) => (B.league.teams[id] || { name: "Team " + id, abbrev: "T" + id });
// "Mar 8-28": the fantasy playoff weeks, from the league's own calendar
const playoffSpan = () => {
  const first = B.weeks[B.regular_weeks], last = B.weeks[B.weeks.length - 1];
  if (!first || !last) return "playoff weeks";
  const [a, b] = [first[0], last[1]].map((d) => new Date(d + "T12:00:00"));
  const mon = (d) => d.toLocaleDateString("en-US", { month: "short" });
  return `${mon(a)} ${a.getDate()}-${mon(a) === mon(b) ? "" : mon(b) + " "}${b.getDate()}`;
};
const takenMap = () => { const m = {}; for (const p of S.picks) m[p.player_id] = p; return m; };
const valueOf = (p) => (S.roster_values[p.id] != null ? S.roster_values[p.id] : p.value);

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "request failed");
  return data;
}

async function act(path, body, done) {
  if (ui.busy) return;
  ui.busy = true;
  try {
    S = await api(path, body || {});
    if (done) done();
    render();
  } catch (err) {
    toast(err.message);
  } finally {
    ui.busy = false;
  }
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 3500);
}

function draft(id) {
  const p = P[id];
  const who = S.pick.mine ? "you" : team(S.pick.team).name;
  act("/api/pick", { player_id: id }, () => {
    toast((p ? p.name : "Player") + " drafted by " + who + ".  Undo is in the top bar.");
    ui.search = ""; $("search").value = "";
    if (ui.selected === id) ui.selected = null;
  });
}

// ---------- top bar ----------

function renderBar() {
  $("league-name").textContent = B.league.name;
  const k = S.pick, clock = $("clock");
  if (S.done) {
    clock.innerHTML = "<b>Draft complete.</b>";
  } else {
    const next = S.my_picks.slice(1);          // my picks after the one I am on or waiting for
    const mineNow = k.mine;
    const wait = S.my_picks.length ? S.my_picks[0] - k.overall : null;
    let html = `Round <b>${k.round}</b>, pick <b>${k.in_round}</b> <span class="num-muted">(#${k.overall} of ${k.total})</span> &middot; On the clock: <b class="${mineNow ? "you" : ""}">${mineNow ? "YOU" : esc(team(k.team).name)}</b>`;
    if (!mineNow && wait != null) html += ` &middot; you pick in <b>${wait}</b> (#${S.my_picks[0]})`;
    if (next.length) html += ` <span class="num-muted">&middot; then #${next.slice(0, 2).join(", #")}</span>`;
    if (S.order_confirmed === false) html += ` <span class="num-muted" title="Only your own seat was set by hand. Team names on the clock may be off until the real order is in; your pick numbers are right.">&middot; other seats unconfirmed</span>`;
    if (!S.picks.length && B.league.draft_ms) {
      html += ` <span class="num-muted">&middot; draft starts ${new Date(B.league.draft_ms).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</span>`;
    }
    clock.innerHTML = html;
  }
  const s = S.sync, chip = $("sync-chip");
  const look = !s.available ? ["", "Manual entry"]
    : !s.enabled ? ["", "ESPN sync paused"]
    : s.state === "live" ? ["good", "ESPN live"]
    : s.state === "finished" ? ["good", "ESPN: draft finished"]
    : s.state === "error" ? ["critical", "ESPN sync problem"]
    : s.state === "waiting" ? ["warning", "ESPN connected, draft not started"]
    : ["", "ESPN sync starting"];
  chip.innerHTML = `<span class="dot ${look[0]}"></span>${esc(look[1])}`;
  chip.title = s.message || (s.last_ok ? "Last update " + s.last_ok : "");
  const live = s.enabled && (s.state === "live" || s.state === "finished") && s.espn_picks > 0;
  $("btn-mock-one").disabled = $("btn-mock-me").disabled = S.done || live;
  $("mock-group").title = live ? "Practice picks are off while ESPN's draft is live" : "";
  $("btn-undo").disabled = !S.picks.length;
  const reset = $("btn-reset");
  reset.textContent = ui.resetArmed ? "Click again to clear all picks" : "Reset";
  reset.classList.toggle("armed", ui.resetArmed);
  reset.disabled = !S.picks.length;
}

// ---------- recommendation ----------

function meta(p) {
  return `${esc(p.team)} &middot; ${esc(p.pos.join("/"))}${p.age ? " &middot; " + Math.round(p.age) : ""}`;
}

function renderRec() {
  const el = $("rec");
  el.classList.toggle("mine", !S.done && S.pick.mine);
  if (S.done) {
    const totals = {};
    for (const k of S.picks) totals[k.team] = (totals[k.team] || 0) + (P[k.player_id] ? P[k.player_id].value : 0);
    const order = Object.keys(totals).sort((a, b) => totals[b] - totals[a]);
    const place = order.indexOf(String(B.league.my_team)) + 1;
    el.innerHTML = `<div class="rec-head"><span class="rec-title">Draft complete</span></div>
      <div class="empty">By this board's values your roster ranks <b>${place}</b> of ${order.length}. The draft grid shows every team.</div>`;
    return;
  }
  if (!S.my_picks.length) {
    el.innerHTML = `<div class="rec-head"><span class="rec-title">No picks left for you</span></div>`;
    return;
  }
  const h = S.horizons;
  if (S.pick.mine) {
    const cands = S.recommend.slice(0, 3);
    const centers = myCOnly();
    const cards = cands.map((c, i) => {
      const p = P[c.id];
      return `<div class="cand ${i === 0 ? "top" : ""}" data-id="${c.id}">
        <div class="cand-label">${i === 0 ? "Take" : "Or"}</div>
        <div class="cand-name">${esc(p.name)}</div>
        <div class="cand-meta">${meta(p)} &middot; ${fmt(p.fppg, 1)} per game &middot; room rank ${p.market}</div>
        <div class="cand-reason">${esc(c.reason)}${c.gap ? `. Costs about ${fmt(c.gap)} pts across this pick and the next.` : ""}</div>
        ${centers >= C_ONLY_LIMIT && cOnly(p) ? `<div class="cand-warn"><span class="dot warning"></span><span>${esc(cCrowd(centers))}</span></div>` : ""}
        <div class="cand-row"><span class="cand-num"><b>${fmt(c.value)}</b> value</span>
          <button class="${i === 0 ? "primary" : ""}" data-draft="${c.id}">Draft</button></div>
      </div>`;
    }).join("");
    el.innerHTML = `<div class="rec-head"><span class="rec-title">Your pick &mdash; #${S.pick.overall}</span>
        <span class="rec-sub">${h.length ? "Scored on this pick plus what should be left at #" + h[0] : "Your last pick"}${S.need_only ? " &middot; limited to players who fill an open " + esc(S.needs.join("/")) + " slot" : ""}</span></div>
      <div class="rec-grid">${cards || '<div class="empty">Nobody left on the board.</div>'}</div>`;
  } else {
    const wait = S.my_picks[0] - S.pick.overall;
    const cards = S.likely.slice(0, 5).map(([id, prob]) => {
      const p = P[id];
      return `<div class="cand" data-id="${id}">
        <div class="cand-name">${esc(p.name)}</div>
        <div class="cand-meta">${meta(p)}</div>
        <div class="cand-row"><span class="cand-num"><b>${pct(prob)}</b> your best option</span><span class="cand-num num-muted">${fmt(valueOf(p))}</span></div>
      </div>`;
    }).join("");
    el.innerHTML = `<div class="rec-head"><span class="rec-title">You pick at #${S.my_picks[0]} &mdash; ${wait} pick${wait === 1 ? "" : "s"} away</span>
        <span class="rec-sub">Who is most likely to be the best player left when you pick, from ${"400"} simulated runs of the room</span></div>
      <div class="rec-grid waiting">${cards || '<div class="empty">Waiting for the board.</div>'}</div>`;
  }
}

// ---------- players table ----------

const COLS = [
  { key: "rank", label: "#", get: (p) => p.rank, dir: 1 },
  { key: "name", label: "Player", left: true, get: (p) => p.name, dir: 1 },
  { key: "value", label: "Value", get: (p) => valueOf(p), dir: -1, title: "Fantasy points above a waiver pickup over the fantasy season, playoff weeks weighted up" },
  { key: "fppg", label: "Ours", get: (p) => p.fppg, dir: -1, title: "Our fantasy points per game" },
  { key: "espn", label: "ESPN", get: (p) => p.espn.fppg, dir: -1, title: "ESPN's projected fantasy points per game" },
  { key: "games", label: "Games", get: (p) => p.games, dir: -1, title: "Expected games played inside the fantasy season" },
  { key: "adp", label: "ADP", get: (p) => p.espn.adp || 999, dir: 1, title: "Average draft position in ESPN leagues" },
  { key: "edge", label: "vs room", get: (p) => p.market - p.rank, dir: -1, title: "How many spots later the room ranks him than this board does" },
  { key: "s1", label: "", get: (p) => surv(p, 0), dir: -1, title: "Chance he is still available at your next pick" },
  { key: "s2", label: "", get: (p) => surv(p, 1), dir: -1, title: "Chance he is still available at the pick after that" },
];
const surv = (p, i) => { const s = S.survive[p.id]; return s ? s[i] : -1; };
const narrow = window.matchMedia("(max-width: 1320px)");
const cols = () => (narrow.matches ? COLS.filter((c) => c.key !== "espn" && c.key !== "s2") : COLS);

function flagsHtml(p, centers = 0) {
  const out = [];
  if (centers >= C_ONLY_LIMIT && cOnly(p)) out.push(`<span class="flag" title="${esc(cCrowd(centers))}"><span class="dot warning"></span>C-only #${centers + 1}</span>`);
  if (p.out_until) out.push(`<span class="flag" title="Expected back around ${esc(p.out_until)} (${esc(p.out_source || "")})"><span class="dot critical"></span>out to ${esc(p.out_until.slice(5))}</span>`);
  else if (p.injury && p.injury !== "ACTIVE") out.push(`<span class="flag" title="ESPN status: ${esc(p.injury)}"><span class="dot warning"></span>${p.injury === "DAY_TO_DAY" ? "day-to-day" : esc(p.injury.toLowerCase())}</span>`);
  if (p.flags.includes("rookie")) out.push('<span class="flag">rookie</span>');
  else if (p.flags.includes("new_team")) out.push('<span class="flag">new team</span>');
  if (p.flags.includes("inj_return") && !p.flags.includes("rookie")) out.push('<span class="flag" title="Played under 50 games last season">hurt last yr</span>');
  if (p.intel && (p.intel.applied_minutes || p.intel.applied_rate || p.intel.games_rate || p.intel.out_until)) out.push('<span class="flag" title="A research note changes his numbers; open his card">adjusted</span>');
  return out.length ? `<span class="flags">${out.join("")}</span>` : "";
}

function visiblePlayers() {
  const taken = takenMap();
  const words = ui.search.toLowerCase().split(/\s+/).filter(Boolean);
  // a word can also be an NBA team, in the board's spelling (NOP) or ESPN's (Bkn, Phx, Utah)
  const alias = B.team_aliases || {};
  const codes = words.map((w) => alias[w.toUpperCase()] || w.toUpperCase());
  const col = COLS.find((c) => c.key === ui.sort) || COLS[0];
  const rows = B.players.filter((p) => {
    if (!ui.showTaken && !words.length && taken[p.id]) return false;
    if (ui.pos && !p.pos.includes(ui.pos)) return false;
    const name = p.name.toLowerCase();
    return words.every((w, i) => name.includes(w) || p.team === codes[i]);
  });
  rows.sort((a, b) => {
    const x = col.get(a), y = col.get(b);
    return (x < y ? -1 : x > y ? 1 : 0) * ui.dir || a.rank - b.rank;
  });
  return { rows, taken };
}

function renderTable() {
  const h = S.horizons;
  COLS[8].label = h[0] ? "There at #" + h[0] : "";
  COLS[9].label = h[1] ? "at #" + h[1] : "";
  const shown = cols(), has = (key) => shown.some((c) => c.key === key);
  document.querySelector("#board-table colgroup").innerHTML = shown.map((c) => `<col class="c-${c.key}">`).join("") + '<col class="c-act">';
  document.querySelector("#board-table thead").innerHTML = "<tr>" + shown.map((c) =>
    `<th class="${c.left ? "left " : ""}${ui.sort === c.key ? "sorted" : ""}" data-sort="${c.key}" title="${esc(c.title || "")}">${esc(c.label)}${ui.sort === c.key ? (ui.dir > 0 ? " ▴" : " ▾") : ""}</th>`).join("") + '<th class="nosort"></th></tr>';

  const { rows, taken } = visiblePlayers();
  const maxValue = Math.max(1, ...B.players.filter((p) => !taken[p.id]).map(valueOf));
  const showTiers = ui.sort === "rank" && ui.dir > 0 && !ui.search && !ui.pos;
  const btn = S.done ? "" : S.pick.mine ? "Draft" : "→ " + team(S.pick.team).abbrev;
  const centers = myCOnly();
  let lastTier = null;
  const html = [];
  for (const p of rows) {
    const t = taken[p.id];
    if (showTiers && !t && p.tier !== lastTier) {
      if (lastTier !== null) html.push(`<tr class="tier"><td colspan="${shown.length + 1}">Tier ${p.tier}</td></tr>`);
      lastTier = p.tier;
    }
    const v = valueOf(p), edge = p.market - p.rank, s = S.survive[p.id];
    const cell = (x) => (x == null ? "" : `${pct(x)}<span class="meter small"><i style="width:${Math.round(100 * x)}%"></i></span>`);
    html.push(`<tr class="row ${t ? "taken" : ""} ${ui.selected === p.id ? "selected" : ""}" data-id="${p.id}">
      <td class="num-muted">${p.rank}</td>
      <td class="left who"><div class="who-in"><span class="pname">${esc(p.name)}</span><span class="psub">${esc(p.team)} &middot; ${esc(p.pos.join("/"))}</span>${flagsHtml(p, t ? 0 : centers)}</div></td>
      <td>${fmt(v)}<span class="meter"><i style="width:${Math.max(0, Math.min(100, Math.round(100 * v / maxValue)))}%"></i></span></td>
      <td>${fmt(p.fppg, 1)}</td>
      ${has("espn") ? `<td class="num-muted">${fmt(p.espn.fppg, 1)}</td>` : ""}
      <td>${fmt(p.games)}</td>
      <td>${p.espn.adp ? fmt(p.espn.adp, 1) : ""}</td>
      <td><span class="${edge >= 12 ? "edge-up" : edge <= -12 ? "edge-down" : ""}">${edge > 0 ? "+" : ""}${edge}</span></td>
      <td>${t ? "" : cell(s && h[0] ? s[0] : null)}</td>
      ${has("s2") ? `<td>${t ? "" : cell(s && h[1] ? s[1] : null)}</td>` : ""}
      <td class="act">${t ? `<span class="by">${esc(team(t.team).abbrev)} #${t.overall}</span>` : btn ? `<button class="${S.pick.mine ? "primary" : ""}" data-draft="${p.id}" title="${S.pick.mine ? "Draft him" : "Record him as taken by " + esc(team(S.pick.team).name)}">${esc(btn)}</button>` : ""}</td>
    </tr>`);
  }
  document.querySelector("#board-table tbody").innerHTML = html.join("") || `<tr><td class="left" colspan="${shown.length + 1}">No players match.</td></tr>`;
  const open = rows.filter((p) => !taken[p.id]);
  $("search-hint").textContent = ui.search ? (open.length === 1 ? "Enter drafts " + open[0].name : open.length + " matches") : "";
}

// ---------- draft grid ----------

function renderGrid() {
  const order = B.league.pick_teams.slice(0, B.league.size);
  const rounds = B.league.pick_teams.length / B.league.size;
  const byOverall = {};
  for (const k of S.picks) byOverall[k.overall] = k;
  let html = '<table><thead><tr><th class="rnd">Rd</th>' + order.map((t) =>
    `<th class="${t === B.league.my_team ? "me" : ""}" title="${esc(team(t).name)}">${esc(team(t).abbrev)}</th>`).join("") + "</tr></thead><tbody>";
  for (let r = 0; r < rounds; r++) {
    html += `<tr><td class="rnd">${r + 1}</td>`;
    for (const t of order) {
      let overall = null;
      for (let i = r * B.league.size; i < (r + 1) * B.league.size; i++) if (B.league.pick_teams[i] === t) overall = i + 1;
      const k = byOverall[overall];
      const name = k ? (P[k.player_id] ? P[k.player_id].name : k.name || "") : "";
      const short = name.replace(/^(\S)\S*\s+(?=\S)/, "$1. ");          // "Jalen Johnson" -> "J. Johnson"
      html += `<td class="${t === B.league.my_team ? "me " : ""}${!S.done && overall === S.pick.overall ? "now" : ""}" title="${esc(name)}"><span class="pk">${overall}</span>${esc(short)}</td>`;
    }
    html += "</tr>";
  }
  $("grid-view").innerHTML = html + "</tbody></table>";
}

// ---------- my roster and the log ----------

function assignSlots(ids) {
  // fill the position-specific slots with as many players as possible, then UT, then the bench
  const slots = [];
  for (const s of SPECIFIC) for (let i = 0; i < (B.league.slots[s] || 0); i++) slots.push(s);
  const owner = {};
  const place = (id, seen) => {
    for (let i = 0; i < slots.length; i++) {
      if (P[id] && P[id].slots.includes(slots[i]) && !seen.has(i)) {
        seen.add(i);
        if (owner[i] == null || place(owner[i], seen)) { owner[i] = id; return true; }
      }
    }
    return false;
  };
  ids.forEach((id) => place(id, new Set()));
  const used = new Set(Object.values(owner));
  const rest = ids.filter((id) => !used.has(id));
  const out = slots.map((s, i) => [s, owner[i]]);
  for (const s of ["UT", "BE"]) for (let i = 0; i < (B.league.slots[s] || 0); i++) out.push([s, rest.shift()]);
  for (const id of rest) out.push(["+", id]);
  return out;
}

function renderRoster() {
  const mine = S.picks.filter((k) => k.team === B.league.my_team).map((k) => k.player_id);
  const rows = assignSlots(mine).map(([s, id]) => {
    const p = P[id];
    return `<div class="slot"><span class="s">${s}</span><span class="n ${p ? "" : "open"}">${p ? esc(p.name) + ` <span class="psub">${esc(p.pos.join("/"))}</span>` : id ? "Player " + id : "open"}</span><span class="v">${p ? fmt(p.fppg, 1) : ""}</span></div>`;
  }).join("");
  const known = mine.map((id) => P[id]).filter(Boolean);
  const hurt = known.filter((p) => p.flags.includes("inj_return") || p.out_until).length;
  const stash = known.filter((p) => p.absent_games > 6).length;
  const centers = known.filter(cOnly).length;
  const notes = [];
  if (S.needs.length && known.length) notes.push("Still open: " + S.needs.join(", ") + ".");
  if (centers >= C_ONLY_LIMIT) notes.push(cCrowd(centers));
  if (hurt >= 2) notes.push(`${hurt} of your ${known.length} played under 50 games last season or are out now; about one in three such players does it again.`);
  if (stash >= (B.league.slots.IR || 0) && stash) notes.push("Both IR slots are spoken for, so another long-term absentee would sit on your bench.");
  $("roster").innerHTML = `<h2>Your roster &middot; ${known.length} of ${B.league.pick_teams.filter((t) => t === B.league.my_team).length}</h2>
    <div class="slots">${rows}</div>${notes.length ? `<div class="side-note">${notes.map(esc).join(" ")}</div>` : ""}`;
}

function renderLog() {
  const rows = S.picks.slice().reverse().slice(0, 40).map((k) =>
    `<div class="log-row ${k.team === B.league.my_team ? "me" : ""}"><span class="k">${k.overall}</span><span class="t" title="${esc(team(k.team).name)}">${esc(team(k.team).abbrev)}</span><span class="n">${esc(P[k.player_id] ? P[k.player_id].name : k.name || "")}</span></div>`).join("");
  $("log").innerHTML = `<h2>Picks</h2><div class="log">${rows || '<div class="empty">No picks yet.</div>'}</div>`;
}

// ---------- player card ----------

function renderDrawer() {
  const el = $("drawer"), p = P[ui.selected];
  if (!p) { el.hidden = true; return; }
  const t = takenMap()[p.id], s = S.survive[p.id], h = S.horizons;
  const status = p.out_until
    ? `<div class="status-line"><span class="dot critical"></span><span>Out. Expected back around <b>${esc(p.out_until)}</b> <span class="num-muted">(${esc(p.out_source || "")})</span></span></div>`
    : p.injury && p.injury !== "ACTIVE" ? `<div class="status-line"><span class="dot warning"></span><span>${esc(p.injury.replace(/_/g, " ").toLowerCase())}${p.injury_report && p.injury_report.comment ? ": " + esc(p.injury_report.comment) : ""}</span></div>` : "";
  const steps = [`<tr><td>ESPN projection</td><td>${fmt(p.espn.fppg, 1)}</td></tr>`]
    .concat((p.steps || []).map(([label, d]) => `<tr><td>${esc(label)}</td><td>${d > 0 ? "+" : ""}${fmt(d, 1)}</td></tr>`))
    .concat([`<tr class="total"><td>Our per-game number</td><td>${fmt(p.fppg, 1)}</td></tr>`]).join("");
  const line = p.espn.line;
  const moves = p.intel && (p.intel.minutes || p.intel.rate);   // confidence only scales these two
  const intel = p.intel ? `<h4>Research note <span class="num-muted">${moves ? "&middot; " + esc(p.intel.confidence || "medium") + " confidence" : ""}${p.intel.updated ? " &middot; " + esc(p.intel.updated) : ""}</span></h4>
    <div class="note">${esc(p.intel.note || "")}
      <div class="num-muted">${[p.intel.applied_minutes ? (p.intel.applied_minutes > 0 ? "+" : "") + fmt(p.intel.applied_minutes, 1) + " minutes" : "", p.intel.applied_rate ? (p.intel.applied_rate > 0 ? "+" : "") + fmt(100 * p.intel.applied_rate, 0) + "% per minute" : "", p.intel.games_rate ? "games x" + p.intel.games_rate : ""].filter(Boolean).join(" &middot; ")}</div>
      <div>${(p.intel.sources || []).map((u, i) => (/^https?:/.test(u) ? `<a href="${esc(u)}" target="_blank" rel="noopener">source ${i + 1}</a>` : `<span class="num-muted">${esc(u)}</span>`)).join(" &middot; ")}</div></div>` : "";
  const news = (p.news || []).map((n) => `<div class="news-item"><div class="when">${esc(n.date)}</div><div class="head">${esc(n.headline)}</div><div class="body">${esc(n.story)}</div></div>`).join("");
  const other = p.adp_other || {};
  el.hidden = false;
  el.innerHTML = `<button class="close quiet" data-close="1" title="Close (Esc)">Close</button>
    <h3>${esc(p.name)}</h3>
    <div class="sub">${meta(p)} &middot; our #${p.rank}, tier ${p.tier} &middot; room #${p.market}</div>
    ${status}
    <div class="actions">${t ? `<span>Drafted by <b>${esc(team(t.team).name)}</b> at #${t.overall}</span>`
      : S.done ? "" : `<button class="${S.pick.mine ? "primary" : ""}" data-draft="${p.id}">${S.pick.mine ? "Draft him" : "Record as taken by " + esc(team(S.pick.team).name)}</button>`}
      ${!t && s && h[0] ? `<span class="num-muted">${pct(s[0])} to be there at #${h[0]}${h[1] ? ", " + pct(s[1]) + " at #" + h[1] : ""}</span>` : ""}</div>
    <div class="tiles">
      <div class="tile"><div class="l">Value</div><div class="v">${fmt(valueOf(p))}</div><div class="d">above a pickup</div></div>
      <div class="tile"><div class="l">Per game</div><div class="v">${fmt(p.fppg, 1)}</div><div class="d">&plusmn;${fmt(p.sigma, 1)} &middot; bar ${fmt(p.bar, 1)}</div></div>
      <div class="tile"><div class="l">Games</div><div class="v">${fmt(p.games)}</div><div class="d">plays ${pct(p.availability)}</div></div>
      <div class="tile"><div class="l">Playoffs</div><div class="v">${fmt(p.games_playoffs, 1)}</div><div class="d">games ${esc(playoffSpan())}</div></div>
    </div>
    <h4>From ESPN's number to ours</h4>
    <table class="kv">${steps}</table>
    ${intel}
    <h4>Projections</h4>
    <table class="kv">
      <tr><td>ESPN</td><td>${fmt(p.espn.fppg, 1)} per game &middot; ${fmt(p.espn.mpg, 1)} min &middot; ${fmt(p.espn.gp)} games</td></tr>
      ${p.fpros ? `<tr><td>FantasyPros</td><td>${fmt(p.fpros.fppg, 1)} per game &middot; ${fmt(p.fpros.mpg, 1)} min &middot; ${fmt(p.fpros.gp)} games</td></tr>` : ""}
      ${p.last ? `<tr><td>Last season</td><td>${fmt(p.last.fppg, 1)} per game &middot; ${fmt(p.last.mpg, 1)} min &middot; ${fmt(p.last.gp)} games</td></tr>` : "<tr><td>Last season</td><td>did not play</td></tr>"}
      <tr><td>ESPN stat line</td><td>${fmt(line.pts, 1)} pts &middot; ${fmt(line.reb, 1)} reb &middot; ${fmt(line.ast, 1)} ast &middot; ${fmt(line.stl, 1)} stl &middot; ${fmt(line.blk, 1)} blk &middot; ${fmt(line.to, 1)} to</td></tr>
      <tr><td>Draft position</td><td>ESPN ${p.espn.adp ? fmt(p.espn.adp, 1) : "n/a"}${other.yahoo ? " &middot; Yahoo " + fmt(other.yahoo, 1) : ""}${other.fantrax ? " &middot; Fantrax " + fmt(other.fantrax, 1) : ""}</td></tr>
    </table>
    ${news ? "<h4>Latest news</h4>" + news : ""}
    ${(p.articles || []).length ? "<h4>Mentioned in</h4>" + p.articles.map(([d, hl]) => `<div class="news-item"><span class="when">${esc(d)}</span> &middot; ${esc(hl)}</div>`).join("") : ""}
    ${p.outlook ? `<h4>ESPN outlook</h4><div class="outlook">${esc(p.outlook)}</div>` : ""}`;
}

// ---------- wiring ----------

function render() {
  renderBar();
  renderRec();
  if (ui.view === "players") renderTable(); else renderGrid();
  renderRoster();
  renderLog();
  renderDrawer();
}

function stamp(s) { return [s.version, s.done, s.sync.state, s.sync.enabled, s.sync.message, (s.order || []).join(","), s.order_confirmed, s.picks.length ? s.picks[s.picks.length - 1].player_id : 0].join("|"); }

async function syncOrder() {
  // the draft order changed under us (ESPN's real order arrived): reload the fixed half of the page
  if (!S.order) return;      // a server from before this field existed: restart it to pick up changes
  if (S.order.join(",") === B.league.pick_teams.slice(0, B.league.size).join(",") && S.order_confirmed === B.league.order_confirmed) return;
  B = await api("/api/board");
  P = {};
  for (const p of B.players) P[p.id] = p;
}

async function poll() {
  if (ui.busy) return;
  try {
    const next = await api("/api/state");
    if (ui.busy) return;
    const changed = !S || stamp(next) !== stamp(S);
    S = next;
    if (changed) { await syncOrder(); render(); }
  } catch (err) { /* server restarting; try again on the next tick */ }
}

document.addEventListener("click", (e) => {
  const d = e.target.closest("[data-draft]");
  if (d) { e.stopPropagation(); draft(Number(d.dataset.draft)); return; }
  if (e.target.closest("[data-close]")) { ui.selected = null; renderDrawer(); renderTable(); return; }
  const th = e.target.closest("th[data-sort]");
  if (th) {
    const col = COLS.find((c) => c.key === th.dataset.sort);
    if (ui.sort === col.key) ui.dir = -ui.dir; else { ui.sort = col.key; ui.dir = col.dir; }
    renderTable();
    return;
  }
  const row = e.target.closest("tr.row, .cand");
  if (row && row.dataset.id) { ui.selected = Number(row.dataset.id); renderDrawer(); if (ui.view === "players") renderTable(); return; }
  const pos = e.target.closest("#pos-filter button");
  if (pos) {
    ui.pos = pos.dataset.pos;
    document.querySelectorAll("#pos-filter button").forEach((b) => b.classList.toggle("on", b === pos));
    renderTable();
    return;
  }
  const tab = e.target.closest(".tab");
  if (tab) {
    ui.view = tab.dataset.view;
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("on", b === tab));
    $("players-view").hidden = ui.view !== "players";
    $("grid-view").hidden = ui.view !== "grid";
    for (const id of ["search", "pos-filter", "search-hint"]) $(id).hidden = ui.view !== "players";
    document.querySelector(".check").hidden = ui.view !== "players";
    render();
    return;
  }
  if (!e.target.closest("#drawer") && ui.selected && !e.target.closest("button, input, label")) { ui.selected = null; renderDrawer(); if (ui.view === "players") renderTable(); }
});

$("search").addEventListener("input", (e) => { ui.search = e.target.value.trim(); renderTable(); });
$("search").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const { rows, taken } = visiblePlayers();
  const open = rows.filter((p) => !taken[p.id]);
  if (open.length === 1 && !S.done) draft(open[0].id);
  else if (open.length) { ui.selected = open[0].id; renderDrawer(); renderTable(); }
});
narrow.addEventListener("change", () => { if (S && ui.view === "players") renderTable(); });
$("show-taken").addEventListener("change", (e) => { ui.showTaken = e.target.checked; renderTable(); });
$("btn-undo").addEventListener("click", () => act("/api/undo"));
$("btn-mock-one").addEventListener("click", () => act("/api/mock", { until_me: false }));
$("btn-mock-me").addEventListener("click", () => act("/api/mock", { until_me: true }));
$("btn-reset").addEventListener("click", () => {
  if (!ui.resetArmed) { ui.resetArmed = true; renderBar(); setTimeout(() => { ui.resetArmed = false; if (S) renderBar(); }, 4000); return; }
  ui.resetArmed = false;
  act("/api/reset");
});
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== $("search")) { e.preventDefault(); $("search").focus(); $("search").select(); }
  if (e.key === "Escape") {
    if (ui.selected) { ui.selected = null; renderDrawer(); renderTable(); }
    else if (ui.search) { ui.search = ""; $("search").value = ""; renderTable(); }
    $("search").blur();
  }
});

(async function init() {
  B = await api("/api/board");
  for (const p of B.players) P[p.id] = p;
  S = await api("/api/state");
  document.title = B.league.name + " draft board";
  render();
  setInterval(poll, 1500);
})();
