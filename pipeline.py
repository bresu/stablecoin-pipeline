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


# Address-type filters supported by the pipeline.
# NULL classifications are excluded automatically because IS TRUE / IS FALSE
# only match classified addresses. The zero address is also excluded from all
# address-type filtered views because it is neither an EOA nor a smart contract.
#
# IMPORTANT ANALYSIS RULE:
# All main behavioural/activity analyses exclude ERC-20 mint and burn events,
# defined as Transfer events where either endpoint is the zero address. Minting
# and burning are aggregated separately by monthly_mint_burn(). The successful-
# transaction base views deliberately retain these events so issuance/destruction
# remains available for separate analysis.
FILTER_SPECS = {
    "eoa_eoa": (False, False),
    "eoa_sc": (False, True),
    "sc_eoa": (True, False),
    "sc_sc": (True, True),
}

BASE_VIEW_COMMENT = "stablecoin_pipeline:base_v2_success_true"
FILTER_VIEW_COMMENT_PREFIX = "stablecoin_pipeline:address_filter_v2_zero_excluded"
NATIVE_ETH_VIEW = "eth_native_transfers"
NATIVE_ETH_VIEW_COMMENT = "stablecoin_pipeline:native_eth_v1_success_true_value_positive_to_nonnull"


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


def materialized_view_exists(conn, view: str) -> bool:
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
        return bool(cur.fetchone()[0])


def ensure_materialized_view_exists(conn, view: str) -> None:
    if not materialized_view_exists(conn, view):
        raise RuntimeError(f"Required materialized view public.{view} does not exist")


def materialized_view_definition(conn, view: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_viewdef(%s::regclass, true)",
            (f"public.{view}",),
        )
        row = cur.fetchone()
    return row[0] if row else ""


def materialized_view_comment(conn, view: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT obj_description(%s::regclass, 'pg_class')",
            (f"public.{view}",),
        )
        row = cur.fetchone()
    return row[0] if row else None


def base_view_is_success_filtered(conn, view: str) -> bool:
    """Return True when an existing base view explicitly filters tx.success IS TRUE."""
    if not materialized_view_exists(conn, view):
        return False

    comment = materialized_view_comment(conn, view)
    if comment == BASE_VIEW_COMMENT:
        return True

    definition = materialized_view_definition(conn, view)
    normalized = re.sub(r"\s+", " ", definition).lower()
    return (
        "eth_tx" in normalized
        and re.search(r"\bsuccess\s+is\s+true\b", normalized) is not None
    )


def native_eth_view_is_current(conn, view: str = NATIVE_ETH_VIEW) -> bool:
    """Return True when the native ETH materialized view matches this pipeline version."""
    if not materialized_view_exists(conn, view):
        return False

    comment = materialized_view_comment(conn, view)
    if comment == NATIVE_ETH_VIEW_COMMENT:
        return True

    definition = materialized_view_definition(conn, view)
    normalized = re.sub(r"\s+", " ", definition).lower()
    return (
        "eth_tx" in normalized
        and re.search(r"\bsuccess\s+is\s+true\b", normalized) is not None
        and re.search(r"\bvalue\s*>\s*0\b", normalized) is not None
        and re.search(r"\bto_id\s+is\s+not\s+null\b", normalized) is not None
    )

def filter_view_is_current(conn, view: str, transfer_filter: str) -> bool:
    if not materialized_view_exists(conn, view):
        return False
    expected = f"{FILTER_VIEW_COMMENT_PREFIX}:{transfer_filter}"
    return materialized_view_comment(conn, view) == expected


def run_sql(conn, sql: str, params=None) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()


def export_query(conn, sql: str, output_path: Path) -> None:
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = [column.name for column in cur.description]
        rows = cur.fetchall()

    df = pd.DataFrame(rows, columns=columns)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)

    # Release the read snapshot and ACCESS SHARE locks before the next analysis.
    conn.commit()
    print(f"[export] {output_path} ({len(df):,} rows)")


def create_standard_view_indexes(conn, view: str) -> None:
    sql = f"""
    CREATE INDEX IF NOT EXISTS idx_{view}_tx
    ON public.{view} (block_number, tx_index);

    CREATE INDEX IF NOT EXISTS idx_{view}_from
    ON public.{view} (from_id);

    CREATE INDEX IF NOT EXISTS idx_{view}_to
    ON public.{view} (to_id);
    """
    run_sql(conn, sql)


def normalize_rebuild_mode(value) -> str:
    """
    Supported values:
      false / never      -> never drop an existing view
      true / always      -> always rebuild an existing view
      if_legacy          -> rebuild only if the view is not at the current schema version
    """
    if value is True:
        return "always"
    if value is False or value is None:
        return "never"

    mode = str(value).strip().lower()
    aliases = {
        "false": "never",
        "true": "always",
        "never": "never",
        "always": "always",
        "if_legacy": "if_legacy",
        "legacy": "if_legacy",
    }
    if mode not in aliases:
        raise ValueError(
            f"Invalid rebuild mode {value!r}. Use false, true, 'never', "
            "'always', or 'if_legacy'."
        )
    return aliases[mode]


