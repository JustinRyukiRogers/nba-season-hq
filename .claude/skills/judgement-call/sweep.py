"""Every outstanding Season HQ suggestion, with the context the model cannot read, for a judgment call.

Reads site/data.json (run `python3 -m seasonkit refresh` first), cache/news.json and data/takes.json.
For each suggestion it prints the players involved: the HQ's number and how it is built, his last
games' minutes, his depth-chart spot, teammates who are out or day-to-day (who he may be covering
for, and when they return), injury report, ownership trend, latest news, and any take already on him.

    python3 .claude/skills/judgement-call/sweep.py
"""
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
d = json.loads((ROOT / "site" / "data.json").read_text())
news = json.loads((ROOT / "cache" / "news.json").read_text()) if (ROOT / "cache" / "news.json").exists() else {}
takes_path = ROOT / "data" / "takes.json"
takes = (json.loads(takes_path.read_text()).get("takes") or []) if takes_path.exists() else []
P = d["players"]
log = d.get("gamelog") or {}
start = date.fromisoformat(d["calendar"]["start"])
today = d["calendar"]["today"]
me = d["league"]["me"]
seen = set()


def name(pid):
    return (P.get(str(pid)) or {}).get("name", str(pid))


def owner(p):
    if p["owner"] == me:
        return "yours"
    if p["owner"]:
        return d["league"]["teams"][str(p["owner"])]["abbrev"]
    return "FA" if p["fa"] != "WAIVERS" else "waivers"


def context(pid, label=""):
    p = P.get(str(pid))
    if not p:
        print("    %s %s: not in the pool" % (label, pid))
        return
    r = p["ros"]
    head = "    %s%s (%s %s, %s) %s" % (label + " " if label else "", p["name"], p["team"], "/".join(p["pos"]), owner(p),
                                       p["status"].replace("_", " ").lower())
    print(head)
    if str(pid) in seen:
        print("      (context above)")
        return
    seen.add(str(pid))
    built = ("%.1f min x %.2f" % (r["mpg"], r["rate"])) if r.get("mpg") else "points-per-game blend"
    print("      HQ: %.1f a game (%s; preseason %.1f at %s min), value %.0f, %.1f games left; %.1f%% owned (%+.1f today)" % (
        r["fppg"], built, r["prior"], p.get("proj_mpg") or "?", r.get("value") or 0, r["games"], p["own"], p["own_chg"]))
    games = (log.get(str(pid)) or [])[-6:]
    if games:
        print("      last games (date min/pts): " + ", ".join(
            "%s %.0f/%.0f" % ((start.fromordinal(start.toordinal() + g[0])).strftime("%-m/%-d"), g[2], g[1]) for g in games))
    dep = p.get("depth")
    if dep:
        print("      depth: %s #%d%s" % (dep["pos"], dep["rank"], (" (#%d now; ahead of him out: %s)" % (dep["now"], ", ".join(dep["out"])))
                                        if dep.get("out") else ""))
    mates = [q for q in P.values() if q["team"] == p["team"] and q["id"] != p["id"] and q["status"] != "ACTIVE"
             and ((q.get("proj_mpg") or 0) >= 15 or ((q["stats"].get("season") or [0, 0])[1] or 0) >= 15)]
    if mates:
        print("      teammates not active: " + "; ".join(
            "%s %s%s (%s min)" % (q["name"], q["status"].replace("_", " ").lower(),
                                   ", back " + q["inj"]["return"] if q.get("inj") and q["inj"].get("return") else "",
                                   q.get("proj_mpg") or "?") for q in mates))
    if p.get("inj"):
        i = p["inj"]
        print("      injury: %s %s %s, back %s: %s" % (i.get("status"), i.get("type") or "", i.get("detail") or "",
                                                       i.get("return") or "?", (i.get("short") or "")[:200]))
    items = sorted((news.get(str(pid)) or {}).get("items") or [], key=lambda x: x.get("at") or "", reverse=True)[:3]
    for it in items:
        print("      news %s: %s" % ((it.get("at") or "?")[:16].replace("T", " "), it["headline"][:220]))
    for t in takes:
        if p["name"] in [t["player"]] + list(t.get("also") or []):
            print("      TAKE (%s, written %s, until %s): %s" % (t["stance"], t["written"], t.get("until", "+7d"), t["text"][:160]))


