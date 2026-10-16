"""Non-ESPN sources: FantasyPros consensus projections, Hashtag Basketball's cross-platform ADP,
and Basketball Reference season tables (age and team history). All parsed with the standard library."""
import html
import re
import time
import unicodedata

from .config import HISTORY, team_abbrev, write_json
from .espn import http_get

TAG = re.compile(r"<[^>]+>")
ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)


def norm_name(name):
    """Join key across sources: ascii, lowercase, no punctuation or generational suffix."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().replace("-", " ")
    n = re.sub(r"[.'`’]", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def text(fragment):
    return html.unescape(TAG.sub("", fragment)).strip()


def number(value):
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


# ---------- FantasyPros ----------

def fetch_fantasypros():
    return http_get("https://www.fantasypros.com/nba/projections/overall.php", timeout=60).decode("utf-8", "ignore")


def parse_fantasypros(page):
    """-> {name_key: {name, team, pts, reb, ast, blk, stl, fg_pct, ft_pct, tpm, gp, min, to}} (season totals)."""
    table = re.search(r'<table[^>]*id="data"[^>]*>(.*?)</table>', page, re.S)
    if not table:
        return {}
    heads = [text(h).lower() for h in re.findall(r"<th[^>]*>(.*?)</th>", table.group(1), re.S)]
    rename = {"fg%": "fg_pct", "ft%": "ft_pct", "3pm": "tpm"}
    cols = [rename.get(h, h) for h in heads]
    out = {}
    for row in ROW.findall(table.group(1)):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) != len(cols):
            continue
        name = re.search(r'fp-player-name="([^"]+)"', cells[0])
        team = re.search(r"\(([A-Z]{2,4})\s*-", text(cells[0]))
        if not name:
            continue
        rec = {c: number(text(v)) for c, v in zip(cols[1:], cells[1:])}
        if not rec.get("gp"):
            continue
        rec.update(name=html.unescape(name.group(1)), team=team_abbrev(team.group(1)) if team else None)
        out[norm_name(rec["name"])] = rec
    return out


# ---------- Hashtag Basketball ADP ----------

def fetch_hashtag_adp():
    return http_get("https://hashtagbasketball.com/fantasy-basketball-adp", timeout=60).decode("utf-8", "ignore")


def parse_hashtag_adp(page):
    """-> {name_key: {name, yahoo, espn, fantrax, blend}} average draft position per platform."""
    out = {}
    for row in ROW.findall(page):
        name = re.search(r"<td>\s*<a[^>]*>([^<]+)</a>", row)
        if not name:
            continue
        rec = {"name": html.unescape(name.group(1)).strip()}
        for key, col in (("yahoo", "y"), ("espn", "e"), ("fantrax", "f"), ("blend", "b")):
            cell = re.search(r'<td class="adp-col-%s">([^<]*)</td>' % col, row)
            rec[key] = number(cell.group(1)) if cell else None
        if any(rec[k] for k in ("yahoo", "espn", "fantrax")):
            out[norm_name(rec["name"])] = rec
    return out


# ---------- Basketball Reference ----------

def fetch_bref(years, pause=4):
    """Download season totals pages into data/history/raw_bref (skipping ones already there)."""
    raw = HISTORY / "raw_bref"
    raw.mkdir(parents=True, exist_ok=True)
    for year in years:
        path = raw / ("NBA_%d_totals.html" % year)
        if not path.exists():
            path.write_bytes(http_get("https://www.basketball-reference.com/leagues/NBA_%d_totals.html" % year,
                                      timeout=60))
            time.sleep(pause)  # bref rate-limits at about 20 requests a minute
    return parse_bref()


def parse_bref():
    """-> data/history/bref.json: {season: {name_key: {name, age, pos, g, mp, teams: [{team, g, mp, pts}]}}}.
    A traded player has one summary row (2TM/TOT) plus a row per team; `teams` keeps the per-team rows."""
    cell = re.compile(r'<t[dh][^>]*data-stat="([^"]+)"[^>]*>(.*?)</t[dh]>', re.S)
    out = {}
    for path in sorted((HISTORY / "raw_bref").glob("NBA_*_totals.html")):
        year = re.search(r"NBA_(\d{4})_", path.name).group(1)
        page = path.read_bytes().decode("utf-8", "ignore")
        table = re.search(r'<table[^>]*id="totals_stats"[^>]*>(.*?)</table>', page, re.S)
        season = {}
        for row in ROW.findall(table.group(1) if table else ""):
            c = {k: text(v) for k, v in cell.findall(row)}
            name = c.get("name_display") or c.get("player")
            team = c.get("team_name_abbr") or c.get("team_id")
            if not name or name == "Player" or not team:
                continue
            games = number(c.get("games") or c.get("g") or "")
            line = {"team": team, "g": games, "mp": number(c.get("mp", "")), "pts": number(c.get("pts", ""))}
            rec = season.setdefault(norm_name(name), {"name": name, "age": number(c.get("age", "")),
                                                      "pos": c.get("pos"), "g": games, "mp": line["mp"], "teams": []})
            if not re.match(r"^\dTM$|^TOT$", team):
                rec["teams"].append(line)
        out[year] = season
    write_json(HISTORY / "bref.json", out)
    return out
