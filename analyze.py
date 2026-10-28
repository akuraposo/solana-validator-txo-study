#!/usr/bin/env python3
"""
Analyze reconciled transaction data: does fee tier / send order predict
in-block position? Per-client comparison across validators.

Inputs : evidence/tx_state.json (from reconcile.py), evidence/run_*_live.json
Outputs: reports/summary.csv, data/analysis.json, reports/*.png
"""
import csv
import glob
import json
import os
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
EVID = os.path.join(HERE, "evidence")
DATA = os.path.join(HERE, "data")
REPORTS = os.path.join(HERE, "reports")
STATE = os.path.join(EVID, "tx_state.json")

TIERS = ["low", "mid", "high"]
TIER_RANK = {"low": 0, "mid": 1, "high": 2}


def load():
    st = json.load(open(STATE))
    # enrich with run metadata (send order) from run files
    meta = {}
    for f in sorted(glob.glob(os.path.join(EVID, "run_*_live.json"))):
        try:
            d = json.load(open(f))
        except json.JSONDecodeError:
            continue
        for r in d["records"]:
            if r.get("sig") and not r["sig"].startswith("DRYRUN"):
                m = meta.get(r["sig"], {})
                m.update({k: r[k] for k in ("tier", "send_index", "window_pos",
                                            "target_identity", "target_client",
                                            "target_version_lot", "sent_unix_ms")
                          if k in r})
                meta[r["sig"]] = m
    # spend ledger: authoritative tier+identity per sig (covers txs whose run
    # file was truncated by a killed process)
    ledger = {}
    if os.path.exists(os.path.join(EVID, "spend_ledger.jsonl")):
        for line in open(os.path.join(EVID, "spend_ledger.jsonl")):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            sig = r.get("sig")
            if sig and sig not in ledger:
                ledger[sig] = {"tier": r.get("tier"), "target_identity": r.get("identity")}
    # identity -> client map
    idmap = {}
    vfile = os.path.join(DATA, "validators.json")
    if os.path.exists(vfile):
        for v in json.load(open(vfile))["selected"]:
            idmap[v["identity"]] = {"target_client": v["client"],
                                    "target_version_lot": v["version"],
                                    "validator_name": v["name"]}
    rows = []
    for sig, d in st.items():
        row = {"sig": sig}
        row.update(ledger.get(sig, {}))
        row.update(idmap.get(row.get("target_identity"), {}))
        row.update(d)
        row.update(meta.get(sig, {}))
        if not row.get("target_client") and row.get("target_identity"):
            row["target_client"] = f"id:{row['target_identity'][:8]}"
        rows.append(row)
    return rows


def spearman(xs, ys):
    """small-n spearman rank correlation (no scipy)."""
    n = len(xs)
    if n < 3:
        return None
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return round(num / (dx * dy), 4)