def section(title):
    print("\n" + "=" * 100 + "\n" + title)


section("TO-DOS (the Today tab)")
for a in d["actions"]:
    print("\n  [%s] %s" % (a["urgency"], a["title"]))
    if a.get("why"):
        print("    why: " + a["why"])
    for key, label in (("player", "add/subject"), ("other", "activate"), ("drop", "drop")):
        if a.get(key):
            context(a[key], label)
    for key in ("give", "get", "start", "sit"):
        for pid in a.get(key) or []:
            context(pid, key)

section("UPGRADES (season-long adds over %s)" % name(d["upgrades"]["drop"]))
for u in d["upgrades"]["list"][:3]:
    print("\n  +%.0f value" % u["gain"])
    context(u["id"], "add")
context(d["upgrades"]["drop"], "drop") if d["upgrades"]["list"] else None

section("IR STASH (top 3)")
for s in d["stash"]["list"][:3]:
    print("\n  back %s, %.1f games after" % (s["back"], s["games_after"]))
    context(s["id"], "stash")

section("STREAMS AND HUNCHES (next stream day %s)" % (d["streams"]["days"][:1] or ["?"])[0])
for kind in ("streams", "hunches"):
    for s in d[kind]["list"][:3]:
        print("\n  %s: net %+.1f (that day %+.1f, season cost %+.1f)%s" % (
            {"streams": "stream", "hunches": "hunch"}[kind], s["net"], s["gain"], s["ros_cost"], " signs: " + "; ".join(s.get("signs") or []) if s.get("signs") else ""))
        context(s["id"], "add")
        if s.get("drop"):
            context(s["drop"], "drop")

section("WEEK PLAN MOVES")
for m in d["plan"]["moves"]:
    print("\n  %s %s: %s" % (m.get("day"), m["kind"], name(m["id"])) + (" for " + name(m["drop"]) if m.get("drop") else ""))
    context(m["id"], "add")

section("TRADE IDEAS (top 2)")
for t in d["trades"]["list"][:2]:
    print("\n  with %s: give %s, get %s: +%d season points here, them %+d on ESPN" % (
        d["league"]["teams"][str(t["team"])]["abbrev"], " + ".join(map(name, t["give"])), " + ".join(map(name, t["get"])),
        t["gain"], t["their_espn"]))
    for pid in t["give"] + t["get"]:
        context(pid, "give" if pid in t["give"] else "get")

section("LEAGUE MOVES (trades, claims, pickups; judged on today's numbers; rosters already reflect them)")
L = d.get("league_moves") or {"list": [], "unmatched": []}
if L["unmatched"]:
    print("  NOT YET ON NEW ROSTERS: " + ", ".join(map(name, L["unmatched"])))
recent = [t for t in L["list"] if t["kind"] == "trade"][:5] + [t for t in L["list"] if t["kind"] != "trade"][:8]
for t in recent:
    print("\n  %s %s" % (t["kind"].upper(), " <-> ".join(d["league"]["teams"][str(sd["team"])]["abbrev"] for sd in t["sides"])))
    for sd in t["sides"]:
        print("    %s got %s, gave %s: %s season lineup points, value %+d, ESPN %+d%s" % (
            d["league"]["teams"][str(sd["team"])]["abbrev"], " + ".join(map(name, sd["got"])) or "-",
            " + ".join(map(name, sd["gave"])) or "-", "%+d" % sd["gain"] if sd["gain"] is not None else "?",
            sd["value"], sd["espn"], ", plays you week %d" % sd["meets_me"] if sd.get("meets_me") else ""))
    if t["kind"] == "trade":
        for m in t["moves"]:
            context(m["id"], "moved")
if not L["list"]:
    print("  none this season")

section("EXISTING TAKES")
for t in takes:
    until = t.get("until") or "(written + 7 days)"
    state = "EXPIRED" if t.get("until") and t["until"] < today else "live"
    print("  [%s, %s until %s] %s: %s" % (state, t["stance"], until, t["player"], t["text"][:120]))
