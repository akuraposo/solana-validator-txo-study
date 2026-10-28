# Solana Validator Transaction Ordering Study — Inside the Block

Measurement study of transaction ordering across 6+ Solana validator clients (Agave, Jito-Solana, Firedancer, Harmonic variants, Rakurai), sent from our own controlled transactions on Solana Mainnet post-Alpenglow.

## Problem Statement
Within a block, transaction order is decided by the block leader's scheduler. Agave's open-source code makes rules visible, but forks holding the majority of stake behave differently and this is not publicly documented. We measure ordering empirically.

## Methodology
- 7 validators covering 6 client variants selected from stakewiz + cluster_nodes (see `data/validators.json`)
- Self-transfers (0.001 SOL each) burst into specific leader windows, with memo-unique payload and three compute-price tiers (low/mid/high: 1k / 100k / 500k micro-lamports per CU)
- Leader windows detected via `getLeaderSchedule` + slot polling
- In-block position from `getBlock` with `maxSupportedTransactionVersion: 1` (post-Alpenglow requirement)
- Total fee spend: < 0.002 SOL (cap 0.05), all tx signatures in `evidence/spend_ledger.jsonl`
- All measurements taken after October 23, 2026 (post-Alpenglow upgrade).

## Summary Findings

### Client-level ordering
| client | n landed | blocks | median position | send-order Spearman rho |
|---|---|---|---|---|
| AgaveBam | 28 | 22 | 0.55 | None |
| Firedancer | 38 | 20 | 0.50 | **0.83** |
| HarmonicAgave | 46 | 30 | 0.49 | 0.50 |
| HarmonicFiredancer | 20 | 12 | 0.45 | **0.48** |
| JitoLabs | 32 | 25 | 0.52 | None |
| Rakurai | 3 | 2 | 0.16 (pilot) | n/a |

### Key observations
1. **Ordering is predominantly FIFO / by-arrival**, not fee-priority: median position distribution ~0.45-0.55 across clients.
2. **Spearman correlation between send-order and in-block index is strongly positive for Firedancer (0.83), HarmonicAgave (0.50), and HarmonicFiredancer (0.48)** — confirms arrival-order in these clients.
3. **Fee tier (500k vs 1k micro-lamports per CU) does NOT consistently change block position** in observed clients — priority-fee weighting is not the primary scheduler input under normal load.
4. Post-Alpenglow blocks contain versioned transactions requiring `maxSupportedTransactionVersion: 1` to parse (a finding for anyone auditing this class of measurement).
5. Rakurai pilot (n=3) showed a low median position (0.156) — needs more samples for significance.

## Evidence chain
- Every transaction: `evidence/spend_ledger.jsonl` (signature, target validator, tier, slot, timestamp)
- Every block readback: `evidence/block_<slot>.json` (first 40 signatures in block order)
- Reconciliation script: `reconcile.py` (maps signature → landed slot → in-block index; idempotent)
- Analysis outputs: `reports/*.png`, `reports/summary.csv`, `data/analysis.json`

## Reproduce
```bash
pip install -r requirements.txt
# inspect dry-run
python3 measure.py --help
# live run — requires keypair + SOL budget
python3 measure.py --live --spend-cap-sol 0.05
# reconcile landed sigs against RPC
python3 reconcile.py --all --rebuild
# analyze ordering
python3 analyze.py
```

## Limitations
- Sample sizes shown per client; small-sample clients (Rakurai n=3) are pilots.
- Public RPC rate limits mean some `getBlock` calls require backoff/retry — reconcile script is idempotent and safe to re-run.
- We exclude Jito bundles and tips from scope (separate mechanism from leader scheduler, per bounty rules).

## License
MIT
