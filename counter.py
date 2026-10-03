#!/usr/bin/env python3
"""
claude-code-home-counter -- build your own usage stats board for the Claude Code home page,
entirely from the transcripts already on your machine. Nothing is uploaded anywhere.

Usage:
  python counter.py              build board.html + stats-data.js next to this script
  python counter.py --out DIR    write the two files into DIR instead
  python counter.py --watch 60   keep rebuilding every 60 seconds (the board auto-refreshes)

It reads ~/.claude/projects (your local Claude Code sessions), counts sessions, messages,
tokens, tool calls, cost and concurrency, and renders a single self-contained HTML card you
can open in any browser. Dollar figures use the public list prices below; the board can show
them in your own currency.
"""
import os, sys, json, time, datetime, collections, hashlib, argparse

ROOT = os.path.expanduser(os.path.join("~", ".claude", "projects"))
HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "template.html")
JE = {"user", "assistant", "attachment", "system"}
LOCAL = datetime.datetime.now().astimezone().tzinfo  # local timezone -> local day boundaries
KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")

# USD per 1,000,000 tokens: (input, output, cache read, cache write).
# Approximate public list prices. Unknown models are still counted for tokens, just not priced.
PRICE = {
    "claude-opus-5": (5, 25, 0.5, 6.25), "claude-opus-5-5": (4, 20, 0.2, 5),
    "claude-opus-4-8": (5, 25, 0.5, 6.25), "claude-opus-4-7": (5, 25, 0.5, 6.25),
    "claude-opus-4-6": (5, 25, 0.5, 6.25), "claude-opus-4-1": (15, 75, 1.5, 18.75),
    "claude-opus-4": (15, 75, 1.5, 18.75), "claude-sonnet-5-5": (2, 10, 0.2, 2.5),
    "claude-sonnet-5": (2, 10, 0.2, 2.5), "claude-sonnet-4-6": (3, 15, 0.3, 3.75),
    "claude-sonnet-4-5": (3, 15, 0.3, 3.75), "claude-sonnet-4": (3, 15, 0.3, 3.75),
    "claude-3-7-sonnet": (3, 15, 0.3, 3.75), "claude-fable-5-1": (10, 50, 0.25, 12.5),
    "claude-fable-5": (10, 50, 1, 12.5), "claude-haiku-4-5": (1, 5, 0.1, 1.25),
    "claude-3-5-haiku": (0.8, 4, 0.08, 1),
}
CACHE_VERSION = 1


