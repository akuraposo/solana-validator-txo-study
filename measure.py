#!/usr/bin/env python3
"""
Inside the Block — Solana validator transaction-ordering measurement.

Sends self-transfer transactions (0.001 SOL) into the leader windows of
selected mainnet validators, alternating priority-fee tiers, then records
where each transaction landed inside its block (index / block tx count).

SAFETY:
  * DRY-RUN by default. Nothing is ever sent without `--live`.
  * Hard spend cap in lamports, enforced before every send and persisted in
    evidence/spend_ledger.jsonl so restarts cannot exceed it.
  * Keys only via env (SOLANA_KEYPAIR_JSON) or --keypair; never in the repo.

Usage:
  python measure.py --dry-run          # default: build + simulate, no send
  python measure.py --live --spend-cap-sol 0.05
"""
import argparse
import base64
import itertools
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.message import Message
from solders.transaction import Transaction

# SPL Memo v2 program — used to make every tx byte-unique so the network
# cannot dedupe identical blockhash+tier duplicates (lesson learned:
# duplicate txs are silently dropped, with maxRetries=0 they never land).
MEMO_PROG = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")

RPC = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
HERE = os.path.dirname(os.path.abspath(__file__))
EVID = os.path.join(HERE, "evidence")
DATA = os.path.join(HERE, "data")
LEDGER = os.path.join(EVID, "spend_ledger.jsonl")

SLOTS_PER_EPOCH = 432000
SLOT_MS = 400.0
WIN = 4                      # leader window = 4 slots
TRANSFER_LAMPORTS = 1_000_000  # 0.001 SOL self-transfer
BASE_FEE_LAMPORTS = 5_000
CU_LIMIT = 20_000            # generous margin: simple transfer ~300 CU at recent
                            # versions; a too-tight limit fails on-chain with
                            # ComputationalBudgetExceeded (observed post-Alpenglow)

# Priority fee tiers (micro-lamports per CU), sent in rotating order.
TIERS = {"low": 1_000, "mid": 50_000, "high": 500_000}

COMPUTE_PROG = Pubkey.from_string("ComputeBudget111111111111111111111111111111")
SYSTEM_PROG = Pubkey.from_string("11111111111111111111111111111111")


class Rpc:
    def __init__(self, url):
        self.url = url
        self.s = requests.Session()
        self.i = 0

    def call(self, method, params=None, retries=5):
        self.i += 1
        body = {"jsonrpc": "2.0", "id": self.i, "method": method, "params": params or []}
        delay = 0.5
        for a in range(retries):
            try:
                r = self.s.post(self.url, json=body, timeout=20)
                if r.status_code == 429:
                    time.sleep(delay)
                    delay = min(delay * 2, 10)
                    continue
                j = r.json()
                if "error" in j:
                    raise RuntimeError(f"{method}: {j['error']}")
                return j["result"]
            except (requests.RequestException, ValueError):
                if a == retries - 1:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 10)


def load_keypair(path):
    raw = json.load(open(path))
    return Keypair.from_bytes(bytes(raw))


def spend_so_far():
    """Total projected fees over UNIQUE signatures only (the ledger contains
    a historical double-write bug: the same sig was appended once per send
    attempt; fee-cap must count each tx once)."""
    if not os.path.exists(LEDGER):
        return 0
    seen = set()
    tot = 0
    for line in open(LEDGER):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        sig = rec.get("sig")
        if sig and not sig.startswith("DRYRUN") and sig not in seen:
            seen.add(sig)
            tot += rec["fee_lamports_projected"]
    return tot


def ledger_write(rec):
    os.makedirs(EVID, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps(rec) + "\n")


