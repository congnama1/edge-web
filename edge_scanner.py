#!/usr/bin/env python3
"""
FanDuel vs PrizePicks edge scanner.

Flags PrizePicks "standard" props (even payout, no demon/goblin) where FanDuel's
no-vig probability says one side hits more than THRESHOLD (default 54%).

Two kinds of flags:
  EXACT  - same line on both books, FanDuel de-vigged prob > threshold.
  SOFTER - PrizePicks line is easier than FanDuel's (e.g. PP over 24.5 vs FD 26.5).
           Probability at the PP line is >= FD's prob at FD's line, so if FD's
           prob is already over threshold, the true prob here is at least that.

Setup:
  pip install requests
  export ODDS_API_KEY=your_key      # free tier at https://the-odds-api.com
Usage:
  python edge_scanner.py --sport nba
  python edge_scanner.py --sport nfl --threshold 0.55 --loop 300
  python edge_scanner.py --sport nba --pp-file pp.json   # if PrizePicks blocks requests

Note: PrizePicks' endpoint often sits behind bot protection. If you get a 403,
save the JSON from https://api.prizepicks.com/projections?league_id=7&per_page=250
in your browser and pass it with --pp-file.
"""
import argparse, json, os, re, sys, time
import requests

ODDS_KEY = os.environ.get("ODDS_API_KEY", "")
ERRORS = []      # odds api failures, for the debug page
REMAINING = {}   # odds api credits left

# sport -> (Odds API sport key, PrizePicks league id, {odds api market: PP stat name})
SPORTS = {
    "nba": ("basketball_nba", 7, {
        "player_points": "Points", "player_rebounds": "Rebounds",
        "player_assists": "Assists", "player_threes": "3-PT Made",
        "player_points_rebounds_assists": "Pts+Rebs+Asts"}),
    "nfl": ("americanfootball_nfl", 9, {
        "player_pass_yds": "Pass Yards", "player_rush_yds": "Rush Yards",
        "player_reception_yds": "Receiving Yards", "player_receptions": "Receptions",
        "player_pass_tds": "Pass TDs"}),
    "mlb": ("baseball_mlb", 2, {
        "pitcher_strikeouts": "Pitcher Strikeouts", "batter_hits": "Hits",
        "batter_total_bases": "Total Bases"}),
    "nhl": ("icehockey_nhl", 8, {
        "player_points": "Points", "player_assists": "Assists",
        "player_shots_on_goal": "Shots On Goal"}),
}


def norm(name):
    name = re.sub(r"[^a-z ]", "", name.lower())
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", name)
    return " ".join(name.split())


def implied(american):
    return 100 / (american + 100) if american > 0 else -american / (-american + 100)


def devig(over_price, under_price):
    """Proportional no-vig probabilities (over, under)."""
    po, pu = implied(over_price), implied(under_price)
    return po / (po + pu), pu / (po + pu)


def get_fanduel(sport):
    key, _, markets = SPORTS[sport]
    base = "https://api.the-odds-api.com/v4/sports"
    events = requests.get(f"{base}/{key}/events", params={"apiKey": ODDS_KEY}, timeout=20)
    events.raise_for_status()
    REMAINING["credits_left"] = events.headers.get("x-requests-remaining")
    ERRORS.clear()
    out = {}  # (player, stat) -> list of (line, p_over, p_under)
    for ev in events.json():
        r = requests.get(f"{base}/{key}/events/{ev['id']}/odds", params={
            "apiKey": ODDS_KEY, "regions": "us", "bookmakers": "fanduel",
            "markets": ",".join(markets), "oddsFormat": "american"}, timeout=20)
        REMAINING["credits_left"] = r.headers.get("x-requests-remaining", REMAINING.get("credits_left"))
        if r.status_code != 200:
            ERRORS.append(f"{r.status_code}: {r.text[:150]}")
            continue
        for bk in r.json().get("bookmakers", []):
            for mk in bk.get("markets", []):
                stat = markets.get(mk["key"])
                by = {}
                for o in mk["outcomes"]:
                    by.setdefault((norm(o["description"]), o["point"]), {})[o["name"]] = o["price"]
                for (player, line), sides in by.items():
                    if "Over" in sides and "Under" in sides:
                        po, pu = devig(sides["Over"], sides["Under"])
                        out.setdefault((player, stat), []).append((line, po, pu))
    return out


def get_prizepicks(sport, pp_file=None):
    if pp_file:
        data = json.load(open(pp_file))
    else:
        r = requests.get("https://api.prizepicks.com/projections",
                         params={"league_id": SPORTS[sport][1], "per_page": 250, "single_stat": "true"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if r.status_code != 200:
            sys.exit(f"PrizePicks returned {r.status_code}. Save the JSON manually and use --pp-file.")
        data = r.json()
    names = {i["id"]: i["attributes"]["name"] for i in data.get("included", [])
             if i["type"] == "new_player"}
    props = []
    for p in data["data"]:
        a = p["attributes"]
        if a.get("odds_type", "standard") != "standard":  # skip demons/goblins
            continue
        pid = p["relationships"]["new_player"]["data"]["id"]
        if pid in names:
            props.append((norm(names[pid]), a["stat_type"], float(a["line_score"]), names[pid]))
    return props


def scan(sport, threshold, pp_file, fd=None):
    if fd is None:
        fd = get_fanduel(sport)
    flags = []
    for player, stat, pp_line, display in get_prizepicks(sport, pp_file):
        for fd_line, po, pu in fd.get((player, stat), []):
            if pp_line == fd_line:
                kind = "EXACT"
            elif pp_line < fd_line:
                kind = "SOFTER"  # only the OVER is helped by a lower line
                pu = 0
            else:
                kind = "SOFTER"  # only the UNDER is helped by a higher line
                po = 0
            for side, p in (("OVER", po), ("UNDER", pu)):
                if p > threshold:
                    flags.append((p, kind, display, stat, side, pp_line, fd_line))
    return sorted(flags, reverse=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", choices=SPORTS, default="nba")
    ap.add_argument("--threshold", type=float, default=0.54)
    ap.add_argument("--pp-file")
    ap.add_argument("--loop", type=int, default=0, help="rescan every N seconds")
    args = ap.parse_args()
    if not ODDS_KEY:
        sys.exit("Set ODDS_API_KEY first (https://the-odds-api.com).")
    while True:
        flags = scan(args.sport, args.threshold, args.pp_file)
        print(f"\n[{time.strftime('%H:%M:%S')}] {len(flags)} flagged (> {args.threshold:.0%})")
        for p, kind, name, stat, side, ppl, fdl in flags:
            print(f"  {p:5.1%}  {kind:6}  {name:22} {stat:18} {side:5} PP {ppl}  (FD {fdl})")
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