def drop_known_filtered_views(conn, symbol: str) -> None:
    """
    Drop only the address-filtered materialized views managed by this pipeline.
    No CASCADE is used, so unknown dependencies cause a safe failure instead of
    being silently deleted.
    """
    for transfer_filter in FILTER_SPECS:
        view = filtered_view_name(symbol, transfer_filter)
        run_sql(conn, f"DROP MATERIALIZED VIEW IF EXISTS public.{view};")


def migrate_legacy_base_view(conn, symbol: str, view: str) -> None:
    """
    One-time migration path for a legacy token-specific materialized view.

    Instead of rescanning the entire erc20_transfer table, build the successful-
    transaction version from the already token-filtered legacy view, then swap it
    into place. The old view is only dropped after the replacement has been fully
    materialized, which also makes an interrupted migration safer.
    """
    tmp_view = f"{view}__success_tmp"

    # Clean up a temp view left by an interrupted earlier migration.
    run_sql(conn, f"DROP MATERIALIZED VIEW IF EXISTS public.{tmp_view};")

    sql = f"""
    CREATE MATERIALIZED VIEW public.{tmp_view} AS
    SELECT t.*
    FROM public.{view} t
    JOIN public.eth_tx tx
      ON tx.block_number = t.block_number
     AND tx.tx_index = t.tx_index
    WHERE tx.success IS TRUE;
    """
    print(
        f"[migrate] {view}: building success-filtered replacement from existing token view"
    )
    run_sql(conn, sql)

    # Only after the replacement exists do we remove managed dependants and swap.
    drop_known_filtered_views(conn, symbol)
    run_sql(conn, f"DROP MATERIALIZED VIEW public.{view};")
    run_sql(
        conn,
        f"ALTER MATERIALIZED VIEW public.{tmp_view} RENAME TO {view};",
    )
    run_sql(
        conn,
        f"COMMENT ON MATERIALIZED VIEW public.{view} IS '{BASE_VIEW_COMMENT}';",
    )


def setup_token_view(
    conn,
    symbol: str,
    address: str,
    setup_if_missing: bool,
    rebuild_mode: str,
    allow_destructive_rebuilds: bool,
    refresh_existing: bool = False,
) -> bool:
    """
    Ensure the base token materialized view exists and explicitly contains only
    transactions with eth_tx.success IS TRUE.

    Returns True if the base view changed in this run (created, rebuilt, migrated,
    or refreshed). A changed base view means any materialized address-filter views
    must also be refreshed/rebuilt before analysis.
    """
    view = transfer_view_name(symbol)
    token_id = int(ensure_token_in_database(conn, symbol, address))
    exists = materialized_view_exists(conn, view)
    current = base_view_is_success_filtered(conn, view) if exists else False

    if exists and rebuild_mode == "if_legacy" and not current:
        if not allow_destructive_rebuilds:
            raise RuntimeError(
                f"{symbol}: public.{view} is a legacy view and needs migration, but "
                "settings.allow_destructive_rebuilds is false."
            )
        migrate_legacy_base_view(conn, symbol, view)
        create_standard_view_indexes(conn, view)
        print(f"[base] {view} | success=true | migrated")
        return True

    if exists and rebuild_mode == "always":
        if not allow_destructive_rebuilds:
            raise RuntimeError(
                f"{symbol}: public.{view} rebuild requested, but "
                "settings.allow_destructive_rebuilds is false."
            )
        print(f"[rebuild] {view}: dropping managed filtered views first")
        drop_known_filtered_views(conn, symbol)
        run_sql(conn, f"DROP MATERIALIZED VIEW public.{view};")
        exists = False
        current = False

    if exists and not current:
        raise RuntimeError(
            f"{symbol}: public.{view} exists but does not explicitly filter "
            "eth_tx.success IS TRUE. Set rebuild_token_view: if_legacy "
            "(recommended) or true, and allow destructive rebuilds."
        )

    created = False
    if not exists:
        if not setup_if_missing and rebuild_mode == "never":
            raise RuntimeError(
                f"{symbol}: public.{view} is missing and setup_token_view is false"
            )

        sql = f"""
        CREATE MATERIALIZED VIEW public.{view} AS
        SELECT t.*
        FROM public.erc20_transfer t
        JOIN public.eth_tx tx
          ON tx.block_number = t.block_number
         AND tx.tx_index = t.tx_index
        WHERE t.token_id = {token_id}
          AND tx.success IS TRUE;
        """
        print(f"[build] {view}: successful transactions only")
        run_sql(conn, sql)
        run_sql(
            conn,
            f"COMMENT ON MATERIALIZED VIEW public.{view} IS '{BASE_VIEW_COMMENT}';",
        )
        created = True

    create_standard_view_indexes(conn, view)

    if not base_view_is_success_filtered(conn, view):
        raise RuntimeError(
            f"{symbol}: safety check failed; public.{view} is not marked/defined "
            "as successful-transactions-only"
        )

    if refresh_existing and not created:
        print(f"[refresh] {view}: incorporating latest base-table data")
        run_sql(conn, f"REFRESH MATERIALIZED VIEW public.{view};")
        print(f"[base] {view} | success=true | refreshed")
        return True

    print(f"[base] {view} | success=true | {'created/rebuilt' if created else 'reused'}")
    return created

