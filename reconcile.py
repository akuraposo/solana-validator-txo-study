#!/usr/bin/env python3
"""
Reconcile spend ledger signatures -> landing slot + in-block index.
Idempotent: signatures already resolved are skipped (state in evidence/tx_state.json).
Also recovers unresolved signatures from crashed runs.
Usage: python reconcile.py [--all]
"""
import json
import os
import sys
import time

from measure import Rpc, RPC, EVID, LEDGER

STATE = os.path.join(EVID, "tx_state.json")


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {}


def save_state(st):
    json.dump(st, open(STATE, "w"), indent=1)


def main():
    only_unresolved = "--all" not in sys.argv
    rebuild = "--rebuild" in sys.argv
    st = {} if rebuild else load_state()
    rpc = Rpc(RPC)

    ledger_sigs = []
    for line in open(LEDGER):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("sig") and not rec["sig"].startswith("DRYRUN"):
            ledger_sigs.append(rec)
    # dedupe
    seen = set()
    led = []
    for rec in ledger_sigs:
        if rec["sig"] not in seen:
            seen.add(rec["sig"])
            led.append(rec)

    def unresolved(sig):
        """not yet in state, or state exists but block resolution failed
        (retryable); sigs proven absent from an existing block (network
        dedupe) are dead and not retried."""
        v = st.get(sig)
        if v is None:
            return True
        if v.get("index_in_block") is not None:
            return False
        if v.get("missing_from_block"):
            return False
        return True

    todo = [r for r in led if not only_unresolved or unresolved(r["sig"])]
    if not todo:
        print("nothing to reconcile")
        return
    print(f"reconciling {len(todo)} signatures...")
    pending = {r["sig"]: r for r in todo}
    deadline = time.time() + 150

    while pending and time.time() < deadline:
        sigs = sorted(pending)
        try:
            res = rpc.call("getSignatureStatuses", [sigs, {"searchTransactionHistory": True}], retries=3)
        except RuntimeError as e:
            print("statuses error:", e)
            time.sleep(2)
            continue
        for s in sigs:
            status = res["value"][sigs.index(s)]
            if status and status.get("confirmationStatus") and status.get("slot"):
                pending[s].update({
                    "slot_landed": status["slot"],
                    "fee_actual_lamports": status.get("fee"),
                    "err": status.get("err"),
                    "confirmed_at_slot": status.get("confirmations"),
                })
                del pending[s]
        if pending:
            time.sleep(1.0)

    for sig, extra in pending.items():
        extra["landed"] = False
        extra["unresolved_after_reconcile"] = True

    by_slot = {}
    for r in todo:
        if r.get("slot_landed"):
            by_slot.setdefault(r["slot_landed"], []).append(r)

    for si, (slot, items) in enumerate(sorted(by_slot.items())):
        if si:
            time.sleep(1.2)  # public RPC throttles burst getBlock calls
        try:
            blk = rpc.call("getBlock", [slot, {"maxSupportedTransactionVersion": 1}], retries=4)
        except RuntimeError as e:
            for r in items:
                r["block_fetch_error"] = str(e)
            continue
        if not blk or not blk.get("transactions"):
            print(f"  slot {slot}: empty/None block response")
            for r in items:
                r["slot_skipped_or_missing"] = True
                r["landed"] = False
            continue
        sig_order = [t["transaction"]["signatures"][0] for t in blk["transactions"]]
        n = len(sig_order)
        meta = {
            "slot": slot, "blockTime": blk.get("blockTime"),
            "blockHeight": blk.get("blockHeight"), "parentSlot": blk.get("parentSlot"),
            "block_tx_count": n,
            "signature_order_first40": sig_order[:40],
        }
        json.dump(meta, open(os.path.join(EVID, f"block_{slot}.json"), "w"), indent=1)
        for r in items:
            r["block_meta"] = {"slot": slot, "block_tx_count": n}
            # clear stale failure flags from earlier passes
            r.pop("slot_skipped_or_missing", None)
            r.pop("missing_from_block", None)
            r["landed"] = True
            if r["sig"] in sig_order:
                idx = sig_order.index(r["sig"])
                r["index_in_block"] = idx
                r["position_percentile"] = round(idx / max(n - 1, 1), 4)
            else:
                r["index_in_block"] = None
                r["landed"] = False
                r["missing_from_block"] = True

    now = int(time.time() * 1000)
    for r in todo:
        r["reconciled_at"] = now
        s = st.get(r["sig"], {})
        s.update(r)
        st[r["sig"]] = s
    save_state(st)

    landed = [r for r in todo if r.get("index_in_block") is not None]
    print(f"resolved {len(landed)}/{len(todo)} into blocks")
    for r in todo:
        print(f"  {r['sig'][:24]:24s} slot={r.get('slot_landed')} idx={r.get('index_in_block')} "
              f"n={r.get('block_meta',{}).get('block_tx_count')} fee={r.get('fee_actual_lamports')} "
              f"tier={r.get('tier')} id={str(r.get('identity'))[:8]}")


if __name__ == "__main__":
    main()