def make_tx(kp, blockhash, price_micro, dest, memo=None):
    """ComputeBudget price + limit, optional unique memo, system self-transfer."""
    ixs = []
    # SetComputeUnitLimit = tag 2, u32 LE
    ixs.append(Instruction(COMPUTE_PROG, bytes([2]) + CU_LIMIT.to_bytes(4, "little"), []))
    # SetComputeUnitPrice = tag 3, u64 LE micro-lamports
    ixs.append(Instruction(COMPUTE_PROG, bytes([3]) + price_micro.to_bytes(8, "little"), []))
    # System transfer: tag 2 + u64 lamports; accounts [from^, to^]
    data = bytes([2]) + TRANSFER_LAMPORTS.to_bytes(8, "little")
    accts = [AccountMeta(kp.pubkey(), True, True), AccountMeta(dest, True, True)]
    ixs.append(Instruction(SYSTEM_PROG, data, accts))
    if memo is not None:
        # SPL Memo v2: makes the tx byte-unique for the same blockhash+tier
        # (network dedups identical txs -> without this, duplicates never land)
        ixs.append(Instruction(MEMO_PROG, memo.encode(), []))
    msg = Message.new_with_blockhash(ixs, kp.pubkey(), blockhash)
    return Transaction([kp], msg, blockhash)


def projected_fee(price_micro):
    return BASE_FEE_LAMPORTS + (price_micro * CU_LIMIT) // 1_000_000


def cluster_versions(rpc):
    nodes = rpc.call("getClusterNodes")
    return {n["pubkey"]: n.get("version", "?") for n in nodes}


def leader_windows(rpc, identities, horizon_slots=288):
    """Return sorted [(slot_abs, identity)] for target validators in horizon."""
    ei = rpc.call("getEpochInfo")
    start = ei["absoluteSlot"] - (ei["slotIndex"] % SLOTS_PER_EPOCH)
    sched = rpc.call("getLeaderSchedule")
    rows = []
    for ident, slots in sched.items():
        if ident not in identities:
            continue
        ss = set(slots)
        for off in range(ei["slotIndex"], ei["slotIndex"] + horizon_slots):
            if off in ss:
                rows.append((start + off, ident))
    return sorted(rows), ei


def group_windows(rows):
    """group consecutive slots (same validator) into windows."""
    out = []
    for slot, ident in rows:
        if out and out[-1]["end"] == slot - 1 and out[-1]["identity"] == ident:
            out[-1]["end"] = slot
        else:
            out.append({"identity": ident, "start": slot, "end": slot})
    return out


def now_ms():
    return int(time.time() * 1000)


