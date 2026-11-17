"""P4 experiment 4, step 1 (offline): would subagentPromptCacheTtl=1h pay off?

Replays subagent transcripts under ~/.claude/projects/*bench*: extra cost of writing every 5m
subagent cache write at 1h vs the most that avoided re-writes could save. No model calls.
Run: python3 bench/exp4_ttl_replay.py
"""
import json, sys, glob, os
from pathlib import Path
from collections import defaultdict
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench
P = bench.load_prices()
def fam(model):
    k = next((k for k in P if k in model), None); return k
def reqs(path):
    last = {}
    for l in path.read_text(errors="replace").splitlines():
        try: o = json.loads(l)
        except ValueError: continue
        m = o.get("message")
        if isinstance(m, dict) and isinstance(m.get("usage"), dict) and m.get("id"):
            last[(m["id"], o.get("requestId"))] = (o.get("timestamp"), m.get("model") or "?", m["usage"])
    out = []
    for ts, model, u in last.values():
        cc = u.get("cache_creation") or {}
        out.append(dict(t=bench.iso(ts).timestamp() if hasattr(bench.iso(ts),'timestamp') else bench.iso(ts),
                        model=model, w5=cc.get("ephemeral_5m_input_tokens") or 0,
                        w1=cc.get("ephemeral_1h_input_tokens") or 0,
                        rd=u.get("cache_read_input_tokens") or 0))
    out.sort(key=lambda r: r["t"]); return out
sess = []
for d in glob.glob(os.path.expanduser("~/.claude/projects/*bench*")):
    for f in Path(d).glob("*/subagents/agent-*.jsonl"):
        r = reqs(f)
        if not r: continue
        meta = bench.jload(f.with_suffix(".meta.json"), {}) or {}
        sess.append(dict(f=f, proj=Path(d).name, typ=meta.get("agentType", "?"), r=r))
print("subagent sessions", len(sess))
W5 = sum(x["w5"] for s in sess for x in s["r"]); W1 = sum(x["w1"] for s in sess for x in s["r"])
print(f"subagent writes: 5m {W5:,}  1h {W1:,}")
extra = save_in = 0.0; per_model = defaultdict(lambda: [0, 0])
for s in sess:
    for x in s["r"]:
        p = P[fam(x["model"])]; extra += x["w5"] * (p["cache_write_1h"] - p["cache_write_5m"]) / 1e6
        per_model[fam(x["model"])][0] += x["w5"]
print("extra cost if every 5m subagent write were 1h: $%.2f" % extra, {k: v[0] for k, v in per_model.items()})
# A) within-session gaps over 5m (expiry-induced rewrites; bound = that request's w5)
gaps = [(s["typ"], b["t"] - a["t"], b["w5"], b["model"]) for s in sess for a, b in zip(s["r"], s["r"][1:]) if b["t"] - a["t"] > 300]
sav_a = sum(w * (P[fam(m)]["cache_write_5m"] - P[fam(m)]["cache_read"]) / 1e6 for _, g, w, m in gaps if g <= 3600)
print(f"A) within-session gaps >5m: {len(gaps)}; max possible saving ${sav_a:.2f}")
# B) cross-session prefix reuse: first request of a session whose same-key sibling ended 5m..60m earlier
def bound(keyf):
    by = defaultdict(list)
    for s in sess: by[keyf(s)].append(s)
    n = 0; sav = 0.0
    for k, ss in by.items():
        ss.sort(key=lambda s: s["r"][0]["t"])
        for i, s in enumerate(ss):
            f = s["r"][0]; prev = [(p["r"][0]["w5"] + p["r"][0]["w1"] + p["r"][0]["rd"], p["r"][-1]["t"]) for p in ss[:i] if 300 < f["t"] - p["r"][-1]["t"] <= 3600]
            # and not already covered by a <=5m sibling
            if not prev or any(f["t"] - p["r"][-1]["t"] <= 300 for p in ss[:i]): continue
            shared = min(f["w5"], max(pv for pv, _ in prev)); n += 1
            pr = P[fam(f["model"])]; sav += shared * (pr["cache_write_5m"] - pr["cache_read"]) / 1e6
    return n, sav
for name, kf in (("same type+project", lambda s: (s["typ"], s["proj"])), ("same type only (loose upper bound)", lambda s: s["typ"])):
    n, sv = bound(kf); print(f"B) {name}: {n} first requests could hit a 1h entry; max saving ${sv:.2f}")