def parse(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(LOCAL)
    except Exception:
        return None


def list_files():
    out = []
    if not os.path.isdir(ROOT):
        return out
    for proj in os.listdir(ROOT):
        pd = os.path.join(ROOT, proj)
        if not os.path.isdir(pd):
            continue
        for name in os.listdir(pd):
            p = os.path.join(pd, name)
            if os.path.isfile(p) and name.endswith(".jsonl"):
                out.append((p, False))
            elif os.path.isdir(p):
                sd = os.path.join(p, "subagents")
                if os.path.isdir(sd):
                    out += [(os.path.join(sd, a), True) for a in os.listdir(sd)
                            if a.startswith("agent-") and a.endswith(".jsonl")]
    return out


def scan(path, sub):
    days, hours = {}, {}
    slots = collections.Counter()
    title = None
    first = last = None
    seen_ids = set()

    def day(d):
        if d not in days:
            days[d] = {"s": 0, "m": 0, "t": 0, "tok": 0, "c": 0.0, "sv": 0.0,
                       "cm": {}, "bm": {}, "u": {}, "h": [0] * 24}
        return days[d]

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if not isinstance(e, dict):
                continue
            t = e.get("type")
            if not sub and t in ("custom-title", "summary", "ai-title"):
                for k in ("customTitle", "title", "aiTitle", "summary"):
                    if isinstance(e.get(k), str) and e[k].strip():
                        title = e[k].strip()
                continue
            if t not in JE or (not sub and e.get("isSidechain")):
                continue
            q = parse(e.get("timestamp"))
            if q is None:
                continue
            d = q.date().isoformat()
            hh = d + " %02d" % q.hour
            D = day(d)
            if not sub:
                if first is None:
                    first = q
                    D["s"] += 1
                    D["h"][q.hour] += 1
                last = q
                D["m"] += 1
                H = hours.setdefault(hh, {"m": 0, "bm": {}, "cm": {}})
                H["m"] += 1
                slots[int(q.timestamp() // 600)] += 1
            if t == "assistant" and isinstance(e.get("message"), dict):
                m = e["message"]
                c = m.get("content")
                if isinstance(c, list):
                    D["t"] += sum(1 for x in c if isinstance(x, dict) and x.get("type") == "tool_use")
                u = m.get("usage")
                model = m.get("model") or "unknown"
                if u and model != "<synthetic>":
                    v = [u.get(k) if isinstance(u.get(k), (int, float)) else 0 for k in KEYS]
                    tt = sum(v)
                    D["tok"] += tt
                    D["bm"][model] = D["bm"].get(model, 0) + tt
                    U = D["u"].setdefault(model, [0, 0, 0, 0])
                    for i in range(4):
                        U[i] += v[i]
                    H = hours.setdefault(hh, {"m": 0, "bm": {}, "cm": {}})
                    H["bm"][model] = H["bm"].get(model, 0) + tt
                    mid = m.get("id")
                    if model in PRICE and not (mid and mid in seen_ids):
                        pr = PRICE[model]
                        cost = sum(v[i] * pr[i] for i in range(4)) / 1e6
                        D["c"] += cost
                        D["sv"] += v[2] * (pr[0] - pr[2]) / 1e6
                        D["cm"][model] = D["cm"].get(model, 0) + cost
                        H["cm"][model] = H["cm"].get(model, 0) + cost
                    if mid:
                        seen_ids.add(mid)
    return {"sub": sub, "days": days, "hours": hours,
            "slots": {str(k): v for k, v in slots.items()}, "title": title,
            "first": first.isoformat() if first else None,
            "last": last.isoformat() if last else None}


def build(out_dir):
    now = datetime.datetime.now(LOCAL)
    today = now.date().isoformat()
    cache_path = os.path.join(out_dir, ".counter-cache.json")
    try:
        cache = json.load(open(cache_path, encoding="utf-8"))
        if cache.get("__v") != CACHE_VERSION:
            cache = {}
    except Exception:
        cache = {}
    fresh, rescanned = {}, 0
    for p, sub in list_files():
        try:
            st = os.stat(p)
        except OSError:
            continue
        sig = [st.st_mtime, st.st_size]
        old = cache.get(p)
        if old and old.get("sig") == sig:
            fresh[p] = old
            continue
        try:
            r = scan(p, sub)
        except Exception:
            continue
        r["sig"] = sig
        fresh[p] = r
        rescanned += 1
    try:
        tmp = cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(fresh, __v=CACHE_VERSION), f)
        os.replace(tmp, cache_path)
    except Exception:
        pass

    days = {}
    todayh = [{"m": 0, "bm": {}, "cm": {}} for _ in range(24)]
    slot_sess = collections.defaultdict(int)
    lanes, longest, agents = [], 0.0, 0
    t_start = int(datetime.datetime.fromisoformat(today + "T00:00:00").replace(tzinfo=LOCAL).timestamp() // 600)
    nowslot = int(now.timestamp() // 600)
    running = 0
    for p, r in fresh.items():
        if r["sub"]:
            agents += 1
        for d, x in r["days"].items():
            D = days.setdefault(d, {"d": d, "s": 0, "m": 0, "t": 0, "tok": 0, "c": 0.0, "sv": 0.0,
                                    "cm": {}, "bm": {}, "u": {}, "h": [0] * 24})
            for m, v in x.get("cm", {}).items():
                D["cm"][m] = D["cm"].get(m, 0) + v
            for k in ("s", "m", "t", "tok", "c", "sv"):
                D[k] += x.get(k, 0)
            for m, v in x["bm"].items():
                D["bm"][m] = D["bm"].get(m, 0) + v
            for m, v in x["u"].items():
                U = D["u"].setdefault(m, [0, 0, 0, 0])
                for i in range(4):
                    U[i] += v[i]
            for i in range(24):
                D["h"][i] += x["h"][i]
        for hh, x in r["hours"].items():
            if hh[:10] == today:
                H = todayh[int(hh[11:])]
                H["m"] += x["m"]
                for m, v in x["bm"].items():
                    H["bm"][m] = H["bm"].get(m, 0) + v
                for m, v in x.get("cm", {}).items():
                    H["cm"][m] = H["cm"].get(m, 0) + v
        if not r["sub"] and r["first"]:
            s = [int(k) for k in r["slots"]]
            for k in s:
                slot_sess[k] += 1
            dur = (datetime.datetime.fromisoformat(r["last"]) - datetime.datetime.fromisoformat(r["first"])).total_seconds()
            longest = max(longest, dur)
            act = sorted(k - t_start for k in s if t_start <= k < t_start + 144)
            if act:
                lanes.append({"t": r["title"] or "Untitled session", "s": act})
            if any(k >= nowslot - 1 for k in s):
                running += 1
    lanes.sort(key=lambda l: l["s"][0])
    maxpar = collections.defaultdict(int)
    for k, n in slot_sess.items():
        d = datetime.datetime.fromtimestamp(k * 600, LOCAL).date().isoformat()
        maxpar[d] = max(maxpar[d], n)
    best = max(slot_sess.items(), key=lambda kv: kv[1]) if slot_sess else (nowslot, 0)
    for d, D in days.items():
        D["p"] = maxpar.get(d, 0)
    data = {
        "today": today, "computed": now.strftime("%Y-%m-%d %H:%M"),
        "days": [days[d] for d in sorted(days)], "todayh": todayh, "lanes": lanes,
        "todayConc": [slot_sess.get(t_start + i, 0) for i in range(144)],
        "nowIdx": nowslot - t_start, "running": running,
        "record": {"n": best[1], "at": datetime.datetime.fromtimestamp(best[0] * 600, LOCAL).strftime("%Y-%m-%d %H:%M")},
        "longestH": round(longest / 3600, 1), "activeH": round(len(slot_sess) / 6, 1), "agents": agents,
    }
    with open(TEMPLATE, encoding="utf-8") as f:
        tpl = f.read()
    bid = hashlib.sha1(tpl.encode("utf-8")).hexdigest()[:12]
    data["build"] = bid
    blob = json.dumps(data, separators=(",", ":"))
    os.makedirs(out_dir, exist_ok=True)
    for name, content in (("stats-data.js", "window.CCS_UPDATE&&window.CCS_UPDATE(" + blob + ");"),
                          ("board.html", tpl.replace("__DATA__", blob).replace("__BUILD__", bid))):
        path = os.path.join(out_dir, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    return rescanned, len(fresh), data


def main():
    ap = argparse.ArgumentParser(description="Build a local Claude Code usage stats board.")
    ap.add_argument("--out", default=HERE, help="output directory (default: next to this script)")
    ap.add_argument("--watch", type=float, default=0, help="rebuild every N seconds (0 = once)")
    a = ap.parse_args()
    out_dir = os.path.abspath(os.path.expanduser(a.out))
    while True:
        t0 = time.time()
        try:
            rescanned, total, data = build(out_dir)
            msg = "built board.html from %d sessions (%d rescanned) in %.1fs" % (total, rescanned, time.time() - t0)
        except Exception as e:
            msg = "error: %r" % e
        print(time.strftime("%H:%M:%S"), msg)
        if not a.watch:
            board = os.path.join(out_dir, "board.html")
            print("open:", board)
            try:
                import webbrowser
                webbrowser.open("file:///" + board.replace("\\", "/"))
            except Exception:
                pass
            return
        time.sleep(max(5, a.watch))


if __name__ == "__main__":
    main()