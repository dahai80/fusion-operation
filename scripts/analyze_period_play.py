import json
import os
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATS = os.path.join(ROOT, "out", "ops", "stats_history.jsonl")
PUB = os.path.join(ROOT, "out", "queue", "published")

def date_of(snap):
    return snap.get("snapshot_at", "")[:10]

def create_date(item):
    ca = item.get("created_at")
    if not ca:
        return ""
    import datetime
    try:
        return datetime.datetime.fromtimestamp(int(ca)).strftime("%Y-%m-%d")
    except Exception:
        return ""

snaps = []
with open(STATS, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            snaps.append(json.loads(line))
        except Exception:
            pass

published = []
for fn in os.listdir(PUB):
    p = os.path.join(PUB, fn)
    try:
        with open(p, encoding="utf-8") as f:
            published.append(json.load(f))
    except Exception:
        pass

# vid -> latest snapshot (max plays, latest snapshot_at)
latest = {}
for s in snaps:
    vid = s.get("vid", "")
    if not vid:
        continue
    cur = latest.get(vid)
    if cur is None or (s.get("snapshot_at", "") >= cur.get("snapshot_at", "")):
        latest[vid] = s

print("=== TOTAL SCOPE ===")
print(f"stats_history lines: {len(snaps)}")
print(f"unique vid in stats: {len(latest)}")
print(f"published items: {len(published)}")
print(f"snapshot_at range: {min(s.get('snapshot_at','') for s in snaps)} ~ {max(s.get('snapshot_at','') for s in snaps)}")

# published by create date
pub_by_date = defaultdict(list)
for it in published:
    pub_by_date[create_date(it)].append(it)

print("\n=== PUBLISHED PER DAY (all time) ===")
for d in sorted(pub_by_date):
    print(f"  {d}: {len(pub_by_date[d])}")

# vid -> published date
vid_pubdate = {}
for it in published:
    v = it.get("vid", "")
    if v:
        vid_pubdate[v] = create_date(it)

# weekly periods: 08-22~08-28 (W1), 08-29~09-04 (W2), 09-05~09-07 (W3 partial)
periods = [("W1 08-22~08-28", "2026-08-22", "2026-08-28"),
           ("W2 08-29~09-04", "2026-08-29", "2026-09-04"),
           ("W3 09-05~09-07", "2026-09-05", "2026-09-07")]

print("\n=== PERIOD SPLIT (published in window, latest plays) ===")
print(f"{'period':<18} {'pub':>4} {'>=1000':>7} {'>=500':>6} {'>=100':>6} {'>0':>5} {'=0':>5} {'max':>6} {'avg':>8} {'sum':>7}")
for label, lo, hi in periods:
    vids_in = [v for v, d in vid_pubdate.items() if lo <= d <= hi]
    plays = []
    for v in vids_in:
        s = latest.get(v)
        if s:
            plays.append(int(s.get("plays", 0) or 0))
        else:
            plays.append(0)
    n = len(plays)
    if n == 0:
        print(f"  {label}: no published in window")
        continue
    mx = max(plays)
    avg = sum(plays) / n
    ge1000 = sum(1 for p in plays if p >= 1000)
    ge500 = sum(1 for p in plays if p >= 500)
    ge100 = sum(1 for p in plays if p >= 100)
    gt0 = sum(1 for p in plays if p > 0)
    eq0 = sum(1 for p in plays if p == 0)
    print(f"  {label:<18} {n:>4} {ge1000:>7} {ge500:>6} {ge100:>6} {gt0:>5} {eq0:>5} {mx:>6} {avg:>8.1f} {sum(plays):>7}")

# zero-play ratio trend
print("\n=== ZERO-PLAY RATIO TREND ===")
for label, lo, hi in periods:
    vids_in = [v for v, d in vid_pubdate.items() if lo <= d <= hi]
    n = len(vids_in)
    if n == 0:
        continue
    z = 0
    for v in vids_in:
        s = latest.get(v)
        p = int(s.get("plays", 0) or 0) if s else 0
        if p == 0:
            z += 1
    print(f"  {label}: {z}/{n} = {z/n*100:.1f}% zero")

# vid -> hook_variant from published item
vid_variant = {}
for it in published:
    v = it.get("vid", "")
    if v:
        vid_variant[v] = it.get("hook_variant") or it.get("variant") or "?"

# top 10 across window 08-22~09-07
print("\n=== TOP 10 PLAYS (08-22~09-07 published) ===")
window_vids = [v for v, d in vid_pubdate.items() if "2026-08-22" <= d <= "2026-09-07"]
rows = []
for v in window_vids:
    s = latest.get(v)
    if not s:
        continue
    rows.append((int(s.get("plays", 0) or 0), s.get("title", "")[:40], vid_variant.get(v, "?"), v))
rows.sort(reverse=True)
for p, t, var, v in rows[:10]:
    print(f"  plays={p:<6} variant={var:<3} {t}")

# variant aggregate in window
print("\n=== VARIANT AGG (08-22~09-07, latest plays) ===")
varagg = defaultdict(lambda: {"n": 0, "plays": [], "max": 0})
for v in window_vids:
    s = latest.get(v)
    if not s:
        continue
    var = vid_variant.get(v, "?")
    p = int(s.get("plays", 0) or 0)
    varagg[var]["n"] += 1
    varagg[var]["plays"].append(p)
    if p > varagg[var]["max"]:
        varagg[var]["max"] = p
for var in sorted(varagg):
    a = varagg[var]
    avg = sum(a["plays"]) / a["n"] if a["n"] else 0
    print(f"  variant {var}: n={a['n']} max={a['max']} avg={avg:.1f} sum={sum(a['plays'])}")
