#!/usr/bin/env python3
"""
gf180mcu_area_estimator.py

Reads a SPICE netlist (xschem output) and estimates pre-layout area for
GlobalFoundries GF180MCU designs using device_db.json produced by
scripts/gf180mcu_measure_devices.py.

Usage:
    python3 gf180mcu_area_estimator.py --netlist my_design.spice --db device_db.json
    python3 gf180mcu_area_estimator.py --netlist my_design.spice            # auto-finds db
    python3 gf180mcu_area_estimator.py --netlist my_design.spice --budget "500um x 500um"

Library:
    from gf180mcu_area_estimator import estimate
    report = estimate("my_design.spice", "device_db.json")

Ported from chennakeshavadasa/SCH_AREA_ESTIMATOR (SKY130) to GF180MCU.
NOTE: device_db.json ships with PLACEHOLDER coefficients. Rebuild it with
scripts/gf180mcu_measure_devices.py before trusting results.
"""

import argparse, json, re, sys
from pathlib import Path
from typing import Any

# ── Recognise GF180MCU device model names ────────────────────────────────────
#   gf180mcu_fd_pr__nfet_03v3  W=1 L=0.28 nf=4 m=2
#   gf180mcu_fd_pr__npolyf_u   l=10 w=1 m=1
_GF180_MODEL = re.compile(r'gf180mcu_fd_(?:pr|bs|io)__\S+', re.IGNORECASE)
_PARAM_RE    = re.compile(r'(\w+)\s*=\s*([^\s]+)')


def parse_spice(path: str) -> list:
    """Return list of {'model','dev','params','raw'}."""
    instances = []
    text = Path(path).read_text(errors="replace")
    # Join continuation lines (line ending in '\' or next line starting '+')
    text = re.sub(r'\\\s*\n\+?\s*', ' ', text)
    text = re.sub(r'\n\+\s*', ' ', text)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('*') or line.startswith('.'):
            continue
        if not line.upper().startswith('X'):
            continue
        m = _GF180_MODEL.search(line)
        if not m:
            continue
        model = m.group(0).lower()
        dev = re.sub(r'^gf180mcu_fd_(?:pr|bs|io)__', '', model)
        params: dict = {"w": None, "l": None, "nf": 1, "m": 1}
        for k, v in _PARAM_RE.findall(line):
            key = k.lower()
            try:
                fval = float(v)
            except ValueError:
                continue
            if key in ("w", "width"):
                params["w"] = fval
            elif key in ("l", "length"):
                params["l"] = fval
            elif key in ("nf", "fingers", "nfin"):
                params["nf"] = max(1, int(round(fval)))
            elif key == "m":
                params["m"] = max(1, int(round(fval)))
        instances.append({"model": model, "dev": dev, "params": params, "raw": line[:80]})
    return instances


# ── Area lookups ─────────────────────────────────────────────────────────────
def _lookup_mosfet(db, dev, w, l, nf):
    data = db.get("mosfets", {}).get(dev)
    if not data:
        return None
    model = data.get("model")
    if model and "ah" in model:
        h = max(0.0, model["ah"] * w + model["bh"])
        w_box = max(0.0, model["aw"] * (l * nf) + model["bw"] * nf + model["cw"])
        return h * w_box
    pts = data.get("sweep", [])
    if not pts:
        return None
    best = min(pts, key=lambda p: abs(p["w"]-w) + abs(p["l"]-l) + abs(p["nf"]-nf))
    ref = best["w"] * best["nf"]
    scale = (w * nf) / ref if ref else 1
    return best["area_um2"] * scale


def _lookup_res(db, dev, w, l):
    data = db.get("poly_resistors", {}).get(dev)
    if not data:
        return None
    models = data.get("model", [])
    if models:
        m = models[0] if w is None else min(models, key=lambda x: abs(x["w"]-w))
        return max(0.0, m["slope"] * l + m["intercept"])
    pts = data.get("sweep", [])
    if not pts:
        return None
    best = min(pts, key=lambda p: abs(p["l"]-l) + (abs(p["w"]-w) if w else 0))
    return best["area_um2"]


def _lookup_mim(db, dev, w, l):
    data = db.get("mim_caps", {}).get(dev)
    if not data:
        return None
    model = data.get("model")
    if model:
        b = model["border_um"]
        return (w + 2*b) * (l + 2*b)
    pts = data.get("sweep", [])
    if not pts:
        return None
    best = min(pts, key=lambda p: abs(p["w"]-w) + abs(p["l"]-l))
    return best["area_um2"]


def _lookup_var(db, dev, w, l):
    data = db.get("var_caps", {}).get(dev)
    if not data:
        return None
    model = data.get("model")
    if model:
        return max(0.0, model["a"]*w + model["b"]*l + model["c"])
    pts = data.get("sweep", [])
    if not pts:
        return None
    best = min(pts, key=lambda p: abs(p["w"]-w) + abs(p["l"]-l))
    return best["area_um2"]


def _lookup_bjt(db, dev):
    for k, v in db.get("bjts", {}).items():
        if k.lower() == dev.lower():
            return v["area_um2"]
    return None


def _classify(dev: str) -> str:
    if any(x in dev for x in ("nfet_", "pfet_")) and "cap_" not in dev:
        return "mosfet"
    if "cap_mim" in dev:
        return "mim"
    if any(x in dev for x in ("cap_nmos", "cap_pmos", "cap_var")):
        return "var"
    if any(x in dev for x in ("npolyf", "ppolyf", "nplus_", "pplus_", "_res")):
        return "res"
    if any(x in dev for x in ("npn", "pnp")):
        return "bjt"
    return "unknown"


