import json
import os
import re
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

import pandas as pd
import psycopg
import yaml
from dotenv import load_dotenv


load_dotenv()

PG_DSN = os.getenv("ETH_PG_DSN")
RPC_URL = os.getenv("ETH_RPC_URL")
RPC_TIMEOUT = int(os.getenv("RPC_TIMEOUT", "30"))
CONFIG_PATH = Path("config/tokens.yaml")
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)


BUCKETS_SQL = """
CASE
    WHEN amount_normalized < 0.01 THEN '<0.01'
    WHEN amount_normalized < 0.1 THEN '0.01-0.1'
    WHEN amount_normalized < 1 THEN '0.1-1'
    WHEN amount_normalized < 10 THEN '1-10'
    WHEN amount_normalized < 100 THEN '10-100'
    WHEN amount_normalized < 1000 THEN '100-1000'
    WHEN amount_normalized < 10000 THEN '1000-10000'
    WHEN amount_normalized < 100000 THEN '10000-100000'
    WHEN amount_normalized < 1000000 THEN '100000-1000000'
    WHEN amount_normalized < 10000000 THEN '1000000-10000000'
    ELSE '>=10000000'
END
"""


def clean_address(address: str) -> str:
    cleaned = address.strip().lower()
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]

    if not re.fullmatch(r"[0-9a-f]{40}", cleaned):
        raise ValueError(f"Invalid Ethereum address: {address}")

    return cleaned


def safe_name(symbol: str) -> str:
    name = symbol.strip().lower().replace("-", "_")
    if not re.fullmatch(r"[a-z0-9_]+", name):
        raise ValueError(
            f"Unsafe token symbol {symbol!r}; only letters, numbers, '_' and '-' are supported"
        )
    return name


def transfer_view_name(symbol: str) -> str:
    return f"{safe_name(symbol)}_transfers"


def filtered_view_name(symbol: str, transfer_filter: str) -> str:
    return f"{safe_name(symbol)}_transfers_{transfer_filter}"


def get_onchain_decimals(address: str) -> int:
    """Read ERC-20 decimals() from the configured Ethereum RPC node."""
    if not RPC_URL:
        raise RuntimeError("ETH_RPC_URL is not set")

    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {
                "to": "0x" + clean_address(address),
                "data": "0x313ce567",  # decimals()
            },
            "latest",
        ],
    }).encode("utf-8")

    req = urllib_request.Request(
        RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib_request.urlopen(req, timeout=RPC_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib_error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not read decimals for {address} from ETH_RPC_URL"
        ) from exc

    if "error" in result:
        raise RuntimeError(
            f"RPC error while reading decimals for {address}: {result['error']}"
        )

    raw = result.get("result")
    if not raw or raw == "0x":
        raise RuntimeError(
            f"Contract {address} returned no value for decimals()"
        )

    return int(raw, 16)


def verify_token_decimals(symbol: str, address: str, expected: int) -> None:
    """Fail before aggregation when YAML decimals do not match the contract."""
    actual = get_onchain_decimals(address)
    if actual != expected:
        raise RuntimeError(
            f"{symbol}: YAML decimals={expected}, but contract decimals()={actual}"
        )
    print(f"[verify] {symbol} decimals={actual}")


def ensure_token_in_database(conn, symbol: str, address: str) -> int:
    """Return token.id and fail clearly if the address is absent from token."""
    addr_hex = clean_address(address)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM public.token
            WHERE addr = decode(%s, 'hex')
            """,
            (addr_hex,),
        )
        rows = cur.fetchall()

    if not rows:
        raise RuntimeError(
            f"{symbol}: token address {address} is missing from public.token"
        )
    if len(rows) > 1:
        raise RuntimeError(
            f"{symbol}: token address {address} occurs more than once in public.token"
        )

    return rows[0][0]


def ensure_materialized_view_exists(conn, view: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_matviews
                WHERE schemaname = 'public'
                  AND matviewname = %s
            )
            """,
            (view,),
        )
        exists = cur.fetchone()[0]

    if not exists:
        raise RuntimeError(
            f"Required materialized view public.{view} does not exist. "
            "Set setup_token_view: true for this token."
        )


