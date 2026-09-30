import os, time, threading, json
from flask import Flask, jsonify, request, Response
import edge_scanner as es

app = Flask(__name__)
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "300"))  # protects your API quota
PP_FILE = os.environ.get("PP_FILE") or None
_cache, _lock = {}, threading.Lock()


def cached_scan(sport):
    with _lock:
        hit = _cache.get(sport)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit
        up = f"/tmp/pp_{sport}.json"
        pp = up if os.path.exists(up) else PP_FILE
        flags = es.scan(sport, 0.50, pp)  # cache wide, filter per request
        _cache[sport] = (time.time(), flags)
        return _cache[sport]


@app.route("/api/pp", methods=["POST"])
def api_pp():
    sport = request.args.get("sport", "nba")
    if sport not in es.SPORTS:
        return jsonify(error="bad sport"), 400
    try:
        d = json.loads(request.get_data(as_text=True))
        assert "data" in d
    except Exception:
        return jsonify(error="That doesn't look like PrizePicks JSON. Copy the whole page."), 400
    with open(f"/tmp/pp_{sport}.json", "w") as f:
        json.dump(d, f)
    _cache.pop(sport, None)
    return jsonify(ok=True)


@app.route("/api/scan")
def api_scan():
    sport = request.args.get("sport", "nba")
    if sport not in es.SPORTS:
        return jsonify(error="bad sport"), 400
    thr = float(request.args.get("threshold", 0.54))
    try:
        ts, flags = cached_scan(sport)
    except SystemExit as e:
        return jsonify(error=str(e)), 502
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 502
    rows = [dict(prob=round(p, 4), kind=k, player=n, stat=s, side=sd, pp_line=pl, fd_line=fl)
            for p, k, n, s, sd, pl, fl in flags if p > thr]
    return jsonify(updated=ts, count=len(rows), rows=rows)


PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Edge Scanner</title>
<style>
body{font-family:system-ui;margin:0;padding:12px;background:#0f1115;color:#e8e8e8}
select,input,button{font-size:16px;padding:8px;border-radius:8px;border:1px solid #333;background:#1b1e25;color:#fff}
table{width:100%;border-collapse:collapse;margin-top:12px;font-size:14px}
td,th{padding:6px 4px;border-bottom:1px solid #262a33;text-align:left}
.p{font-weight:700;color:#4ade80}.SOFTER{color:#fbbf24}.msg{margin-top:10px;color:#999}
.wrap{overflow-x:auto}
</style>
<h2>FanDuel vs PrizePicks</h2>
<select id=sport><option>nba<option>nfl<option>mlb<option>nhl</select>
<input id=thr type=number step=0.01 value=0.54 style=width:80px>
<button onclick=load()>Scan</button>
<div class=msg id=msg></div>
<details style="margin-top:10px"><summary>PrizePicks blocked? Paste data manually</summary>
<p class=msg>1) Open <a id=ppl style="color:#7dd3fc" target=_blank>this link</a> in your browser.<br>
2) Select all text, copy it, paste below, tap Save.</p>
<textarea id=pp rows=4 style="width:100%;background:#1b1e25;color:#fff;border-radius:8px"></textarea>
<button onclick=savepp()>Save PrizePicks data</button></details>
<div class=wrap><table><thead><tr><th>Prob<th>Player<th>Stat<th>Pick<th>PP<th>FD<th>Type</tr></thead><tbody id=body></tbody></table></div>
<script>
async function load(){
  msg.textContent='Scanning...';
  const r=await fetch(`/api/scan?sport=${sport.value}&threshold=${thr.value}`);
  const d=await r.json();
  if(d.error){msg.textContent='Error: '+d.error;body.innerHTML='';return}
  msg.textContent=`${d.count} flagged - updated ${new Date(d.updated*1000).toLocaleTimeString()}`;
  body.innerHTML=d.rows.map(x=>`<tr><td class=p>${(x.prob*100).toFixed(1)}%<td>${x.player}<td>${x.stat}<td>${x.side}<td>${x.pp_line}<td>${x.fd_line}<td class=${x.kind}>${x.kind}</tr>`).join('');
}
const LG={nba:7,nfl:9,mlb:2,nhl:8};
function link(){ppl.href=`https://api.prizepicks.com/projections?league_id=${LG[sport.value]}&per_page=250&single_stat=true`}
sport.onchange=link; link();
async function savepp(){
  const r=await fetch(`/api/pp?sport=${sport.value}`,{method:'POST',body:pp.value});
  const d=await r.json();
  if(d.error){msg.textContent='Error: '+d.error;return}
  pp.value='';load();
}
load(); setInterval(load,300000);
</script>"""


@app.route("/")
def home():
    return Response(PAGE, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
