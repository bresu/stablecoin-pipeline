# BSC aggregation pipeline

This folder is deliberately separate from the Ethereum pipeline.

## Why it is different

The BSC database is too large for the Ethereum strategy of building one
materialized view per token and then running several independent scans over each
view. `bep20_transfer` contains roughly 27 billion rows.

The BSC pipeline instead:

1. walks the chain in bounded block ranges;
2. uses the existing `(block_number, ...)` primary-key ordering;
3. extracts all configured BEP-20 assets together into one temporary chunk;
4. computes every downstream aggregate from that temporary chunk;
5. commits a checkpoint after each block range;
6. can resume automatically after a reboot;
7. exports the same `*_all_*.csv` file names and columns as the Ethereum
   `pipeline.py`.

No EOA/smart-contract classification is required for this first aggregation.

## Assets

Stablecoins:

- USDT (Binance-Peg BSC-USD / historical BEP20USDT contract)
- USDC (Binance-Peg USDC)
- USD1
- DAI (Binance-Peg DAI)
- USDe

Comparison assets:

- BTCB
- Binance-Peg ETH (`ETH` in CSV names)
- native BNB

Native BNB is built from successful top-level `transaction.value` transfers.
It does not include internal BNB movements from execution traces, matching the
top-level methodology used for native ETH in the Ethereum pipeline.

## Environment

Add to your `.env` (or export in the shell):

```bash
BSC_PG_DSN="postgresql://USER:PASSWORD@HOST:5432/bsc1"
```

Optional:

```bash
BSC_CONFIG="bsc/config/tokens.yaml"
BSC_OUTPUT_DIR="bsc/output"
```

## First run

From the repository root:

```bash
python bsc/pipeline.py status
```

This validates the DB schema/token addresses and creates empty analysis state.

Then aggregate the BEP-20 assets:

```bash
python bsc/pipeline.py tokens
```

Native BNB can be run separately:

```bash
python bsc/pipeline.py bnb
```

Or run everything and export CSVs:

```bash
python bsc/pipeline.py run
```

If the server reboots, run the same command again. Completed block chunks are
recorded in `analysis_bsc.progress` and skipped.

## Watch progress

```bash
python bsc/pipeline.py status
```

This also reports the exact number of unique economically relevant addresses
seen for each asset. That table is intentionally retained so a later EOA/SC
classification pass can operate on only the relevant address population.

From psql:

```sql
SELECT *
FROM analysis_bsc.progress
ORDER BY source, start_block DESC
LIMIT 20;
```

```sql
SELECT asset, COUNT(*) AS unique_addresses
FROM analysis_bsc.relevant_addresses
GROUP BY asset
ORDER BY unique_addresses DESC;
```

## Export only

Once aggregation is complete:

```bash
python bsc/pipeline.py export
```

The files are written under `bsc/output/` by default and preserve the Ethereum
pipeline's `all` CSV schemas:

- `<asset>_all_monthly_activity.csv`
- `<asset>_all_monthly_summary.csv`
- `<asset>_all_monthly_adoption.csv`
- `<asset>_all_monthly_top100_funded_by.csv`
- `<asset>_all_monthly_top100_users.csv`
- `<asset>_all_transfer_size_histogram_all_time.csv`
- `<asset>_all_monthly_transfer_size_buckets.csv`
- `<asset>_all_monthly_mint_burn.csv` (BEP-20 assets only)

This is the compatibility boundary with the existing plotting code.

## Reset

Only use this if you intentionally want to discard BSC aggregate state:

```bash
python bsc/pipeline.py reset
```

It drops `analysis_bsc` only. It never drops or modifies `public.block`,
`public.transaction`, `public.bep20_transfer`, `public.address`, or
`public.token`.

## Chunk size

The default is 100,000 blocks. Start there. Do **not** change chunk size after
aggregation has started: chunk boundaries are included in the state fingerprint
to prevent overlapping ranges from being double-counted. If you deliberately
want a different chunk size, run `reset` and rebuild from block 1.

The pipeline temporarily disables sequential scans only while extracting a raw
block range, preventing PostgreSQL from accidentally choosing a full scan of
the multi-terabyte raw table.
