# Stablecoin Pipeline

Code and aggregate datasets used for the bachelor thesis **“Stablecoin Usage, Liquidity, and Cross-Chain Flows in Response to Regulatory Change in EVM Chains”**.

The repository contains the final aggregation and plotting pipelines used for the thesis, together with the aggregate CSV exports required to reproduce the analysis without access to the multi-terabyte raw blockchain databases.

## Related resources

- **Interactive plots:** https://thesis.leandergoetz.eu/
- **Interactive plot repository:** https://github.com/bresu/stablecoin-plots-web

## Repository structure

```text
stablecoin-pipeline/
├── pipeline.py                    # Ethereum aggregation pipeline
├── requirements.txt
│
├── config/
│   ├── tokens.yaml                # Ethereum aggregation configuration
│   ├── plot_config.yaml           # Main/final plotting configuration
│   ├── plot_config_combined.yaml  # Pooled Ethereum + BSC GENIUS analysis
│   └── plot_config_divergence.yaml# Ethereum-vs-BSC divergence heatmap
│
├── output/
│   └── final/                     # Final Ethereum aggregate CSV exports
│
├── bsc/
│   ├── README.md
│   ├── pipeline.py                # BNB Smart Chain aggregation pipeline
│   ├── config/
│   │   └── tokens.yaml
│   └── output/                    # Final BSC aggregate CSV exports
│
└── plots/
    ├── plot_pipeline.py           # Final plotting + analysis pipeline
    ├── assets/
    │   ├── ethereum_logo.svg
    │   └── bnb_chain_logo.svg
    └── analysis_output/           # Numerical analysis outputs used for thesis results
```

## Data

The raw blockchain databases are **not included** in this repository because of their size.

- Ethereum raw data were collected from a self-hosted Ethereum node and stored in PostgreSQL.
- BNB Smart Chain data were collected through Envio and stored in PostgreSQL.
- The analysis period ends on **30 June 2026**.

The repository instead contains the final aggregate CSV exports used by the plotting pipeline:

- `output/final/` — Ethereum
- `bsc/output/` — BNB Smart Chain

These files are sufficient to regenerate the thesis plots and the pre/post-GENIUS analysis without querying the raw databases.

### Ethereum transfer categories

For Ethereum stablecoins, aggregate outputs are available for the complete transfer set and, where applicable, for four mutually exclusive sender/receiver account-type categories:

- `all`
- `eoa_eoa`
- `eoa_sc`
- `sc_eoa`
- `sc_sc`

EOA/SC classification was performed only for Ethereum. BSC results therefore use the complete transfer set only.

### Aggregate file families

Depending on the asset and transfer category, the aggregate exports include:

- monthly transfer activity
- monthly adoption / first-time participation
- monthly minting and burning
- monthly summary statistics
- monthly top senders
- monthly top funding addresses
- monthly transfer-size buckets
- all-time transfer-size histograms

The plotting pipeline expects these file names and schemas and reads them directly from the two aggregate output directories.

## Plotting and analysis pipeline

The canonical plotting implementation is:

```text
plots/plot_pipeline.py
```

It generates the figures and numerical comparison tables used in the thesis, including:

- monthly transfer volume, transfer-event count, and transaction count
- active-address and first-time-participation measures
- transfer-size distributions
- minting and burning
- activity-intensity metrics
- top-sender concentration
- Ethereum EOA/SC transfer composition
- pre/post-GENIUS comparisons
- Ethereum/BSC comparisons
- pooled cross-chain metrics
- cross-chain divergence analysis

The script accepts an alternative YAML config through `--config`.

### Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Static PDF/PNG export uses Plotly/Kaleido and may additionally require a working Chrome/Chromium installation. Interactive HTML output does not require static export to succeed.

### Main thesis plotting run

From the repository root:

```bash
python plots/plot_pipeline.py
```

This uses:

```text
config/plot_config.yaml
```

### Pooled Ethereum + BSC GENIUS analysis only

```bash
python plots/plot_pipeline.py --config config/plot_config_combined.yaml
```

This recalculates pooled Ethereum+BSC pre/post metrics from the underlying additive quantities rather than averaging chain-level ratios.

