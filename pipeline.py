import os
from pathlib import Path

import pandas as pd
import psycopg
import yaml
from dotenv import load_dotenv


load_dotenv()

PG_DSN = os.getenv("PG_DSN")
CONFIG_PATH = Path("config/tokens.yaml")
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)


BUCKETS_SQL = """
CASE
    WHEN amount_normalized < 0.01 THEN '<0.01'
    WHEN amount_normalized >= 0.01 AND amount_normalized < 0.1 THEN '0.01-0.1'
    WHEN amount_normalized >= 0.1 AND amount_normalized < 1 THEN '0.1-1'
    WHEN amount_normalized >= 1 AND amount_normalized < 10 THEN '1-10'
    WHEN amount_normalized >= 10 AND amount_normalized < 100 THEN '10-100'
    WHEN amount_normalized >= 100 AND amount_normalized < 1000 THEN '100-1000'
    WHEN amount_normalized >= 1000 AND amount_normalized < 10000 THEN '1000-10000'
    WHEN amount_normalized >= 10000 AND amount_normalized < 100000 THEN '10000-100000'
    ELSE '>=100000'
END
"""


def clean_address(address: str) -> str:
    return address.lower().replace("0x", "")


def safe_name(symbol: str) -> str:
    return symbol.lower().replace("-", "_")


def transfer_view_name(symbol: str) -> str:
    return f"{safe_name(symbol)}_transfers"


def filtered_view_name(symbol: str, transfer_filter: str) -> str:
    return f"{safe_name(symbol)}_transfers_{transfer_filter}"


def run_sql(conn, sql: str, params=None) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()


def export_query(conn, sql: str, output_path: Path) -> None:
    df = pd.read_sql(sql, conn)
    df.to_csv(output_path, index=False)
    print(f"[export] {output_path}")


def setup_token_view(conn, symbol: str, address: str) -> None:
    view = transfer_view_name(symbol)
    addr_hex = clean_address(address)

    sql = f"""
    CREATE MATERIALIZED VIEW IF NOT EXISTS {view} AS
    SELECT *
    FROM erc20_transfer
    WHERE token_id = (
        SELECT id
        FROM token
        WHERE addr = decode(%s, 'hex')
    );
    """

    run_sql(conn, sql, (addr_hex,))

    index_sql = f"""
    CREATE INDEX IF NOT EXISTS idx_{view}_tx
    ON {view} (block_number, tx_index);

    CREATE INDEX IF NOT EXISTS idx_{view}_from
    ON {view} (from_id);

    CREATE INDEX IF NOT EXISTS idx_{view}_to
    ON {view} (to_id);
    """

    run_sql(conn, index_sql)
    print(f"[setup] {view}")


def setup_filtered_view(conn, symbol: str, transfer_filter: str) -> str:
    base_view = transfer_view_name(symbol)

    if transfer_filter == "all":
        return base_view

    if transfer_filter != "eoa_eoa":
        raise ValueError(f"Unknown transfer_filter: {transfer_filter}")

    view = filtered_view_name(symbol, transfer_filter)

    sql = f"""
    CREATE MATERIALIZED VIEW IF NOT EXISTS {view} AS
    SELECT t.*
    FROM {base_view} t
    JOIN address a_from
      ON t.from_id = a_from.id
    JOIN address a_to
      ON t.to_id = a_to.id
    WHERE a_from.is_contract IS FALSE
      AND a_to.is_contract IS FALSE;
    """

    run_sql(conn, sql)

    index_sql = f"""
    CREATE INDEX IF NOT EXISTS idx_{view}_tx
    ON {view} (block_number, tx_index);

    CREATE INDEX IF NOT EXISTS idx_{view}_from
    ON {view} (from_id);

    CREATE INDEX IF NOT EXISTS idx_{view}_to
    ON {view} (to_id);
    """

    run_sql(conn, index_sql)
    print(f"[setup] {view}")

    return view


