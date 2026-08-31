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
    └── analysis_output/
        └── final/                 # Numerical outputs underlying thesis results
```

## Data

The raw blockchain databases are **not included** in this repository because of their size.

- Ethereum raw data were collected from a self-hosted Ethereum node and stored in PostgreSQL.
- BNB Smart Chain data were collected through Envio and stored in PostgreSQL.
- The thesis analysis period ends on **30 June 2026**.

The repository instead contains the final aggregate CSV exports used by the plotting pipeline:

- `output/final/` — Ethereum aggregate data
- `bsc/output/` — BNB Smart Chain aggregate data
- `plots/analysis_output/final/` — derived numerical tables used for the thesis analysis

The first two directories are the main reusable datasets. They are sufficient to regenerate the thesis plots and derived pre/post-GENIUS tables without querying the raw databases.

## CSV data dictionary

### File naming convention

Aggregate files follow the pattern

```text
<asset>_<transfer-filter>_<file-family>.csv
```

For example:

```text
usdt_all_monthly_activity.csv
usdc_eoa_eoa_monthly_activity.csv
dai_sc_sc_monthly_transfer_size_buckets.csv
```

Ethereum stablecoins may contain the following transfer filters:

| Filter | Meaning |
|---|---|
| `all` | All economic transfers |
| `eoa_eoa` | EOA sender → EOA receiver |
| `eoa_sc` | EOA sender → smart-contract receiver |
| `sc_eoa` | Smart-contract sender → EOA receiver |
| `sc_sc` | Smart-contract sender → smart-contract receiver |

EOA/SC classification was performed only for Ethereum. BNB Smart Chain files therefore use the `all` transfer set only.

The EOA/SC classification is the thesis account classification and is not time-varying. Consequently, these subsets should be interpreted as interaction categories under that classification, rather than as a historical reconstruction of an address's contract status at every individual transfer.

### General conventions

The following conventions apply across the aggregate CSVs:

| Convention | Meaning |
|---|---|
| `month` | Calendar month represented by its first day in `YYYY-MM-01` format |
| `transfer_count` | Number of selected transfer events, not number of blockchain transactions |
| `transaction_count` | Number of distinct blockchain transactions containing the selected transfer events |
| `raw_*` columns | Amounts in the token's smallest on-chain integer unit before decimal normalization |
| normalized volume columns | Amounts in native token units after division by the token's configured decimals |
| address columns | Lower-case hexadecimal addresses, generally stored without the `0x` prefix in the aggregate exports |
| `*_id` columns | Internal PostgreSQL address identifiers; these are database-specific and should not be used to match addresses across chains |
| volume | Native token units, **not price-converted USD values** |

For example, `token_volume` for USDT is measured in USDT, for EURC in EURC, for WBTC/BTCB in BTC units, and for ETH in ETH units. A USD-pegged token's nominal token amount may be economically close to USD, but no historical price conversion is applied in these CSVs.

For ERC-20/BEP-20 token datasets, several transfer events can occur inside the same blockchain transaction, so `transfer_count` can be larger than `transaction_count`. For native Ethereum top-level transfers, one transfer corresponds to one transaction and the two counts are therefore identical.

Unless stated otherwise, the core token-transfer exports use successful transactions, exclude transfers involving the zero address, and therefore exclude minting and burning from ordinary user-activity measures. Mint and burn events are exported separately.

Core activity and adoption exports do **not** impose a positive-value or dust threshold. The transfer-size bucket exports, however, contain only transfers with a strictly positive amount. The later GENIUS analysis applies an additional `>= 0.01` token-unit filter to selected count-based metrics for USD-pegged stablecoins; that transformation is documented separately below.

CSV exports are event-driven, so some file families can omit months with no qualifying observations. Asset histories also begin at different dates. When creating a balanced monthly panel, explicitly construct the calendar index and distinguish between a month before an asset's observed activity and a month with zero qualifying events.

### `*_monthly_activity.csv`

**Grain:** one row per calendar month and asset/filter combination.

This is the main monthly activity dataset and is the best starting point for most later analysis.

| Column | Definition |
|---|---|
| `month` | Calendar month |
| `transfer_count` | Number of selected transfer events |
| `transaction_count` | Number of distinct transactions containing those transfer events |
| `raw_volume` | Sum of transferred raw integer amounts |
| `token_volume` | Sum of transferred amounts in native token units |
| `unique_senders` | Number of distinct sender addresses during the month |
| `unique_receivers` | Number of distinct receiver addresses during the month |
| `active_addresses` | Number of distinct addresses appearing as sender or receiver during the month |

`active_addresses` is a union of senders and receivers; it is not the sum of `unique_senders` and `unique_receivers`.

### `*_monthly_summary.csv`

**Grain:** one row per calendar month and asset/filter combination.

This is a compact convenience table containing the most frequently used activity and adoption measures.

| Column | Definition |
|---|---|
| `month` | Calendar month |
| `token_volume` | Total transferred volume in native token units |
| `unique_addresses` | Distinct addresses appearing as sender or receiver during the month |
| `new_addresses` | Addresses whose first observed participation in this selected transfer set occurs in the month |
| `transaction_count` | Distinct transactions containing selected transfer events |
| `transfer_count` | Selected transfer events |

`unique_addresses` corresponds conceptually to `active_addresses` in `monthly_activity`.

### `*_monthly_adoption.csv`

**Grain:** one row per calendar month and asset/filter combination.

| Column | Definition |
|---|---|
| `month` | Calendar month |
| `newly_adopted_addresses` | Number of addresses first observed as either sender or receiver in the selected transfer set during that month |

For an `all` file, this is the first observed participation of the address in the token's economic-transfer dataset. For an EOA/SC subset file, it is the first observed participation **within that subset**, not necessarily the address's first-ever interaction with the token.

This metric is address-based and should not be interpreted as a count of unique human users.

### `*_monthly_top100_users.csv`

**Grain:** up to 100 sender addresses per month.

Addresses are ranked by their total outgoing transferred volume during the month.

| Column | Definition |
|---|---|
| `month` | Calendar month |
| `user_rank` | Monthly rank by outgoing volume; `1` is the largest sender |
| `address_id` | Internal database identifier of the sender |
| `address` | Sender address in hexadecimal form |
| `outgoing_transfer_count` | Number of selected outgoing transfer events from the address |
| `outgoing_transaction_count` | Number of distinct transactions containing those outgoing transfers |
| `raw_outgoing_volume` | Outgoing amount in raw integer units |
| `outgoing_volume` | Outgoing amount in native token units |

These are **monthly top-100 lists**, not an exhaustive address-level dataset.

### `*_monthly_top100_funded_by.csv`

**Grain:** up to 100 funding addresses per month.

For every receiving address, the pipeline identifies its first observed incoming transfer in the selected transfer set. Funding addresses are then ranked by how many such first-time recipients they funded during the month.

| Column | Definition |
|---|---|
| `month` | Calendar month of the recipients' first incoming transfers |
| `funder_rank` | Monthly rank by number of newly funded addresses |
| `funded_by_id` | Internal database identifier of the funding address |
| `funded_by_address` | Funding address in hexadecimal form |
| `newly_funded_addresses` | Number of addresses for which this funder supplied the first observed incoming transfer in the selected transfer set |
| `raw_funded_volume` | Sum of those first incoming transfers in raw integer units |
| `funded_volume` | Sum of those first incoming transfers in native token units |

`funded_volume` is therefore **not** the funder's total monthly outgoing volume. It includes only the first observed incoming transfers to the newly funded recipient addresses represented by this table.

For EOA/SC subset files, "first" is defined within the selected subset.

### `*_monthly_mint_burn.csv`

**Grain:** one row per month in which a mint or burn event is observed.

This file is generated for the complete token transfer set rather than the EOA/SC subsets.

Minting is defined as:

```text
zero address → non-zero address
```

Burning is defined as:

```text
non-zero address → zero address
```

| Column | Definition |
|---|---|
| `month` | Calendar month |
| `mint_event_count` | Number of zero-address mint transfer events |
| `mint_transaction_count` | Distinct transactions containing mint events |
| `unique_mint_recipients` | Distinct non-zero addresses receiving minted tokens |
| `raw_minted_amount` | Minted amount in raw integer units |
| `minted_volume` | Minted amount in native token units |
| `burn_event_count` | Number of burn transfer events |
| `burn_transaction_count` | Distinct transactions containing burn events |
| `unique_burn_senders` | Distinct non-zero addresses sending tokens to the zero address |
| `raw_burned_amount` | Burned amount in raw integer units |
| `burned_volume` | Burned amount in native token units |
| `net_issuance` | `minted_volume - burned_volume` for the month |
| `cumulative_net_issuance` | Cumulative sum of observed monthly net issuance |

`cumulative_net_issuance` is the cumulative value of observed zero-address issuance/destruction events in this dataset. It should not automatically be interpreted as the token's canonical circulating supply.

### `*_monthly_transfer_size_buckets.csv`

**Grain:** one row per calendar month and populated transfer-size bucket.

Only strictly positive transfers are included in this file.

| Column | Definition |
|---|---|
| `month` | Calendar month |
| `size_bucket` | Transfer-size interval in native token units |
| `transfer_count` | Positive transfer events falling into the bucket |
| `bucket_volume` | Total native-token volume of those transfer events |

The bucket boundaries are:

```text
<0.01
0.01-0.1
0.1-1
1-10
10-100
100-1000
1000-10000
10000-100000
100000-1000000
1000000-10000000
>=10000000
```

The intervals are defined in **token units**, not USD. For example, the `<0.01` bucket means less than `0.01 ETH` for ETH and less than `0.01 USDT` for USDT.

### `*_transfer_size_histogram_all_time.csv`

**Grain:** one row per populated transfer-size bucket over the full observed asset/filter history.

| Column | Definition |
|---|---|
| `size_bucket` | Transfer-size interval in native token units |
| `transfer_count` | Positive transfer events in the bucket over the full history |
| `bucket_volume` | Total native-token volume in the bucket over the full history |

This uses the same positive-transfer restriction and bucket boundaries as the monthly transfer-size file.

### Which aggregate file should I use?

For most paper work:

| Question | Recommended file |
|---|---|
| Monthly volume / transfer events / transactions | `*_monthly_activity.csv` |
| Monthly active addresses | `*_monthly_activity.csv` |
| First-time participation | `*_monthly_adoption.csv` |
| Compact monthly panel | `*_monthly_summary.csv` |
| Largest monthly senders | `*_monthly_top100_users.csv` |
| Addresses funding first-time recipients | `*_monthly_top100_funded_by.csv` |
| Minting / burning | `*_monthly_mint_burn.csv` |
| Transfer-size composition through time | `*_monthly_transfer_size_buckets.csv` |
| Full-history transfer-size distribution | `*_transfer_size_histogram_all_time.csv` |

## Derived analysis CSVs

The files under `plots/analysis_output/final/` are derived from the aggregate datasets above by `plots/plot_pipeline.py`. They preserve the exact numerical inputs and outputs underlying the thesis figures.

### Chain-level GENIUS files

These exist separately under:

```text
plots/analysis_output/final/ethereum/
plots/analysis_output/final/bsc/
```

#### `genius_monthly_metric_inputs.csv`

One row per month and asset used in the GENIUS analysis.

Important columns include:

| Column | Definition |
|---|---|
| `token_volume` | Full monthly transfer volume |
| `raw_transfer_count` | Transfer-event count before the GENIUS sub-cent treatment |
| `transfer_count` | Transfer-event count actually used in the GENIUS calculations |
| `genius_excluded_transfer_count` | Difference between raw and used transfer-event count |
| `genius_qualifying_transfer_volume` | Volume of transfers in the qualifying transfer population used for average transfer size |
| `genius_dust_filter_applied` | Whether the USD-stablecoin sub-cent rule was applied |
| `active_addresses` | Monthly active addresses |
| `newly_adopted_addresses` | Monthly first-time participants |
| `average_transfer_size` | Qualifying transfer volume divided by qualifying transfer-event count |
| `volume_per_active_address` | Full transfer volume divided by active addresses |
| `transfers_per_active_address` | Qualifying transfer-event count divided by active addresses |

#### `genius_pre_post_metric_changes.csv`

Wide-format output with one row per asset.

For each metric, the file stores:

```text
<metric>                  percentage change
<metric>_pre_mean         pre-GENIUS monthly mean
<metric>_post_mean        post-GENIUS monthly mean
<metric>_pct_change       percentage change
<metric>_pre_months       number of pre-period monthly observations
<metric>_post_months      number of post-period monthly observations
```

The bare `<metric>` column duplicates the percentage-change value and is retained because it is used directly to build the heatmap.

#### `genius_pre_post_monthly_average_levels.csv`

Long-format equivalent of the pre/post table, with one row per asset and metric.

| Column | Definition |
|---|---|
| `symbol` | Asset |
| `genius_aligned` | Classification used by the thesis plotting configuration |
| `metric` | Internal metric name |
| `metric_label` | Human-readable metric label |
| `pre_mean` | Pre-GENIUS monthly average |
| `post_mean` | Post-GENIUS monthly average |
| `pct_change` | Percentage change from pre to post |
| `pre_months` | Number of monthly observations in the pre period |
| `post_months` | Number of monthly observations in the post period |
| `post_period` | Explicit post-period label used for that metric |
| `sub_cent_transfer_filter` | Whether the metric uses the sub-cent transfer treatment |

This long-format file is usually the most convenient source when quoting exact pre/post numbers in a paper.

### Reconstructed address-ranking files

`reconstructed_all_time_top100_senders.csv` and `reconstructed_all_time_top100_funders.csv` aggregate the retained **monthly top-100 files** across the full sample and then rank addresses by their summed values.

They are intentionally named *reconstructed*: they are reconstructed from monthly top-100 candidate lists, not recalculated from the complete raw address population.

The overlap matrices

```text
top_senders_overlap_top100.csv
top_funders_overlap_top100.csv
```

report the number of shared addresses between the reconstructed top-100 sets of each pair of assets. Diagonal values are therefore 100 when a complete top-100 set is available.

### Cross-chain GENIUS files

Files under

```text
plots/analysis_output/final/cross_chain/
```

contain either separate Ethereum/BSC rows or pooled cross-chain values.

#### `genius_cross_chain_pre_post_metric_changes.csv`

One row per asset and chain. It contains the same seven GENIUS metrics as the chain-level pre/post file, together with their pre/post means and month counts.

#### `genius_cross_chain_pre_post_monthly_average_levels.csv`

Long-format version of the same Ethereum/BSC comparison, including `chain`, `symbol`, `metric`, pre/post means, percentage change, month counts, period label, and sub-cent-filter flag.

#### `genius_cross_chain_combined_monthly_metric_inputs.csv`

Monthly pooled Ethereum+BSC inputs for each cross-chain asset.

Additive quantities are combined across chains first. Address counts are summed across chains and are **not deduplicated across Ethereum and BSC**.

#### `genius_cross_chain_combined_monthly_metrics.csv`

Final monthly pooled metrics used for the combined analysis. Derived ratios are recalculated from the pooled underlying quantities rather than averaging the Ethereum and BSC ratios.

#### `genius_cross_chain_combined_pre_post_metric_changes.csv`

Wide-format pooled pre/post changes and pre/post monthly means.

#### `genius_cross_chain_combined_pre_post_monthly_average_levels.csv`

Long-format pooled pre/post values. The column

```text
address_counts_are_cross_chain_deduplicated
```

is `False` because the thesis does not attempt to identify whether an Ethereum address and a BSC address belong to the same real-world actor.

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

Importantly, this sub-cent treatment is applied by the **plotting/analysis pipeline**, not by rewriting the original aggregate CSVs. The aggregate files therefore preserve the original counts and volumes.

## Cross-chain benchmark mapping

For the cross-chain comparison:

- Ethereum native ETH is paired with Binance-Peg ETH on BSC and reported as **ETH**.
- WBTC on Ethereum is paired with BTCB on BSC and reported as **BTC**.

Transferred amounts remain expressed in the native units of the corresponding asset; benchmark transfer volume is therefore not USD-denominated.

Address counts are calculated independently per chain and summed in pooled cross-chain results. They are **not deduplicated across Ethereum and BSC** and should not be interpreted as unique cross-chain users.

## Plotting and analysis pipeline

The canonical plotting implementation is:

```text
plots/plot_pipeline.py
```

It generates the figures and numerical comparison tables used in the thesis, including monthly activity, adoption, transfer-size distributions, minting/burning, address concentration, EOA/SC composition, GENIUS pre/post analysis, and Ethereum/BSC comparisons.

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

### Ethereum-vs-BSC divergence heatmap only

```bash
python plots/plot_pipeline.py --config config/plot_config_divergence.yaml
```

## Aggregation pipelines

### Ethereum

```bash
python pipeline.py
```

The Ethereum aggregation pipeline queries the thesis PostgreSQL database and writes the aggregate exports consumed by the plotting pipeline.

Running it requires access to the original Ethereum database and the corresponding database configuration. The checked-in aggregate files in `output/final/` are the thesis snapshot and should normally be used when reproducing figures.

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