### Ethereum-vs-BSC divergence heatmap only

```bash
python plots/plot_pipeline.py --config config/plot_config_divergence.yaml
```

This generates the dedicated Ethereum-minus-BSC cross-chain divergence view.

## GENIUS pre/post comparison

The thesis uses the following comparison windows:

- **Pre-GENIUS:** January–June 2025
- **July 2025:** excluded because the GENIUS Act was enacted on 18 July 2025
- **Post-GENIUS, non-address metrics:** August 2025–January 2026
- **Post-GENIUS, address-dependent metrics:** August–December 2025

January 2026 is excluded from address-dependent post-period metrics because of the large increase in address-poisoning / dust activity observed on Ethereum. The same five-month address window is used for BSC for comparability.

### Sub-cent filtering

For USD-pegged stablecoins, transfer-count-dependent GENIUS metrics use transfers of at least `0.01` token units:

- transfer-event count
- average transfer size
- transfer events per active address

Transferred volume and volume per active address remain based on the full transferred volume.

The filter is not applied to non-USD-pegged assets or benchmark assets such as ETH/BTC.

## Cross-chain benchmark mapping

For the cross-chain comparison:

- Ethereum native ETH is paired with Binance-Peg ETH on BSC and reported as **ETH**.
- WBTC on Ethereum is paired with BTCB on BSC and reported as **BTC**.

Transferred amounts remain expressed in the native units of the corresponding asset; benchmark transfer volume is therefore not USD-denominated.

Address counts are calculated independently per chain and summed in pooled cross-chain results. They are **not deduplicated across Ethereum and BSC** and should not be interpreted as unique cross-chain users.

## Numerical analysis outputs

`plots/analysis_output/` contains the tabular outputs underlying the regulatory and cross-chain figures. These are useful when writing the paper because the exact values can be reused without reading them back from the heatmaps.

Important output families include:

```text
genius_monthly_metric_inputs.csv
genius_pre_post_metric_changes.csv
genius_pre_post_monthly_average_levels.csv

genius_cross_chain_pre_post_metric_changes.csv
genius_cross_chain_pre_post_monthly_average_levels.csv

genius_cross_chain_combined_monthly_metrics.csv
genius_cross_chain_combined_pre_post_metric_changes.csv
genius_cross_chain_combined_pre_post_monthly_average_levels.csv
```

The combined metrics are recalculated from pooled chain-level quantities; derived ratios are not obtained by averaging Ethereum and BSC ratios.

## Aggregation pipelines

### Ethereum

```bash
python pipeline.py
```

The Ethereum aggregation pipeline queries the thesis PostgreSQL database and writes the aggregate exports consumed by the plotting pipeline.

Running it requires access to the original Ethereum database and the corresponding database configuration. The checked-in aggregate files in `output/final/` are the final thesis snapshot and should normally be used when reproducing figures.

### BNB Smart Chain

See:

```text
bsc/README.md
```

The BSC pipeline similarly operates on the PostgreSQL database built from Envio data. The final aggregate exports are retained in `bsc/output/`.

## Important methodological notes

Unless stated otherwise, token-transfer metrics are calculated from transfer events belonging to successful transactions and exclude minting and burning events involving the zero address. Native ETH analysis uses successful top-level value transfers.

The thesis is descriptive rather than causal. The pre/post-GENIUS analysis measures changes around the enactment of the Act but does not identify a causal effect of regulation.

Blockchain addresses are not equivalent to individual users. A person or institution may control multiple addresses, while custodial or exchange addresses may represent many users.

## Generated plots

The plotting pipeline can generate interactive HTML plots and static PDF/PNG exports. The public interactive versions used during the thesis are hosted separately:

https://thesis.leandergoetz.eu/

The website source/output is maintained in:

https://github.com/bresu/stablecoin-plots-web

Keeping generated plots separate from this repository makes this repository primarily a reproducible code + aggregate-data archive.

## Thesis snapshot

This repository represents the **final thesis analysis snapshot ending 30 June 2026**.

Future paper work can extend the raw databases or regenerate aggregate outputs, but the checked-in CSVs and configs preserve the exact thesis-era data and analysis choices.
