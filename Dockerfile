FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY measure.py reconcile.py analyze.py ./
# data snapshot + evidence produced by the study (no secrets — keypair is
# supplied at run time via volume/env, never baked into the image)
COPY data/ ./data/
COPY evidence/ ./evidence/
COPY reports/ ./reports/

ENV SOLANA_RPC=https://api.mainnet-beta.solana.com

# DEFAULT IS DRY-RUN: builds + simulates txs, sends nothing.
# Live measurement (opt-in, hard spend-cap enforced):
#   docker run --rm -v /path/to/keypair.json:/key/id.json solana-txo \
#     python measure.py --live --spend-cap-sol 0.05 \
#     --keypair /key/id.json --txs-per-window 9 --max-windows 30
CMD ["python", "measure.py"]