def monthly_activity(conn, symbol: str, view: str, decimals: int, transfer_filter: str) -> None:
    s = safe_name(symbol)

    sql = f"""
    WITH monthly_base AS (
        SELECT
            date_trunc('month', b.ts)::date AS month,
            COUNT(*) AS transfer_count,
            SUM(t.amount) AS raw_volume,
            SUM(t.amount) / POWER(10, {decimals}) AS token_volume,
            COUNT(DISTINCT t.from_id) AS unique_senders,
            COUNT(DISTINCT t.to_id) AS unique_receivers
        FROM {view} t
        JOIN eth_block b
          ON t.block_number = b.block_number
        GROUP BY 1
    ),
    monthly_active AS (
        SELECT
            month,
            COUNT(DISTINCT address_id) AS active_addresses
        FROM (
            SELECT
                date_trunc('month', b.ts)::date AS month,
                t.from_id AS address_id
            FROM {view} t
            JOIN eth_block b
              ON t.block_number = b.block_number

            UNION ALL

            SELECT
                date_trunc('month', b.ts)::date AS month,
                t.to_id AS address_id
            FROM {view} t
            JOIN eth_block b
              ON t.block_number = b.block_number
        ) x
        GROUP BY month
    )
    SELECT
        b.month,
        b.transfer_count,
        b.raw_volume,
        b.token_volume,
        b.unique_senders,
        b.unique_receivers,
        a.active_addresses
    FROM monthly_base b
    JOIN monthly_active a
      ON b.month = a.month
    ORDER BY b.month;
    """

    export_query(
        conn,
        sql,
        OUTPUT_DIR / f"{s}_{transfer_filter}_monthly_activity.csv"
    )


def monthly_adoption_and_funded_by(conn, symbol: str, view: str, decimals: int, transfer_filter: str) -> None:
    s = safe_name(symbol)

    adoption_sql = f"""
    WITH first_seen AS (
        SELECT
            participant_id AS address_id,
            MIN(b.ts) AS first_seen_ts
        FROM (
            SELECT block_number, tx_index, from_id AS participant_id FROM {view}
            UNION ALL
            SELECT block_number, tx_index, to_id AS participant_id FROM {view}
        ) p
        JOIN eth_block b
          ON p.block_number = b.block_number
        GROUP BY participant_id
    )
    SELECT
        date_trunc('month', first_seen_ts)::date AS month,
        COUNT(*) AS newly_adopted_addresses
    FROM first_seen
    GROUP BY 1
    ORDER BY 1;
    """

    export_query(
        conn,
        adoption_sql,
        OUTPUT_DIR / f"{s}_{transfer_filter}_monthly_adoption.csv"
    )

    funded_by_sql = f"""
    WITH incoming_ranked AS (
        SELECT
            t.to_id AS new_address_id,
            t.from_id AS funded_by_id,
            t.amount,
            b.ts,
            ROW_NUMBER() OVER (
                PARTITION BY t.to_id
                ORDER BY b.ts, t.block_number, t.tx_index, t.log_index
            ) AS rn
        FROM {view} t
        JOIN eth_block b
          ON t.block_number = b.block_number
    ),
    first_incoming AS (
        SELECT *
        FROM incoming_ranked
        WHERE rn = 1
    ),
    monthly_funders AS (
        SELECT
            date_trunc('month', ts)::date AS month,
            funded_by_id,
            COUNT(*) AS newly_funded_addresses,
            SUM(amount) AS raw_funded_volume,
            SUM(amount) / POWER(10, {decimals}) AS funded_volume,
            ROW_NUMBER() OVER (
                PARTITION BY date_trunc('month', ts)::date
                ORDER BY COUNT(*) DESC, SUM(amount) DESC
            ) AS funder_rank
        FROM first_incoming
        GROUP BY 1, funded_by_id
    )
    SELECT
        m.month,
        m.funder_rank,
        m.funded_by_id,
        encode(a.addr, 'hex') AS funded_by_address,
        m.newly_funded_addresses,
        m.raw_funded_volume,
        m.funded_volume
    FROM monthly_funders m
    JOIN address a
      ON m.funded_by_id = a.id
    WHERE m.funder_rank <= 100
    ORDER BY m.month, m.funder_rank;
    """

    export_query(
        conn,
        funded_by_sql,
        OUTPUT_DIR / f"{s}_{transfer_filter}_monthly_top100_funded_by.csv"
    )