def setup_native_eth_view(
    conn,
    setup_if_missing: bool,
    rebuild_mode: str,
    allow_destructive_rebuilds: bool,
    refresh_existing: bool = False,
) -> bool:
    """
    Build a compact materialized source for native ETH analysis.

    Included:
      - top-level transactions only
      - success IS TRUE
      - value > 0
      - non-NULL to_id (contract-creation transactions are excluded because the
        created contract address is not stored in eth_tx.to_id)

    The view aliases eth_tx.value to amount and provides log_index=0 so the same
    downstream aggregation functions used for ERC-20 transfers can be reused.
    """
    view = NATIVE_ETH_VIEW
    exists = materialized_view_exists(conn, view)
    current = native_eth_view_is_current(conn, view) if exists else False

    should_rebuild = False
    if exists and rebuild_mode == "always":
        should_rebuild = True
    elif exists and rebuild_mode == "if_legacy" and not current:
        should_rebuild = True

    if should_rebuild:
        if not allow_destructive_rebuilds:
            raise RuntimeError(
                f"Native ETH view public.{view} needs rebuild, but "
                "settings.allow_destructive_rebuilds is false."
            )
        print(f"[rebuild] {view}")
        run_sql(conn, f"DROP MATERIALIZED VIEW public.{view};")
        exists = False
        current = False

    if exists and not current:
        raise RuntimeError(
            f"public.{view} exists but does not match the current native ETH "
            "definition. Set native_eth.rebuild_view: if_legacy (recommended) "
            "or true."
        )

    created = False
    if not exists:
        if not setup_if_missing and rebuild_mode == "never":
            raise RuntimeError(
                f"public.{view} is missing and native_eth.setup_view is false"
            )

        sql = f"""
        CREATE MATERIALIZED VIEW public.{view} AS
        SELECT
            tx.block_number,
            tx.tx_index,
            0::integer AS log_index,
            tx.from_id,
            tx.to_id,
            tx.value AS amount
        FROM public.eth_tx tx
        WHERE tx.success IS TRUE
          AND tx.value > 0
          AND tx.to_id IS NOT NULL;
        """
        print(
            f"[build] {view}: successful top-level ETH value transfers; "
            "contract-creation tx excluded"
        )
        run_sql(conn, sql)
        run_sql(
            conn,
            f"COMMENT ON MATERIALIZED VIEW public.{view} IS '{NATIVE_ETH_VIEW_COMMENT}';",
        )
        created = True

    create_standard_view_indexes(conn, view)

    if not native_eth_view_is_current(conn, view):
        raise RuntimeError(
            f"Native ETH safety check failed for public.{view}"
        )

    if refresh_existing and not created:
        print(f"[refresh] {view}: incorporating latest eth_tx data")
        run_sql(conn, f"REFRESH MATERIALIZED VIEW public.{view};")
        print(f"[native] {view} | refreshed")
        return True

    print(f"[native] {view} | {'created/rebuilt' if created else 'reused'}")
    return created