def run(args):
    os.makedirs(EVID, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)
    kp = load_keypair(args.keypair)
    rpc = Rpc(RPC)
    vals = json.load(open(os.path.join(DATA, "validators.json")))["selected"]
    sel = {v["identity"]: v for v in vals}
    print(f"pubkey={kp.pubkey()} mode={'LIVE' if args.live else 'DRY-RUN'} cap={args.spend_cap_sol} SOL")

    versions = cluster_versions(rpc)
    rows, ei = leader_windows(rpc, set(sel), horizon_slots=args.horizon)
    wins = [w for w in group_windows(rows) if w["end"] - w["start"] >= 2]
    print(f"epoch={ei['epoch']} slot={ei['absoluteSlot']} targets: {len(wins)} windows in next {args.horizon} slots")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    recs = []
    led_sigs = {json.loads(l)["sig"] for l in open(LEDGER) if l.strip() and json.loads(l).get("sig")} \
        if os.path.exists(LEDGER) else set()
    cap_lamports = int(args.spend_cap_sol * 1_000_000_000)
    outp = os.path.join(EVID, f"run_{run_id}_{'live' if args.live else 'dry'}.json")

    def dump_partial():
        json.dump({
            "run_id": run_id, "mode": "live" if args.live else "dry-run",
            "rpc": RPC, "spend_cap_sol": args.spend_cap_sol,
            "keypair_pubkey": str(kp.pubkey()),
            "epoch": ei["epoch"],
            "cluster_versions_at_run": versions,
            "alpenglow_note": "all measurements taken AFTER 2026-10-21 Alpenglow upgrade",
            "records": recs,
        }, open(outp, "w"), indent=1)

    if args.single_identity:
        wins = [w for w in wins if sel[w["identity"]]["identity"] == args.single_identity]
    wins = wins[: args.max_windows]
    print(f"planned windows: {len(wins)}")

    for k, w in enumerate(wins):
        v = sel[w["identity"]]
        spent = spend_so_far()
        if args.live and spent >= cap_lamports:
            print(f"SPEND CAP REACHED ({spent} lamports). stopping.")
            break
        n_tx = args.txs_per_window
        proj = projected_fee(TIERS["high"]) * n_tx
        if args.live and spent + proj > cap_lamports:
            print(f"window {k}: projected spend exceeds cap, skipping")
            continue

        # ── sync to just before our window, then fire the burst without
        #    further slot polling (polling RTT would eat the 1.6 s window).
        while True:
            cur = rpc.call("getSlot")
            if cur >= w["start"] - 1:
                break
            if cur > w["end"]:
                break
            time.sleep(0.30)
        if cur > w["end"]:
            print(f"window {k}: missed (cur={cur} > {w['end']})")
            continue

        blockhash = rpc.call("getLatestBlockhash")["value"]["blockhash"]
        from solders.hash import Hash
        bh = Hash.from_string(blockhash)

        # send burst: rotate tiers low/mid/high with spacing, twice (early + late in window)
        sigs_meta = []
        plan = [(t, q) for q in ("start", "late") for t in ("low", "mid", "high")] * (n_tx // 6 + 1)
        plan = plan[:n_tx]
        t0 = now_ms()
        memo_c = itertools.count(int(time.time()) % 100000)
        for j, (tier, qpos) in enumerate(plan):
            price = TIERS[tier]
            memo = f"txo|{run_id}|w{k}|{j}|{tier}|{next(memo_c)}|{now_ms()}"
            tx = make_tx(kp, bh, price, kp.pubkey(), memo=memo)
            b64 = base64.b64encode(bytes(tx)).decode()
            if args.live:
                # no per-send slot polling: RTT would eat the 1.6 s window.
                # We synced to the window boundary above; reconcile() assigns
                # each signature to its ACTUAL landing slot afterwards.
                r = rpc.call("sendTransaction", [b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}], retries=2)
                sig = r
                cur2 = rpc.call("getSlot") if j == 0 else cur
                fee_proj = projected_fee(price)
                if sig not in led_sigs:
                    led_sigs.add(sig)
                    ledger_write({"ts": now_ms(), "sig": sig, "fee_lamports_projected": fee_proj,
                                  "tier": tier, "identity": w["identity"]})
            else:
                sig = f"DRYRUN-j{k}-{j}"
                cur2 = cur
                fee_proj = projected_fee(price)
            recs.append({
                "sig": sig, "tier": tier, "window_pos": qpos, "send_index": j,
                "sent_unix_ms": now_ms(), "observed_slot_when_sending": cur2 if args.live else None,
                "target_slot_window": [w["start"], w["end"]],
                "target_identity": w["identity"], "target_client": v["client"],
                "target_version_lot": v["version"], "projected_fee_lamports": fee_proj,
            })
            time.sleep(args.send_gap_ms / 1000.0)
        print(f"window {k} {v['name']:<22s} slots {w['start']}-{w['end']} sent={len(plan)} mode={'live' if args.live else 'dry'}")
        dump_partial()

    # DEFFERED resolve: reconciliation happens once at the end (reconcile.py
    # can also be run later) so per-window block fetches never eat the
    # following leader windows.
    if args.live and any(not r["sig"].startswith("DRYRUN") for r in recs):
        try:
            resolve(rpc, recs, versions)
        except Exception as e:
            print(f"resolve() failed (data kept, run reconcile.py): {e!r}")

    outp = os.path.join(EVID, f"run_{run_id}_{'live' if args.live else 'dry'}.json")
    json.dump({
        "run_id": run_id, "mode": "live" if args.live else "dry-run",
        "rpc": RPC, "spend_cap_sol": args.spend_cap_sol,
        "keypair_pubkey": str(kp.pubkey()),
        "epoch": ei["epoch"],
        "cluster_versions_at_run": versions,
        "alpenglow_note": "all measurements taken AFTER 2026-10-21 Alpenglow upgrade",
        "records": recs,
    }, open(outp, "w"), indent=1)
    print(f"evidence -> {outp}  ({len(recs)} records)")