def monthly_top_users(conn, symbol: str, view: str, decimals: int, transfer_filter: str) -> None:
    s = safe_name(symbol)

    sql = f"""
    WITH monthly_users AS (
        SELECT
            date_trunc('month', b.ts)::date AS month,
            t.from_id AS address_id,
            COUNT(*) AS outgoing_transfer_count,
            SUM(t.amount) AS raw_outgoing_volume,
            SUM(t.amount) / POWER(10, {decimals}) AS outgoing_volume,
            ROW_NUMBER() OVER (
                PARTITION BY date_trunc('month', b.ts)::date
                ORDER BY SUM(t.amount) DESC
            ) AS user_rank
        FROM {view} t
        JOIN eth_block b
          ON t.block_number = b.block_number
        GROUP BY 1, t.from_id
    )
    SELECT
        m.month,
        m.user_rank,
        m.address_id,
        encode(a.addr, 'hex') AS address,
        m.outgoing_transfer_count,
        m.raw_outgoing_volume,
        m.outgoing_volume
    FROM monthly_users m
    JOIN address a
      ON m.address_id = a.id
    WHERE m.user_rank <= 100
    ORDER BY m.month, m.user_rank;
    """

    export_query(
        conn,
        sql,
        OUTPUT_DIR / f"{s}_{transfer_filter}_monthly_top100_users.csv"
    )


def transfer_size_histogram_all_time(conn, symbol: str, view: str, decimals: int, transfer_filter: str) -> None:
    s = safe_name(symbol)

    sql = f"""
    WITH normalized AS (
        SELECT
            amount / POWER(10, {decimals}) AS amount_normalized
        FROM {view}
        WHERE amount > 0
    )
    SELECT
        {BUCKETS_SQL} AS size_bucket,
        COUNT(*) AS transfer_count,
        SUM(amount_normalized) AS bucket_volume
    FROM normalized
    GROUP BY 1
    ORDER BY
        MIN(amount_normalized);
    """

    export_query(
        conn,
        sql,
        OUTPUT_DIR / f"{s}_{transfer_filter}_transfer_size_histogram_all_time.csv"
    )


def monthly_transfer_size_buckets(conn, symbol: str, view: str, decimals: int, transfer_filter: str) -> None:
    s = safe_name(symbol)

    sql = f"""
    WITH normalized AS (
        SELECT
            date_trunc('month', b.ts)::date AS month,
            t.amount / POWER(10, {decimals}) AS amount_normalized
        FROM {view} t
        JOIN eth_block b
          ON t.block_number = b.block_number
        WHERE t.amount > 0
    )
    SELECT
        month,
        {BUCKETS_SQL} AS size_bucket,
        COUNT(*) AS transfer_count,
        SUM(amount_normalized) AS bucket_volume
    FROM normalized
    GROUP BY 1, 2
    ORDER BY 1, MIN(amount_normalized);
    """

    export_query(
        conn,
        sql,
        OUTPUT_DIR / f"{s}_{transfer_filter}_monthly_transfer_size_buckets.csv"
    )


def run_pipeline() -> None:
    if not PG_DSN:
        raise RuntimeError("Missing PG_DSN in .env")

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    with psycopg.connect(PG_DSN) as conn:
        for symbol, cfg in config["tokens"].items():
            if not cfg.get("enabled", False):
                print(f"[skip] {symbol}")
                continue

            decimals = cfg["decimals"]
            address = cfg["address"]
            transfer_filter = cfg.get("transfer_filter", "all")
            analyses = cfg.get("analyses", {})

            print(f"\n=== {symbol} | filter={transfer_filter} ===")

            if cfg.get("setup_token_view", False):
                setup_token_view(conn, symbol, address)

            view = setup_filtered_view(conn, symbol, transfer_filter)

            if analyses.get("monthly_activity", False):
                monthly_activity(conn, symbol, view, decimals, transfer_filter)

            if analyses.get("monthly_adoption_and_funded_by", False):
                monthly_adoption_and_funded_by(conn, symbol, view, decimals, transfer_filter)

            if analyses.get("monthly_top_users", False):
                monthly_top_users(conn, symbol, view, decimals, transfer_filter)

            if analyses.get("transfer_size_histogram_all_time", False):
                transfer_size_histogram_all_time(conn, symbol, view, decimals, transfer_filter)

            if analyses.get("monthly_transfer_size_buckets", False):
                monthly_transfer_size_buckets(conn, symbol, view, decimals, transfer_filter)


if __name__ == "__main__":
    run_pipeline()