def lookup_area(db, inst):
    dev = inst["dev"]; p = inst["params"]
    w, l, nf, m = p.get("w"), p.get("l"), p.get("nf", 1), p.get("m", 1)
    cat = _classify(dev)
    area_1, note = None, ""
    if cat == "mosfet":
        if w is None or l is None:
            note = "missing W or L"
        else:
            area_1 = _lookup_mosfet(db, dev, w, l, nf); note = f"w={w} l={l} nf={nf}"
    elif cat == "res":
        if l is None:
            note = "missing L"
        else:
            area_1 = _lookup_res(db, dev, w, l); note = f"l={l}" + (f" w={w}" if w else "")
    elif cat == "mim":
        if w is None or l is None:
            note = "missing W or L"
        else:
            area_1 = _lookup_mim(db, dev, w, l); note = f"w={w} l={l}"
    elif cat == "var":
        if w is None or l is None:
            note = "missing W or L"
        else:
            area_1 = _lookup_var(db, dev, w, l); note = f"w={w} l={l}"
    elif cat == "bjt":
        area_1 = _lookup_bjt(db, dev); note = "fixed size"
    else:
        note = "unrecognised device"
    if area_1 is None:
        return None, note
    return area_1 * m, note + (f" x m={m}" if m > 1 else "")


def parse_budget(s):
    if s is None:
        return None
    s = s.lower().replace("um", "").replace("µm", "")
    s = s.replace("vs", "x").replace("*", "x").replace(" ", "")
    if "x" in s:
        a, b = s.split("x")[:2]
        return float(a) * float(b)
    return float(s)


def estimate(netlist, db_path, routing_overhead=1.30):
    db = json.loads(Path(db_path).read_text())
    instances = parse_spice(netlist)
    rows, total, unknown = [], 0.0, []
    for inst in instances:
        area, note = lookup_area(db, inst)
        if area is not None:
            total += area
            rows.append({"dev": inst["dev"], "model": inst["model"],
                         "params": inst["params"], "area": round(area, 4), "note": note})
        else:
            unknown.append({"dev": inst["dev"], "reason": note})
    routing_area = total * (routing_overhead - 1)
    total_ro = total + routing_area
    return {
        "netlist": netlist, "db": db_path,
        "instances_parsed": len(instances),
        "instances_costed": len(rows),
        "instances_unknown": len(unknown),
        "device_area_um2": round(total, 2),
        "routing_overhead": routing_overhead,
        "routing_area_um2": round(routing_area, 2),
        "total_area_um2": round(total_ro, 2),
        "equiv_side_um": round(total_ro ** 0.5, 2),
        "breakdown": rows, "unknown": unknown,
    }


def print_report(r, budget=None):
    from itertools import groupby
    W = 72
    print("=" * W)
    print(" GF180MCU Pre-Layout Area Estimator")
    print(f" Netlist : {r['netlist']}")
    print(f" DB      : {r['db']}")
    print("=" * W)
    print(f" {'Device':<38} {'Params':<20} {'Area (um2)':>10}")
    print(f" {'-'*38} {'-'*20} {'-'*10}")
    rows = sorted(r["breakdown"], key=lambda x: x["dev"])
    for dev, grp in groupby(rows, key=lambda x: x["dev"]):
        grp = list(grp); dtot = sum(x["area"] for x in grp)
        for x in grp:
            print(f" {x['dev'][-38:]:<38} {x['note'][:20]:<20} {x['area']:>10.3f}")
        if len(grp) > 1:
            print(f" {'subtotal':>38} {'':20} {dtot:>10.3f}")
    print(f" {'-'*38} {'-'*20} {'-'*10}")
    print(f" {'Devices subtotal':<38} {'':20} {r['device_area_um2']:>10.2f}")
    print(f" {'Routing overhead (x'+str(r['routing_overhead'])+')':<38} {'':20} {r['routing_area_um2']:>10.2f}")
    print(f" {'-'*38} {'-'*20} {'-'*10}")
    print(f" {'TOTAL ESTIMATED AREA':<38} {'':20} {r['total_area_um2']:>10.2f}")
    print(f" {'Equivalent square side':<38} {'':20} {r['equiv_side_um']:>10.2f}")
    print("=" * W)
    print(f" Parsed {r['instances_parsed']} instances, "
          f"costed {r['instances_costed']}, skipped {r['instances_unknown']}")
    print("=" * W)
    if budget is not None:
        left = budget - r["total_area_um2"]
        util = 100.0 * r["total_area_um2"] / budget if budget else 0
        print(f" Area Budget Allowed  {budget:>10.2f} um2")
        print(f" Area Left Over       {left:>10.2f} um2")
        print(f" Utilization          {util:>9.1f} %")
        if left < 0:
            print(f" WARNING: You are OVER BUDGET by {abs(left):.2f} um2!")
        print("=" * W)


def main():
    ap = argparse.ArgumentParser(description="GF180MCU pre-layout area estimator")
    ap.add_argument("--netlist", required=True)
    ap.add_argument("--db", default=None)
    ap.add_argument("--budget", default=None)
    ap.add_argument("--routing", type=float, default=1.30)
    args = ap.parse_args()
    db = args.db or str(Path(__file__).resolve().parent / "device_db.json")
    if not Path(db).exists():
        sys.exit(f"device_db.json not found (looked at {db}); pass --db")
    r = estimate(args.netlist, db, routing_overhead=args.routing)
    print_report(r, budget=parse_budget(args.budget))


if __name__ == "__main__":
    main()