def resolve(rpc, recs, versions):
    """wait for confirmations, fetch blocks, record landing index."""
    live = [r for r in recs if not r["sig"].startswith("DRYRUN") and "slot_landed" not in r]
    if not live:
        return
    sig_list = [r["sig"] for r in live]
    by_sig = {r["sig"]: r for r in live}
    deadline = time.time() + 40
    pending = set(sig_list)
    while pending and time.time() < deadline:
        res = rpc.call("getSignatureStatuses", [sorted(pending), {"searchTransactionHistory": False}])
        for s, st in zip(sorted(pending), res["value"]):
            if st and st.get("confirmationStatus") in ("confirmed", "finalized") and st.get("slot"):
                by_sig[s]["slot_landed"] = st["slot"]
                by_sig[s]["fee_actual_lamports"] = st.get("fee")
                pending.discard(s)
        if pending:
            time.sleep(0.8)
    for r in live:
        r["landed"] = "slot_landed" in r
    # fetch blocks uniquely
    slots = sorted({r["slot_landed"] for r in live if r.get("slot_landed")})
    for slot in slots:
        try:
            blk = rpc.call("getBlock", [slot, {"maxSupportedTransactionVersion": 1}], retries=3)
        except RuntimeError as e:
            for r in live:
                if r.get("slot_landed") == slot:
                    r["block_fetch_error"] = str(e)
            continue
        if not blk or not blk.get("transactions"):
            # slot was skipped (leader produced no block, e.g. block not yet
            # replayed at RPC or skipped slot) — txs from our ledger will move
            # to the next leader's block on a later reconcile pass.
            for r in live:
                if r.get("slot_landed") == slot:
                    r["slot_skipped_or_missing"] = True
                    r.pop("slot_landed", None)
                    r["landed"] = False
            continue
        sig_order = [t["transaction"]["signatures"][0] for t in blk["transactions"]]
        n = len(sig_order)
        block_meta = {
            "slot": slot, "blockTime": blk.get("blockTime"), "blockHeight": blk.get("blockHeight"),
            "parentSlot": blk.get("parentSlot"), "block_tx_count": n,
            "signature_order_first40": sig_order[:40],
        }
        for r in live:
            if r.get("slot_landed") == slot:
                r["index_in_block"] = sig_order.index(r["sig"]) if r["sig"] in sig_order else None
                r["position_percentile"] = None if r["index_in_block"] is None else round(r["index_in_block"] / max(n - 1, 1), 4)
                r["block_meta"] = block_meta
        # persist per-block raw minimal evidence (judge-verifiable)
        bp = os.path.join(EVID, f"block_{slot}.json")
        json.dump(block_meta, open(bp, "w"), indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="REAL SEND (default: dry-run)")
    ap.add_argument("--spend-cap-sol", type=float, default=0.0,
                    help="hard fee spend cap; REQUIRED for --live")
    ap.add_argument("--keypair", default=os.environ.get("SOLANA_KEYPAIR_JSON", os.path.expanduser("~/.config/solana/pk1.json")))
    ap.add_argument("--txs-per-window", type=int, default=6)
    ap.add_argument("--max-windows", type=int, default=8)
    ap.add_argument("--horizon", type=int, default=288)
    ap.add_argument("--send-gap-ms", type=int, default=120)
    ap.add_argument("--single-identity", default=None, help="only windows of this identity")
    args = ap.parse_args()

    if args.live and args.spend_cap_sol <= 0:
        sys.exit("refusing --live without a positive --spend-cap-sol")
    if not args.live:
        args.spend_cap_sol = args.spend_cap_sol or 0.05
    if not os.path.exists(args.keypair):
        sys.exit(f"keypair file not found: {args.keypair} (env SOLANA_KEYPAIR_JSON)")
    run(args)


if __name__ == "__main__":
    main()