def main():
    os.makedirs(REPORTS, exist_ok=True)
    rows = load()
    # send-timestamp order across the whole run, per block we need the RELATIVE
    # send order among txs that landed in the same block:
    same_block = defaultdict(list)
    for r in rows:
        if r.get("index_in_block") is not None:
            same_block[r["slot_landed"]].append(r)
    for slot, items in same_block.items():
        items.sort(key=lambda r: r.get("sent_unix_ms", 0))
        for rank, r in enumerate(items):
            r["send_order_in_block"] = rank
            r["n_ours_in_block"] = len(items)

    landed = [r for r in rows if r.get("index_in_block") is not None]
    print(f"total sends: {len(rows)}, resolved+in-block: {len(landed)}")

    by_client = defaultdict(list)
    for r in landed:
        by_client[r.get("target_client", "?")].append(r)

    summary = {}
    for cl, items in sorted(by_client.items()):
        idxs = [r["index_in_block"] for r in items]
        pcts = [r["position_percentile"] for r in items]
        # fee tier effect (only meaningful when high/low both present in same block-ish)
        tier_med = {}
        for t in TIERS:
            sub = [r["position_percentile"] for r in items if r.get("tier") == t]
            if sub:
                sub2 = sorted(sub)
                tier_med[t] = round(sub2[len(sub2) // 2], 4)
        # send-order effect within blocks
        snd = [(r["send_order_in_block"], r["index_in_block"])
               for r in items if r.get("n_ours_in_block", 0) >= 3]
        rho = spearman([a for a, _ in snd], [b for _, b in snd]) if snd else None
        # fee-fee: within blocks holding >=3 of our txs from THIS client,
        # does higher tier -> lower index?
        ftxt = [(TIER_RANK[r.get("tier", "mid")], r["index_in_block"]) for s in same_block.values()
                if len(s) >= 3 and any(x.get("target_client") == cl for x in s)
                for r in s if r.get("target_client") == cl and r.get("tier") in TIER_RANK]
        rho_fee = spearman([a for a, _ in ftxt], [b for _, b in ftxt]) if len(ftxt) >= 3 else None
        # pooled across all clients (for global conclusion)
        summary[cl] = {
            "n_landed": len(items),
            "n_blocks": len({r["slot_landed"] for r in items}),
            "index_min": min(idxs), "index_max": max(idxs),
            "percentile_median": round(sorted(pcts)[len(pcts) // 2], 4),
            "percentile_by_tier_median": tier_med,
            "spearman_sendorder_vs_index_same_block": rho,
            "spearman_tier_vs_index_same_block": rho_fee,
            "n_same_block_groups": sum(1 for s in same_block.values() if len(s) >= 3 and any(r.get("target_client") == cl for r in s)),
        }
        print(f"{cl:22s} n={len(items):3d} pct_med={summary[cl]['percentile_median']:.3f} "
              f"tiers={tier_med} sendorder_rho={rho} tier_rho={rho_fee}")

    # ---- figures -----------------------------------------------------------
    # 1: position percentile by tier, per client
    clients = sorted(by_client)
    fig, ax = plt.subplots(figsize=(10, 4.5))
    width = 0.25
    for i, t in enumerate(TIERS):
        xs, ys = [], []
        for j, cl in enumerate(clients):
            sub = [r["position_percentile"] for r in by_client[cl] if r.get("tier") == t]
            if sub:
                xs.append(j + (i - 1) * width)
                ys.append(sorted(sub)[len(sub) // 2])
        ax.bar(xs, ys, width=width, label=t)
    ax.set_xticks(range(len(clients)))
    ax.set_xticklabels(clients, rotation=20, ha="right")
    ax.set_ylabel("median position percentile in block")
    ax.set_title("Our tx position inside the block vs priority fee tier (0=first, 1=last)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(REPORTS, "position_by_tier.png"), dpi=130)

    # 2: scatter index vs send order for same-block groups
    fig, ax = plt.subplots(figsize=(6.5, 5.5))
    colors = plt.cm.tab10([i % 10 for i in range(len(clients))])
    for c, cl in zip(colors, clients):
        xs = [r["send_order_in_block"] for r in landed
              if r.get("target_client") == cl and r.get("n_ours_in_block", 0) >= 3]
        ys = [r["index_in_block"] for r in landed
              if r.get("target_client") == cl and r.get("n_ours_in_block", 0) >= 3]
        if xs:
            ax.scatter(xs, ys, alpha=0.75, label=cl, color=c)
    ax.set_xlabel("our send order within block (0 = sent first)")
    ax.set_ylabel("tx index inside block")
    ax.set_title("Send order vs landed position (blocks with >=3 of our txs)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(REPORTS, "sendorder_vs_index.png"), dpi=130)

    # 3: box plot — position percentile distribution per client
    fig, ax = plt.subplots(figsize=(10, 5))
    data = [[r["position_percentile"] for r in by_client[cl]] for cl in clients]
    bp = ax.boxplot(data, tick_labels=clients, patch_artist=True, showmeans=True)
    for patch, c in zip(bp["boxes"], plt.cm.tab10.colors):
        patch.set_facecolor(c)
        patch.set_alpha(0.6)
    ax.set_ylabel("position percentile in block (0 = first, 1 = last)")
    ax.set_title("Distribution of our tx position inside blocks, by validator client")
    ax.tick_params(axis="x", rotation=20)
    fig.tight_layout()
    fig.savefig(os.path.join(REPORTS, "position_boxplot_by_client.png"), dpi=130)

    json.dump(summary, open(os.path.join(DATA, "analysis.json"), "w"), indent=1)

    # ---- summary CSV --------------------------------------------------------
    with open(os.path.join(REPORTS, "summary.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["client", "n_landed", "n_blocks", "index_min", "index_max",
                    "percentile_median", "pct_median_low", "pct_median_mid", "pct_median_high",
                    "spearman_sendorder_vs_index_same_block"])
        for cl, s in summary.items():
            tm = s["percentile_by_tier_median"]
            w.writerow([cl, s["n_landed"], s["n_blocks"], s["index_min"], s["index_max"],
                        s["percentile_median"], tm.get("low", ""), tm.get("mid", ""),
                        tm.get("high", ""), s["spearman_sendorder_vs_index_same_block"]])

    # markdown tables for README
    lines = ["## Per-client summary (auto-generated by analyze.py)\n",
             "| client | n landed | blocks | percentile median | pct med (low/mid/high) | send-order rho |",
             "|---|---|---|---|---|---|"]
    for cl, s in summary.items():
        tm = s["percentile_by_tier_median"]
        lines.append(f"| {cl} | {s['n_landed']} | {s['n_blocks']} | {s['percentile_median']} | "
                     f"{tm.get('low','-')}/{tm.get('mid','-')}/{tm.get('high','-')} | "
                     f"{s['spearman_sendorder_vs_index_same_block']} |")
    open(os.path.join(DATA, "analysis_tables.md"), "w").write("\n".join(lines) + "\n")
    print("\nfigures + tables written to reports/ and data/")


if __name__ == "__main__":
    main()