def setup_filtered_view(
    conn,
    symbol: str,
    transfer_filter: str,
    rebuild_mode: str,
    allow_destructive_rebuilds: bool,
    force_refresh: bool = False,
) -> str:
    base_view = transfer_view_name(symbol)
    ensure_materialized_view_exists(conn, base_view)

    if transfer_filter == "all":
        return base_view

    if transfer_filter not in FILTER_SPECS:
        valid = ", ".join(["all", *FILTER_SPECS.keys()])
        raise ValueError(
            f"Unknown transfer_filter {transfer_filter!r}. Valid values: {valid}"
        )

    view = filtered_view_name(symbol, transfer_filter)
    exists = materialized_view_exists(conn, view)
    current = filter_view_is_current(conn, view, transfer_filter) if exists else False

    should_rebuild = False
    if exists and rebuild_mode == "always":
        should_rebuild = True
    elif exists and rebuild_mode == "if_legacy" and not current:
        should_rebuild = True

    if should_rebuild:
        if not allow_destructive_rebuilds:
            raise RuntimeError(
                f"{symbol}/{transfer_filter}: filtered view rebuild requested, but "
                "settings.allow_destructive_rebuilds is false."
            )
        print(f"[rebuild] {view}")
        run_sql(conn, f"DROP MATERIALIZED VIEW public.{view};")
        exists = False
        current = False

    if exists and not current and rebuild_mode == "never":
        raise RuntimeError(
            f"{view} predates the current address-filter definition. "
            "Set rebuild_filtered_views: if_legacy to migrate it once."
        )

    if not exists:
        from_is_contract, to_is_contract = FILTER_SPECS[transfer_filter]
        from_literal = "TRUE" if from_is_contract else "FALSE"
        to_literal = "TRUE" if to_is_contract else "FALSE"

        sql = f"""
        CREATE MATERIALIZED VIEW public.{view} AS
        SELECT t.*
        FROM public.{base_view} t
        JOIN public.address a_from
          ON t.from_id = a_from.id
        JOIN public.address a_to
          ON t.to_id = a_to.id
        WHERE a_from.is_contract IS {from_literal}
          AND a_to.is_contract IS {to_literal}
          AND a_from.addr <> decode(repeat('00', 20), 'hex')
          AND a_to.addr <> decode(repeat('00', 20), 'hex');
        """
        print(f"[build] {view}")
        run_sql(conn, sql)
        comment = f"{FILTER_VIEW_COMMENT_PREFIX}:{transfer_filter}"
        run_sql(
            conn,
            f"COMMENT ON MATERIALIZED VIEW public.{view} IS '{comment}';",
        )
        exists = True
        current = True
    elif force_refresh and current:
        print(f"[refresh] {view}: base view changed")
        run_sql(conn, f"REFRESH MATERIALIZED VIEW public.{view};")

    create_standard_view_indexes(conn, view)
    print(f"[filter] {view}")
    return view

def get_transfer_filters(cfg: dict) -> list[str]:
    """Support both the new transfer_filters list and the old transfer_filter scalar."""
    filters = cfg.get("transfer_filters")
    if filters is None:
        filters = [cfg.get("transfer_filter", "all")]
    elif isinstance(filters, str):
        filters = [filters]

    result = []
    for item in filters:
        value = str(item).strip().lower()
        if value not in ["all", *FILTER_SPECS.keys()]:
            raise ValueError(f"Unknown transfer filter {item!r}")
        if value not in result:
            result.append(value)
    return result