def run_sql(conn, sql: str, params=None) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()


def export_query(conn, sql: str, output_path: Path) -> None:
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = [column.name for column in cur.description]
        rows = cur.fetchall()

    # Aggregation outputs are small enough to materialize in memory.
    df = pd.DataFrame(rows, columns=columns)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)

    # Release the read snapshot and ACCESS SHARE locks before the next analysis.
    conn.commit()
    print(f"[export] {output_path} ({len(df):,} rows)")


def setup_token_view(conn, symbol: str, address: str) -> None:
    view = transfer_view_name(symbol)
    token_id = ensure_token_in_database(conn, symbol, address)

    sql = f"""
    CREATE MATERIALIZED VIEW IF NOT EXISTS public.{view} AS
    SELECT *
    FROM public.erc20_transfer
    WHERE token_id = %s;
    """

    run_sql(conn, sql, (token_id,))

    index_sql = f"""
    CREATE INDEX IF NOT EXISTS idx_{view}_tx
    ON public.{view} (block_number, tx_index);

    CREATE INDEX IF NOT EXISTS idx_{view}_from
    ON public.{view} (from_id);

    CREATE INDEX IF NOT EXISTS idx_{view}_to
    ON public.{view} (to_id);
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

def monthly_summary(
    conn,
    symbol: str,
    view: str,
    decimals: int,
    transfer_filter: str,
) -> None:
    """
    Export one row per month containing:

    - Token transfer volume
    - ERC-20 Transfer event count
    - Distinct Ethereum transaction count
    - Unique active addresses
    - Addresses appearing for the first time
    """
    s = safe_name(symbol)

    sql = f"""
    WITH zero_address AS (
        SELECT id
        FROM address
        WHERE addr = decode(repeat('00', 20), 'hex')
    ),

    monthly_transfers AS (
        SELECT
            date_trunc('month', b.ts)::date AS month,

            -- Number of ERC-20 Transfer events
            COUNT(*) AS transfer_count,

            -- Number of distinct Ethereum transactions containing
            -- at least one token Transfer event
            COUNT(
                DISTINCT (t.block_number, t.tx_index)
            ) AS transaction_count,

            -- Volume expressed in whole tokens rather than raw units
            SUM(t.amount) /
                POWER(10::numeric, {decimals}) AS token_volume

        FROM {view} t
        JOIN eth_block b
          ON b.block_number = t.block_number
        GROUP BY 1
    ),

    participant_events AS (
        SELECT
            date_trunc('month', b.ts)::date AS month,
            t.from_id AS address_id
        FROM {view} t
        JOIN eth_block b
          ON b.block_number = t.block_number

        UNION ALL

        SELECT
            date_trunc('month', b.ts)::date AS month,
            t.to_id AS address_id
        FROM {view} t
        JOIN eth_block b
          ON b.block_number = t.block_number
    ),

    cleaned_participants AS (
        SELECT
            month,
            address_id
        FROM participant_events
        WHERE address_id IS NOT NULL
          AND address_id NOT IN (
              SELECT id
              FROM zero_address
          )
    ),

    monthly_active_addresses AS (
        SELECT
            month,
            COUNT(DISTINCT address_id) AS unique_addresses
        FROM cleaned_participants
        GROUP BY month
    ),

    address_first_seen AS (
        SELECT
            address_id,
            MIN(month) AS first_seen_month
        FROM cleaned_participants
        GROUP BY address_id
    ),

    monthly_new_addresses AS (
        SELECT
            first_seen_month AS month,
            COUNT(*) AS new_addresses
        FROM address_first_seen
        GROUP BY first_seen_month
    )

    SELECT
        mt.month,
        mt.token_volume,
        maa.unique_addresses,
        COALESCE(mna.new_addresses, 0) AS new_addresses,
        mt.transaction_count,
        mt.transfer_count
    FROM monthly_transfers mt
    JOIN monthly_active_addresses maa
      ON maa.month = mt.month
    LEFT JOIN monthly_new_addresses mna
      ON mna.month = mt.month
    ORDER BY mt.month;
    """

    export_query(
        conn,
        sql,
        OUTPUT_DIR / f"{s}_{transfer_filter}_monthly_summary.csv",
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
        raise RuntimeError("Missing ETH_PG_DSN in .env")

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    settings = config.get("settings", {})
    expected_database = settings.get("expected_database", "ethereum1")
    verify_decimals = settings.get("verify_decimals", True)

    if verify_decimals and not RPC_URL:
        raise RuntimeError(
            "settings.verify_decimals is true, but ETH_RPC_URL is missing in .env"
        )

    tokens = config.get("tokens", {})
    if not tokens:
        raise RuntimeError("No tokens found in config/tokens.yaml")

    with psycopg.connect(PG_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    current_database(),
                    current_user,
                    inet_server_addr()::text,
                    inet_server_port()
                """
            )
            database, user, host, port = cur.fetchone()
        conn.commit()

        print(
            f"[db] database={database} user={user} "
            f"host={host} port={port}"
        )

        if expected_database and database != expected_database:
            raise RuntimeError(
                f"Refusing to run: expected database {expected_database!r}, "
                f"but connected to {database!r}"
            )

        enabled_tokens = [
            (symbol, cfg)
            for symbol, cfg in tokens.items()
            if cfg.get("enabled", False)
        ]

        for symbol, cfg in tokens.items():
            if not cfg.get("enabled", False):
                print(f"[skip] {symbol}")

        print(f"[preflight] validating {len(enabled_tokens)} enabled tokens")

        # Validate every token before the first expensive materialized-view build.
        for symbol, cfg in enabled_tokens:
            decimals = int(cfg["decimals"])
            address = cfg["address"]
            transfer_filter = cfg.get("transfer_filter", "all")

            if verify_decimals:
                verify_token_decimals(symbol, address, decimals)

            ensure_token_in_database(conn, symbol, address)

            # Existing base views must be present when setup is disabled.
            if not cfg.get("setup_token_view", False):
                base_view = transfer_view_name(symbol)
                ensure_materialized_view_exists(conn, base_view)

                if transfer_filter == "eoa_eoa":
                    filtered_view = filtered_view_name(symbol, transfer_filter)
                    ensure_materialized_view_exists(conn, filtered_view)

        print("[preflight] all enabled tokens passed")

        for symbol, cfg in enabled_tokens:
            decimals = int(cfg["decimals"])
            address = cfg["address"]
            transfer_filter = cfg.get("transfer_filter", "all")
            analyses = cfg.get("analyses", {})

            print(f"\n=== {symbol} | filter={transfer_filter} ===")

            if cfg.get("setup_token_view", False):
                setup_token_view(conn, symbol, address)

            view = setup_filtered_view(conn, symbol, transfer_filter)
            ensure_materialized_view_exists(conn, view)

            if analyses.get("monthly_activity", False):
                monthly_activity(conn, symbol, view, decimals, transfer_filter)

            if analyses.get("monthly_summary", False):
                monthly_summary(
                    conn,
                    symbol,
                    view,
                    decimals,
                    transfer_filter,
                )

            if analyses.get("monthly_adoption_and_funded_by", False):
                monthly_adoption_and_funded_by(
                    conn,
                    symbol,
                    view,
                    decimals,
                    transfer_filter,
                )

            if analyses.get("monthly_top_users", False):
                monthly_top_users(conn, symbol, view, decimals, transfer_filter)

            if analyses.get("transfer_size_histogram_all_time", False):
                transfer_size_histogram_all_time(
                    conn,
                    symbol,
                    view,
                    decimals,
                    transfer_filter,
                )

            if analyses.get("monthly_transfer_size_buckets", False):
                monthly_transfer_size_buckets(
                    conn,
                    symbol,
                    view,
                    decimals,
                    transfer_filter,
                )


if __name__ == "__main__":
    run_pipeline()