def ensure_address_classification_column(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'address'
                  AND column_name = 'is_contract'
            )
            """
        )
        exists = bool(cur.fetchone()[0])
    if not exists:
        raise RuntimeError(
            "Address-type filtering was requested, but public.address.is_contract "
            "does not exist. Run the address classifier first."
        )


def ensure_zero_address_exists(conn) -> None:
    """Fail clearly if the canonical Ethereum zero address is absent from address."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM public.address
                WHERE addr = decode(repeat('00', 20), 'hex')
            )
            """
        )
        exists = bool(cur.fetchone()[0])
    conn.commit()

    if not exists:
        raise RuntimeError(
            "public.address does not contain the zero address; cannot apply the "
            "mint/burn exclusion consistently."
        )


def economic_transfer_ctes(view: str, exclude_zero_address: bool) -> str:
    """Return CTE definitions for the common downstream analyses."""
    if exclude_zero_address:
        return f"""
        zero_address AS (
            SELECT id
            FROM public.address
            WHERE addr = decode(repeat('00', 20), 'hex')
        ),
        economic_transfers AS (
            SELECT t.*
            FROM public.{view} t
            WHERE t.from_id NOT IN (SELECT id FROM zero_address)
              AND t.to_id NOT IN (SELECT id FROM zero_address)
        )
        """

    return f"""
    economic_transfers AS (
        SELECT t.*
        FROM public.{view} t
    )
    """


def monthly_activity(
    conn,
    symbol: str,
    view: str,
    decimals: int,
    transfer_filter: str,
    exclude_zero_address: bool = True,
    one_row_per_transaction: bool = False,
) -> None:
    """
    Monthly transfer activity.

    ERC-20 analyses normally exclude zero-address mint/burn events. Native ETH can
    opt out of that exclusion. transaction_count counts distinct Ethereum
    transactions; for a one-row-per-transaction source such as native ETH it uses
    COUNT(*) directly.
    """
    s = safe_name(symbol)
    ctes = economic_transfer_ctes(view, exclude_zero_address)
    tx_count_expr = (
        "COUNT(*)"
        if one_row_per_transaction
        else "COUNT(DISTINCT (t.block_number, t.tx_index))"
    )

    sql = f"""
    WITH
    {ctes},
    monthly_base AS (
        SELECT
            date_trunc('month', b.ts)::date AS month,
            COUNT(*) AS transfer_count,
            {tx_count_expr} AS transaction_count,
            SUM(t.amount) AS raw_volume,
            SUM(t.amount) / POWER(10::numeric, {decimals}) AS token_volume,
            COUNT(DISTINCT t.from_id) AS unique_senders,
            COUNT(DISTINCT t.to_id) AS unique_receivers
        FROM economic_transfers t
        JOIN public.eth_block b
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
            FROM economic_transfers t
            JOIN public.eth_block b
              ON t.block_number = b.block_number

            UNION ALL

            SELECT
                date_trunc('month', b.ts)::date AS month,
                t.to_id AS address_id
            FROM economic_transfers t
            JOIN public.eth_block b
              ON t.block_number = b.block_number
        ) x
        GROUP BY month
    )
    SELECT
        b.month,
        b.transfer_count,
        b.transaction_count,
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
    exclude_zero_address: bool = True,
    one_row_per_transaction: bool = False,
) -> None:
    """Export monthly volume, event/transfer count, transaction count and adoption."""
    s = safe_name(symbol)
    ctes = economic_transfer_ctes(view, exclude_zero_address)
    tx_count_expr = (
        "COUNT(*)"
        if one_row_per_transaction
        else "COUNT(DISTINCT (t.block_number, t.tx_index))"
    )

    sql = f"""
    WITH
    {ctes},
    monthly_transfers AS (
        SELECT
            date_trunc('month', b.ts)::date AS month,
            COUNT(*) AS transfer_count,
            {tx_count_expr} AS transaction_count,
            SUM(t.amount) / POWER(10::numeric, {decimals}) AS token_volume
        FROM economic_transfers t
        JOIN public.eth_block b
          ON b.block_number = t.block_number
        GROUP BY 1
    ),
    participant_events AS (
        SELECT
            date_trunc('month', b.ts)::date AS month,
            t.from_id AS address_id
        FROM economic_transfers t
        JOIN public.eth_block b
          ON b.block_number = t.block_number

        UNION ALL

        SELECT
            date_trunc('month', b.ts)::date AS month,
            t.to_id AS address_id
        FROM economic_transfers t
        JOIN public.eth_block b
          ON b.block_number = t.block_number
    ),
    monthly_active_addresses AS (
        SELECT
            month,
            COUNT(DISTINCT address_id) AS unique_addresses
        FROM participant_events
        GROUP BY month
    ),
    address_first_seen AS (
        SELECT
            address_id,
            MIN(month) AS first_seen_month
        FROM participant_events
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

def monthly_mint_burn(conn, symbol: str, base_view: str, decimals: int) -> None:
    """
    Aggregate zero-address issuance/destruction separately from user activity.

    Mint: zero address -> non-zero address
    Burn: non-zero address -> zero address

    This always uses the token's successful-transaction base view, not an
    address-type filtered view, and therefore runs once per token.
    """
    s = safe_name(symbol)

    sql = f"""
    WITH zero_address AS (
        SELECT id
        FROM public.address
        WHERE addr = decode(repeat('00', 20), 'hex')
    ),
    monthly AS (
        SELECT
            date_trunc('month', b.ts)::date AS month,

            COUNT(*) FILTER (
                WHERE t.from_id = z.id
                  AND t.to_id <> z.id
            ) AS mint_event_count,
            COUNT(DISTINCT (t.block_number, t.tx_index)) FILTER (
                WHERE t.from_id = z.id
                  AND t.to_id <> z.id
            ) AS mint_transaction_count,
            COUNT(DISTINCT t.to_id) FILTER (
                WHERE t.from_id = z.id
                  AND t.to_id <> z.id
            ) AS unique_mint_recipients,
            COALESCE(SUM(t.amount) FILTER (
                WHERE t.from_id = z.id
                  AND t.to_id <> z.id
            ), 0) AS raw_minted_amount,

            COUNT(*) FILTER (
                WHERE t.to_id = z.id
                  AND t.from_id <> z.id
            ) AS burn_event_count,
            COUNT(DISTINCT (t.block_number, t.tx_index)) FILTER (
                WHERE t.to_id = z.id
                  AND t.from_id <> z.id
            ) AS burn_transaction_count,
            COUNT(DISTINCT t.from_id) FILTER (
                WHERE t.to_id = z.id
                  AND t.from_id <> z.id
            ) AS unique_burn_senders,
            COALESCE(SUM(t.amount) FILTER (
                WHERE t.to_id = z.id
                  AND t.from_id <> z.id
            ), 0) AS raw_burned_amount

        FROM public.{base_view} t
        CROSS JOIN zero_address z
        JOIN public.eth_block b
          ON b.block_number = t.block_number
        WHERE t.from_id = z.id
           OR t.to_id = z.id
        GROUP BY 1
    ),
    normalized AS (
        SELECT
            month,
            mint_event_count,
            mint_transaction_count,
            unique_mint_recipients,
            raw_minted_amount,
            raw_minted_amount / POWER(10::numeric, {decimals}) AS minted_volume,
            burn_event_count,
            burn_transaction_count,
            unique_burn_senders,
            raw_burned_amount,
            raw_burned_amount / POWER(10::numeric, {decimals}) AS burned_volume
        FROM monthly
    )
    SELECT
        month,
        mint_event_count,
        mint_transaction_count,
        unique_mint_recipients,
        raw_minted_amount,
        minted_volume,
        burn_event_count,
        burn_transaction_count,
        unique_burn_senders,
        raw_burned_amount,
        burned_volume,
        minted_volume - burned_volume AS net_issuance,
        SUM(minted_volume - burned_volume) OVER (
            ORDER BY month
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS cumulative_net_issuance
    FROM normalized
    ORDER BY month;
    """

    export_query(
        conn,
        sql,
        OUTPUT_DIR / f"{s}_all_monthly_mint_burn.csv",
    )

def monthly_adoption_and_funded_by(
    conn,
    symbol: str,
    view: str,
    decimals: int,
    transfer_filter: str,
    exclude_zero_address: bool = True,
) -> None:
    s = safe_name(symbol)
    ctes = economic_transfer_ctes(view, exclude_zero_address)

    adoption_sql = f"""
    WITH
    {ctes},
    first_seen AS (
        SELECT
            participant_id AS address_id,
            MIN(b.ts) AS first_seen_ts
        FROM (
            SELECT block_number, tx_index, from_id AS participant_id
            FROM economic_transfers
            UNION ALL
            SELECT block_number, tx_index, to_id AS participant_id
            FROM economic_transfers
        ) p
        JOIN public.eth_block b
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
    WITH
    {ctes},
    incoming_ranked AS (
        SELECT
            t.to_id AS new_address_id,
            t.from_id AS funded_by_id,
            t.amount,
            b.ts,
            ROW_NUMBER() OVER (
                PARTITION BY t.to_id
                ORDER BY b.ts, t.block_number, t.tx_index, t.log_index
            ) AS rn
        FROM economic_transfers t
        JOIN public.eth_block b
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
            SUM(amount) / POWER(10::numeric, {decimals}) AS funded_volume,
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
    JOIN public.address a
      ON m.funded_by_id = a.id
    WHERE m.funder_rank <= 100
    ORDER BY m.month, m.funder_rank;
    """

    export_query(
        conn,
        funded_by_sql,
        OUTPUT_DIR / f"{s}_{transfer_filter}_monthly_top100_funded_by.csv"
    )

def monthly_top_users(
    conn,
    symbol: str,
    view: str,
    decimals: int,
    transfer_filter: str,
    exclude_zero_address: bool = True,
    one_row_per_transaction: bool = False,
) -> None:
    s = safe_name(symbol)
    ctes = economic_transfer_ctes(view, exclude_zero_address)
    outgoing_tx_count_expr = (
        "COUNT(*)"
        if one_row_per_transaction
        else "COUNT(DISTINCT (t.block_number, t.tx_index))"
    )

    sql = f"""
    WITH
    {ctes},
    monthly_users AS (
        SELECT
            date_trunc('month', b.ts)::date AS month,
            t.from_id AS address_id,
            COUNT(*) AS outgoing_transfer_count,
            {outgoing_tx_count_expr} AS outgoing_transaction_count,
            SUM(t.amount) AS raw_outgoing_volume,
            SUM(t.amount) / POWER(10::numeric, {decimals}) AS outgoing_volume,
            ROW_NUMBER() OVER (
                PARTITION BY date_trunc('month', b.ts)::date
                ORDER BY SUM(t.amount) DESC
            ) AS user_rank
        FROM economic_transfers t
        JOIN public.eth_block b
          ON t.block_number = b.block_number
        GROUP BY 1, t.from_id
    )
    SELECT
        m.month,
        m.user_rank,
        m.address_id,
        encode(a.addr, 'hex') AS address,
        m.outgoing_transfer_count,
        m.outgoing_transaction_count,
        m.raw_outgoing_volume,
        m.outgoing_volume
    FROM monthly_users m
    JOIN public.address a
      ON m.address_id = a.id
    WHERE m.user_rank <= 100
    ORDER BY m.month, m.user_rank;
    """

    export_query(
        conn,
        sql,
        OUTPUT_DIR / f"{s}_{transfer_filter}_monthly_top100_users.csv"
    )

def transfer_size_histogram_all_time(
    conn,
    symbol: str,
    view: str,
    decimals: int,
    transfer_filter: str,
    exclude_zero_address: bool = True,
) -> None:
    s = safe_name(symbol)
    ctes = economic_transfer_ctes(view, exclude_zero_address)

    sql = f"""
    WITH
    {ctes},
    normalized AS (
        SELECT
            t.amount / POWER(10::numeric, {decimals}) AS amount_normalized
        FROM economic_transfers t
        WHERE t.amount > 0
    )
    SELECT
        {BUCKETS_SQL} AS size_bucket,
        COUNT(*) AS transfer_count,
        SUM(amount_normalized) AS bucket_volume
    FROM normalized
    GROUP BY 1
    ORDER BY MIN(amount_normalized);
    """

    export_query(
        conn,
        sql,
        OUTPUT_DIR / f"{s}_{transfer_filter}_transfer_size_histogram_all_time.csv"
    )

def monthly_transfer_size_buckets(
    conn,
    symbol: str,
    view: str,
    decimals: int,
    transfer_filter: str,
    exclude_zero_address: bool = True,
) -> None:
    s = safe_name(symbol)
    ctes = economic_transfer_ctes(view, exclude_zero_address)

    sql = f"""
    WITH
    {ctes},
    normalized AS (
        SELECT
            date_trunc('month', b.ts)::date AS month,
            t.amount / POWER(10::numeric, {decimals}) AS amount_normalized
        FROM economic_transfers t
        JOIN public.eth_block b
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
    verify_decimals = bool(settings.get("verify_decimals", False))
    allow_destructive_rebuilds = bool(
        settings.get("allow_destructive_rebuilds", False)
    )

    if verify_decimals and not RPC_URL:
        raise RuntimeError(
            "settings.verify_decimals is true, but ETH_RPC_URL is missing in .env"
        )

    tokens = config.get("tokens", {})
    native_eth_cfg = config.get("native_eth", {}) or {}
    native_eth_enabled = bool(native_eth_cfg.get("enabled", False))

    if not tokens and not native_eth_enabled:
        raise RuntimeError("No enabled analysis sources found in config/tokens.yaml")

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

        print(f"[preflight] validating {len(enabled_tokens)} enabled ERC-20 tokens")

        any_address_filter = False
        for symbol, cfg in enabled_tokens:
            decimals = int(cfg["decimals"])
            address = cfg["address"]
            filters = get_transfer_filters(cfg)

            if verify_decimals:
                verify_token_decimals(symbol, address, decimals)

            ensure_token_in_database(conn, symbol, address)
            any_address_filter = any_address_filter or any(
                transfer_filter != "all" for transfer_filter in filters
            )

            normalize_rebuild_mode(cfg.get("rebuild_token_view", False))
            normalize_rebuild_mode(cfg.get("rebuild_filtered_views", "if_legacy"))

        if any_address_filter:
            ensure_address_classification_column(conn)

        if enabled_tokens:
            ensure_zero_address_exists(conn)

        if native_eth_enabled:
            native_symbol = str(native_eth_cfg.get("symbol", "ETH")).strip().upper()
            if safe_name(native_symbol) != "eth":
                raise ValueError(
                    "native_eth.symbol should normally be ETH; this pipeline reserves "
                    "the native source for Ethereum."
                )
            if int(native_eth_cfg.get("decimals", 18)) != 18:
                raise ValueError("Native ETH decimals must be 18")
            normalize_rebuild_mode(native_eth_cfg.get("rebuild_view", "if_legacy"))

        print("[preflight] configuration passed")
        print(
            f"[safety] destructive_rebuilds={'enabled' if allow_destructive_rebuilds else 'disabled'}"
        )

        # ---------------- ERC-20 analyses ----------------
        for symbol, cfg in enabled_tokens:
            decimals = int(cfg["decimals"])
            address = cfg["address"]
            analyses = cfg.get("analyses", {})
            filters = get_transfer_filters(cfg)
            base_rebuild_mode = normalize_rebuild_mode(
                cfg.get("rebuild_token_view", False)
            )
            filtered_rebuild_mode = normalize_rebuild_mode(
                cfg.get("rebuild_filtered_views", "if_legacy")
            )
            setup_if_missing = bool(cfg.get("setup_token_view", True))
            refresh_base = bool(cfg.get("refresh_token_view", False))

            print(f"\n=== {symbol} ===")
            print(f"[plan] filters={','.join(filters)}")
            print(f"[plan] base_rebuild={base_rebuild_mode}")
            print(f"[plan] refresh_base={refresh_base}")
            print(f"[plan] filtered_rebuild={filtered_rebuild_mode}")
            print("[plan] success=true only")
            print("[plan] main ERC-20 analyses exclude zero-address mint/burn transfers")

            base_changed = setup_token_view(
                conn=conn,
                symbol=symbol,
                address=address,
                setup_if_missing=setup_if_missing,
                rebuild_mode=base_rebuild_mode,
                allow_destructive_rebuilds=allow_destructive_rebuilds,
                refresh_existing=refresh_base,
            )

            base_view = transfer_view_name(symbol)
            if analyses.get("monthly_mint_burn", False):
                monthly_mint_burn(conn, symbol, base_view, decimals)

            for transfer_filter in filters:
                print(f"\n--- {symbol} | filter={transfer_filter} ---")

                view = setup_filtered_view(
                    conn=conn,
                    symbol=symbol,
                    transfer_filter=transfer_filter,
                    rebuild_mode=filtered_rebuild_mode,
                    allow_destructive_rebuilds=allow_destructive_rebuilds,
                    force_refresh=base_changed and transfer_filter != "all",
                )
                ensure_materialized_view_exists(conn, view)

                if analyses.get("monthly_activity", False):
                    monthly_activity(
                        conn, symbol, view, decimals, transfer_filter,
                        exclude_zero_address=True,
                        one_row_per_transaction=False,
                    )

                if analyses.get("monthly_summary", False):
                    monthly_summary(
                        conn, symbol, view, decimals, transfer_filter,
                        exclude_zero_address=True,
                        one_row_per_transaction=False,
                    )

                if analyses.get("monthly_adoption_and_funded_by", False):
                    monthly_adoption_and_funded_by(
                        conn, symbol, view, decimals, transfer_filter,
                        exclude_zero_address=True,
                    )

                if analyses.get("monthly_top_users", False):
                    monthly_top_users(
                        conn, symbol, view, decimals, transfer_filter,
                        exclude_zero_address=True,
                        one_row_per_transaction=False,
                    )

                if analyses.get("transfer_size_histogram_all_time", False):
                    transfer_size_histogram_all_time(
                        conn, symbol, view, decimals, transfer_filter,
                        exclude_zero_address=True,
                    )

                if analyses.get("monthly_transfer_size_buckets", False):
                    monthly_transfer_size_buckets(
                        conn, symbol, view, decimals, transfer_filter,
                        exclude_zero_address=True,
                    )

        # ---------------- Native ETH analyses ----------------
        if native_eth_enabled:
            symbol = str(native_eth_cfg.get("symbol", "ETH")).strip().upper()
            decimals = 18
            analyses = native_eth_cfg.get("analyses", {})
            rebuild_mode = normalize_rebuild_mode(
                native_eth_cfg.get("rebuild_view", "if_legacy")
            )
            setup_if_missing = bool(native_eth_cfg.get("setup_view", True))
            refresh_view = bool(native_eth_cfg.get("refresh_view", False))
            exclude_zero_address = bool(
                native_eth_cfg.get("exclude_zero_address", False)
            )

            print(f"\n=== {symbol} | native top-level ===")
            print("[plan] filter=all only")
            print("[plan] source=eth_tx.value")
            print("[plan] success=true AND value>0 AND to_id IS NOT NULL")
            print("[plan] internal execution-trace transfers are not included")
            print(f"[plan] exclude_zero_address={exclude_zero_address}")

            setup_native_eth_view(
                conn=conn,
                setup_if_missing=setup_if_missing,
                rebuild_mode=rebuild_mode,
                allow_destructive_rebuilds=allow_destructive_rebuilds,
                refresh_existing=refresh_view,
            )
            view = NATIVE_ETH_VIEW
            transfer_filter = "all"

            if analyses.get("monthly_activity", False):
                monthly_activity(
                    conn, symbol, view, decimals, transfer_filter,
                    exclude_zero_address=exclude_zero_address,
                    one_row_per_transaction=True,
                )

            if analyses.get("monthly_summary", False):
                monthly_summary(
                    conn, symbol, view, decimals, transfer_filter,
                    exclude_zero_address=exclude_zero_address,
                    one_row_per_transaction=True,
                )

            if analyses.get("monthly_adoption_and_funded_by", False):
                monthly_adoption_and_funded_by(
                    conn, symbol, view, decimals, transfer_filter,
                    exclude_zero_address=exclude_zero_address,
                )

            if analyses.get("monthly_top_users", False):
                monthly_top_users(
                    conn, symbol, view, decimals, transfer_filter,
                    exclude_zero_address=exclude_zero_address,
                    one_row_per_transaction=True,
                )

            if analyses.get("transfer_size_histogram_all_time", False):
                transfer_size_histogram_all_time(
                    conn, symbol, view, decimals, transfer_filter,
                    exclude_zero_address=exclude_zero_address,
                )

            if analyses.get("monthly_transfer_size_buckets", False):
                monthly_transfer_size_buckets(
                    conn, symbol, view, decimals, transfer_filter,
                    exclude_zero_address=exclude_zero_address,
                )



if __name__ == "__main__":
    run_pipeline()
