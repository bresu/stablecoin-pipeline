#!/usr/bin/env python3
"""
BSC aggregation pipeline for the thesis dataset.

Design goals
------------
* Never scan the 27B-row bep20_transfer table once per token/analysis.
* Read bounded block ranges using the existing (block_number, ...) primary keys.
* In each block chunk, materialize only the configured BEP-20 transfers into a
  TEMP table, then update all analytical state from that small table.
* Commit after every chunk so the job is restartable after a reboot.
* Preserve the CSV file names and column schemas produced by the Ethereum
  pipeline for transfer_filter="all".
* Aggregate native BNB from transaction.value exactly like native ETH is
  handled by the Ethereum pipeline: successful top-level value transfers only,
  value > 0, to_id IS NOT NULL; internal trace transfers are not included.

This pipeline intentionally does NOT perform EOA/SC classification. If that is
added later, it should be a separate pass over only relevant addresses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

import pandas as pd
import psycopg
import yaml
from dotenv import load_dotenv


load_dotenv()

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("BSC_CONFIG", SCRIPT_DIR / "config" / "tokens.yaml"))
PG_DSN = os.getenv("BSC_PG_DSN")

BUCKET_CASE = """
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

BUCKET_RANK_CASE = """
CASE
    WHEN amount_normalized < 0.01 THEN 1
    WHEN amount_normalized < 0.1 THEN 2
    WHEN amount_normalized < 1 THEN 3
    WHEN amount_normalized < 10 THEN 4
    WHEN amount_normalized < 100 THEN 5
    WHEN amount_normalized < 1000 THEN 6
    WHEN amount_normalized < 10000 THEN 7
    WHEN amount_normalized < 100000 THEN 8
    WHEN amount_normalized < 1000000 THEN 9
    WHEN amount_normalized < 10000000 THEN 10
    ELSE 11
END
"""


def qident(name: str) -> str:
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return '"' + name + '"'


def clean_address(address: str) -> str:
    a = str(address).strip().lower()
    if a.startswith("0x"):
        a = a[2:]
    if not re.fullmatch(r"[0-9a-f]{40}", a):
        raise ValueError(f"Invalid EVM address: {address!r}")
    return a


def safe_symbol(symbol: str) -> str:
    s = str(symbol).strip().lower().replace("-", "_")
    if not re.fullmatch(r"[a-z0-9_]+", s):
        raise ValueError(f"Unsafe symbol: {symbol!r}")
    return s


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


def enabled_tokens(cfg: dict) -> list[tuple[str, dict]]:
    return [
        (str(symbol).strip().upper(), token_cfg)
        for symbol, token_cfg in (cfg.get("tokens") or {}).items()
        if token_cfg.get("enabled", False)
    ]


def config_fingerprint(cfg: dict) -> str:
    """Fingerprint only semantics that affect aggregate state."""
    payload = {
        "range": cfg.get("range", {}),
        "tokens": {
            symbol: {
                "address": token_cfg.get("address"),
                "decimals": int(token_cfg.get("decimals", 0)),
                "enabled": bool(token_cfg.get("enabled", False)),
            }
            for symbol, token_cfg in sorted((cfg.get("tokens") or {}).items())
        },
        "native_bnb": cfg.get("native_bnb", {}),
        # Chunk boundaries are part of the state identity because progress rows
        # are range-specific. Changing chunk size mid-run could otherwise
        # overlap already aggregated blocks and double-count them.
        "chunking": {
            "chunk_size_blocks": int(
                (cfg.get("settings") or {}).get("chunk_size_blocks", 100000)
            ),
            "native_chunk_size_blocks": int(
                (cfg.get("settings") or {}).get(
                    "native_chunk_size_blocks", 100000
                )
            ),
        },
        "version": 1,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def relation_exists(conn, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_name = %s
            )
            """,
            (schema, table),
        )
        return bool(cur.fetchone()[0])


def get_block_timestamp_column(conn) -> str:
    """Support the common timestamp names without hard-coding the scraper schema."""
    preferred = ("ts", "timestamp", "block_time", "time")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'block'
            """
        )
        columns = {row[0] for row in cur.fetchall()}
    for name in preferred:
        if name in columns:
            return name
    raise RuntimeError(
        "Could not identify the timestamp column in public.block. "
        f"Looked for {preferred}; found {sorted(columns)}"
    )


def ensure_raw_schema(conn) -> None:
    required = {
        "bep20_transfer": {
            "block_number", "tx_index", "log_index", "token_id",
            "from_id", "to_id", "amount",
        },
        "transaction": {
            "block_number", "tx_index", "from_id", "to_id",
            "value", "success",
        },
        "block": {"block_number"},
        "token": {"id", "addr"},
        "address": {"id", "addr"},
    }
    with conn.cursor() as cur:
        for table, needed in required.items():
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name=%s
                """,
                (table,),
            )
            got = {r[0] for r in cur.fetchall()}
            missing = needed - got
            if missing:
                raise RuntimeError(
                    f"public.{table} missing required columns: {sorted(missing)}"
                )


def resolve_token_ids(conn, tokens: list[tuple[str, dict]]) -> list[dict]:
    resolved = []
    with conn.cursor() as cur:
        for symbol, tc in tokens:
            addr_hex = clean_address(tc["address"])
            cur.execute(
                """
                SELECT id
                FROM public.token
                WHERE addr = decode(%s, 'hex')
                """,
                (addr_hex,),
            )
            rows = cur.fetchall()
            if len(rows) != 1:
                raise RuntimeError(
                    f"{symbol}: expected exactly one row in public.token for "
                    f"{tc['address']}, found {len(rows)}"
                )
            resolved.append(
                {
                    "symbol": symbol,
                    "token_id": int(rows[0][0]),
                    "decimals": int(tc["decimals"]),
                    "address": "0x" + addr_hex,
                }
            )
    return resolved


def get_zero_address_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM public.address
            WHERE addr = decode(repeat('00', 20), 'hex')
            """
        )
        rows = cur.fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            "Expected exactly one canonical zero address in public.address; "
            f"found {len(rows)}"
        )
    return int(rows[0][0])


def create_state_schema(conn, schema: str) -> None:
    s = qident(schema)
    ddl = f"""
    CREATE SCHEMA IF NOT EXISTS {s};

    CREATE TABLE IF NOT EXISTS {s}.meta (
        key text PRIMARY KEY,
        value text NOT NULL
    );

    CREATE TABLE IF NOT EXISTS {s}.assets (
        asset text PRIMARY KEY,
        decimals integer NOT NULL,
        source text NOT NULL,
        token_id bigint,
        token_address text
    );

    CREATE TABLE IF NOT EXISTS {s}.progress (
        source text NOT NULL,
        start_block integer NOT NULL,
        end_block integer NOT NULL,
        selected_rows bigint NOT NULL DEFAULT 0,
        completed_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (source, start_block, end_block)
    );

    CREATE TABLE IF NOT EXISTS {s}.monthly_totals (
        asset text NOT NULL,
        month date NOT NULL,
        transfer_count bigint NOT NULL DEFAULT 0,
        transaction_count bigint NOT NULL DEFAULT 0,
        raw_volume numeric NOT NULL DEFAULT 0,
        PRIMARY KEY (asset, month)
    );

    CREATE TABLE IF NOT EXISTS {s}.monthly_addresses (
        asset text NOT NULL,
        month date NOT NULL,
        address_id bigint NOT NULL,
        sent boolean NOT NULL DEFAULT false,
        received boolean NOT NULL DEFAULT false,
        PRIMARY KEY (asset, month, address_id)
    );

    CREATE TABLE IF NOT EXISTS {s}.first_seen (
        asset text NOT NULL,
        address_id bigint NOT NULL,
        first_seen_month date NOT NULL,
        PRIMARY KEY (asset, address_id)
    );

    CREATE TABLE IF NOT EXISTS {s}.relevant_addresses (
        asset text NOT NULL,
        address_id bigint NOT NULL,
        PRIMARY KEY (asset, address_id)
    );

    CREATE TABLE IF NOT EXISTS {s}.monthly_users (
        asset text NOT NULL,
        month date NOT NULL,
        address_id bigint NOT NULL,
        outgoing_transfer_count bigint NOT NULL DEFAULT 0,
        outgoing_transaction_count bigint NOT NULL DEFAULT 0,
        raw_outgoing_volume numeric NOT NULL DEFAULT 0,
        PRIMARY KEY (asset, month, address_id)
    );

    CREATE TABLE IF NOT EXISTS {s}.first_incoming (
        asset text NOT NULL,
        new_address_id bigint NOT NULL,
        funded_by_id bigint NOT NULL,
        amount numeric NOT NULL,
        month date NOT NULL,
        block_number integer NOT NULL,
        tx_index integer NOT NULL,
        log_index integer NOT NULL,
        PRIMARY KEY (asset, new_address_id)
    );

    CREATE TABLE IF NOT EXISTS {s}.size_buckets_monthly (
        asset text NOT NULL,
        month date NOT NULL,
        bucket_rank smallint NOT NULL,
        size_bucket text NOT NULL,
        transfer_count bigint NOT NULL DEFAULT 0,
        bucket_volume numeric NOT NULL DEFAULT 0,
        PRIMARY KEY (asset, month, bucket_rank)
    );

    CREATE TABLE IF NOT EXISTS {s}.size_buckets_all (
        asset text NOT NULL,
        bucket_rank smallint NOT NULL,
        size_bucket text NOT NULL,
        transfer_count bigint NOT NULL DEFAULT 0,
        bucket_volume numeric NOT NULL DEFAULT 0,
        PRIMARY KEY (asset, bucket_rank)
    );

    CREATE TABLE IF NOT EXISTS {s}.mint_burn_monthly (
        asset text NOT NULL,
        month date NOT NULL,
        mint_event_count bigint NOT NULL DEFAULT 0,
        mint_transaction_count bigint NOT NULL DEFAULT 0,
        raw_minted_amount numeric NOT NULL DEFAULT 0,
        burn_event_count bigint NOT NULL DEFAULT 0,
        burn_transaction_count bigint NOT NULL DEFAULT 0,
        raw_burned_amount numeric NOT NULL DEFAULT 0,
        PRIMARY KEY (asset, month)
    );

    CREATE TABLE IF NOT EXISTS {s}.mint_burn_participants (
        asset text NOT NULL,
        month date NOT NULL,
        kind text NOT NULL CHECK (kind IN ('mint_recipient', 'burn_sender')),
        address_id bigint NOT NULL,
        PRIMARY KEY (asset, month, kind, address_id)
    );
    """
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()


def seed_assets(conn, schema: str, resolved: list[dict], cfg: dict) -> None:
    s = qident(schema)
    native = cfg.get("native_bnb") or {}
    with conn.cursor() as cur:
        for r in resolved:
            cur.execute(
                f"""
                INSERT INTO {s}.assets(asset, decimals, source, token_id, token_address)
                VALUES (%s, %s, 'bep20', %s, %s)
                ON CONFLICT (asset) DO UPDATE SET
                    decimals = EXCLUDED.decimals,
                    source = EXCLUDED.source,
                    token_id = EXCLUDED.token_id,
                    token_address = EXCLUDED.token_address
                """,
                (r["symbol"], r["decimals"], r["token_id"], r["address"]),
            )
        if native.get("enabled", True):
            cur.execute(
                f"""
                INSERT INTO {s}.assets(asset, decimals, source, token_id, token_address)
                VALUES (%s, 18, 'native', NULL, NULL)
                ON CONFLICT (asset) DO UPDATE SET
                    decimals = 18,
                    source = 'native',
                    token_id = NULL,
                    token_address = NULL
                """,
                (str(native.get("symbol", "BNB")).strip().upper(),),
            )
    conn.commit()


def ensure_fingerprint(conn, schema: str, fingerprint: str) -> None:
    s = qident(schema)
    with conn.cursor() as cur:
        cur.execute(f"SELECT value FROM {s}.meta WHERE key='config_fingerprint'")
        row = cur.fetchone()
        if row and row[0] != fingerprint:
            raise RuntimeError(
                "BSC analysis state was created with a different token/range "
                "configuration. Run `python bsc/pipeline.py reset` before rebuilding."
            )
        if not row:
            cur.execute(
                f"""
                INSERT INTO {s}.meta(key, value)
                VALUES ('config_fingerprint', %s)
                """,
                (fingerprint,),
            )
    conn.commit()


def progress_done(conn, schema: str, source: str, start: int, end: int) -> bool:
    s = qident(schema)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT EXISTS (
                SELECT 1 FROM {s}.progress
                WHERE source=%s AND start_block=%s AND end_block=%s
            )
            """,
            (source, start, end),
        )
        return bool(cur.fetchone()[0])


def create_token_chunk(
    conn,
    resolved: list[dict],
    start: int,
    end: int,
    block_ts_col: str,
) -> int:
    """
    Read the raw bep20_transfer block range exactly once.

    enable_seqscan is disabled only for this extraction, protecting a 27B-row
    table from an accidental whole-table sequential scan when a block-range
    index scan is available from the primary key.
    """
    token_ids = [r["token_id"] for r in resolved]
    values = ", ".join(
        f"({int(r['token_id'])}::bigint, '{r['symbol']}'::text, {int(r['decimals'])}::int)"
        for r in resolved
    )
    ts = qident(block_ts_col)

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS pg_temp.chunk_transfers")
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute(
            f"""
            CREATE TEMP TABLE chunk_transfers ON COMMIT DROP AS
            WITH token_map(token_id, asset, decimals) AS (
                VALUES {values}
            )
            SELECT
                tm.asset,
                tm.decimals,
                t.block_number,
                t.tx_index,
                t.log_index,
                t.from_id,
                t.to_id,
                t.amount,
                date_trunc('month', b.{ts})::date AS month
            FROM public.bep20_transfer t
            JOIN token_map tm
              ON tm.token_id = t.token_id
            JOIN public."transaction" tx
              ON tx.block_number = t.block_number
             AND tx.tx_index = t.tx_index
            JOIN public."block" b
              ON b.block_number = t.block_number
            WHERE t.block_number >= %s
              AND t.block_number < %s
              AND t.token_id = ANY(%s)
              AND tx.success IS TRUE
            """,
            (start, end, token_ids),
        )
        cur.execute("SET LOCAL enable_seqscan = on")
        cur.execute("SELECT COUNT(*) FROM chunk_transfers")
        count = int(cur.fetchone()[0])
        if count:
            cur.execute("ANALYZE chunk_transfers")
    return count


def create_native_chunk(
    conn,
    symbol: str,
    start: int,
    end: int,
    block_ts_col: str,
) -> int:
    ts = qident(block_ts_col)
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS pg_temp.chunk_native")
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute(
            f"""
            CREATE TEMP TABLE chunk_native ON COMMIT DROP AS
            SELECT
                %s::text AS asset,
                18::integer AS decimals,
                tx.block_number,
                tx.tx_index,
                0::integer AS log_index,
                tx.from_id,
                tx.to_id,
                tx.value AS amount,
                date_trunc('month', b.{ts})::date AS month
            FROM public."transaction" tx
            JOIN public."block" b
              ON b.block_number = tx.block_number
            WHERE tx.block_number >= %s
              AND tx.block_number < %s
              AND tx.success IS TRUE
              AND tx.value > 0
              AND tx.to_id IS NOT NULL
            """,
            (symbol, start, end),
        )
        cur.execute("SET LOCAL enable_seqscan = on")
        cur.execute("SELECT COUNT(*) FROM chunk_native")
        count = int(cur.fetchone()[0])
        if count:
            cur.execute("ANALYZE chunk_native")
    return count


def update_common_state(
    conn,
    schema: str,
    source_table: str,
    exclude_zero_address: bool,
    zero_id: int | None,
    one_row_per_transaction: bool,
) -> None:
    """Update every non-mint/burn analysis from one already-filtered TEMP chunk."""
    s = qident(schema)
    src = qident(source_table)
    if exclude_zero_address:
        if zero_id is None:
            raise RuntimeError("zero_id required")
        economic_where = f"from_id <> {int(zero_id)} AND to_id <> {int(zero_id)}"
    else:
        economic_where = "TRUE"

    tx_count = (
        "COUNT(*)"
        if one_row_per_transaction
        else "COUNT(DISTINCT (block_number, tx_index))"
    )

    sqls = []

    # Monthly totals: count/volume are additive across disjoint block chunks.
    sqls.append(f"""
    INSERT INTO {s}.monthly_totals AS d
        (asset, month, transfer_count, transaction_count, raw_volume)
    SELECT
        asset, month, COUNT(*), {tx_count}, SUM(amount)
    FROM {src}
    WHERE {economic_where}
    GROUP BY asset, month
    ON CONFLICT (asset, month) DO UPDATE SET
        transfer_count = d.transfer_count + EXCLUDED.transfer_count,
        transaction_count = d.transaction_count + EXCLUDED.transaction_count,
        raw_volume = d.raw_volume + EXCLUDED.raw_volume;
    """)

    # Exact monthly sender/receiver/active-address sets.
    sqls.append(f"""
    INSERT INTO {s}.monthly_addresses AS d
        (asset, month, address_id, sent, received)
    SELECT
        asset,
        month,
        address_id,
        BOOL_OR(sent),
        BOOL_OR(received)
    FROM (
        SELECT asset, month, from_id AS address_id, TRUE AS sent, FALSE AS received
        FROM {src}
        WHERE {economic_where}
        UNION ALL
        SELECT asset, month, to_id AS address_id, FALSE AS sent, TRUE AS received
        FROM {src}
        WHERE {economic_where}
    ) u
    GROUP BY asset, month, address_id
    ON CONFLICT (asset, month, address_id) DO UPDATE SET
        sent = d.sent OR EXCLUDED.sent,
        received = d.received OR EXCLUDED.received;
    """)

    # Addresses relevant to these assets. Useful later if EOA/SC classification is revisited.
    sqls.append(f"""
    INSERT INTO {s}.relevant_addresses(asset, address_id)
    SELECT asset, address_id
    FROM (
        SELECT asset, from_id AS address_id FROM {src} WHERE {economic_where}
        UNION
        SELECT asset, to_id AS address_id FROM {src} WHERE {economic_where}
    ) u
    ON CONFLICT DO NOTHING;
    """)

    # First-seen month is robust even if chunks are re-ordered.
    sqls.append(f"""
    INSERT INTO {s}.first_seen AS d(asset, address_id, first_seen_month)
    SELECT asset, address_id, MIN(month)
    FROM (
        SELECT asset, month, from_id AS address_id FROM {src} WHERE {economic_where}
        UNION ALL
        SELECT asset, month, to_id AS address_id FROM {src} WHERE {economic_where}
    ) u
    GROUP BY asset, address_id
    ON CONFLICT (asset, address_id) DO UPDATE SET
        first_seen_month = LEAST(d.first_seen_month, EXCLUDED.first_seen_month);
    """)

    outgoing_tx_count = (
        "COUNT(*)"
        if one_row_per_transaction
        else "COUNT(DISTINCT (block_number, tx_index))"
    )
    sqls.append(f"""
    INSERT INTO {s}.monthly_users AS d
        (asset, month, address_id, outgoing_transfer_count,
         outgoing_transaction_count, raw_outgoing_volume)
    SELECT
        asset,
        month,
        from_id,
        COUNT(*),
        {outgoing_tx_count},
        SUM(amount)
    FROM {src}
    WHERE {economic_where}
    GROUP BY asset, month, from_id
    ON CONFLICT (asset, month, address_id) DO UPDATE SET
        outgoing_transfer_count =
            d.outgoing_transfer_count + EXCLUDED.outgoing_transfer_count,
        outgoing_transaction_count =
            d.outgoing_transaction_count + EXCLUDED.outgoing_transaction_count,
        raw_outgoing_volume =
            d.raw_outgoing_volume + EXCLUDED.raw_outgoing_volume;
    """)

    # Original Ethereum semantics: earliest incoming transfer to each address.
    sqls.append(f"""
    INSERT INTO {s}.first_incoming AS d
        (asset, new_address_id, funded_by_id, amount, month,
         block_number, tx_index, log_index)
    SELECT DISTINCT ON (asset, to_id)
        asset, to_id, from_id, amount, month, block_number, tx_index, log_index
    FROM {src}
    WHERE {economic_where}
    ORDER BY asset, to_id, block_number, tx_index, log_index
    ON CONFLICT (asset, new_address_id) DO UPDATE SET
        funded_by_id = EXCLUDED.funded_by_id,
        amount = EXCLUDED.amount,
        month = EXCLUDED.month,
        block_number = EXCLUDED.block_number,
        tx_index = EXCLUDED.tx_index,
        log_index = EXCLUDED.log_index
    WHERE
        (EXCLUDED.block_number, EXCLUDED.tx_index, EXCLUDED.log_index)
        <
        (d.block_number, d.tx_index, d.log_index);
    """)

    # Monthly and all-time transfer-size buckets. Zero-value transfers are excluded.
    normalized = f"""
        SELECT
            asset,
            month,
            amount / POWER(10::numeric, decimals) AS amount_normalized
        FROM {src}
        WHERE {economic_where}
          AND amount > 0
    """
    sqls.append(f"""
    INSERT INTO {s}.size_buckets_monthly AS d
        (asset, month, bucket_rank, size_bucket, transfer_count, bucket_volume)
    SELECT
        asset,
        month,
        {BUCKET_RANK_CASE} AS bucket_rank,
        {BUCKET_CASE} AS size_bucket,
        COUNT(*),
        SUM(amount_normalized)
    FROM ({normalized}) n
    GROUP BY asset, month, bucket_rank, size_bucket
    ON CONFLICT (asset, month, bucket_rank) DO UPDATE SET
        transfer_count = d.transfer_count + EXCLUDED.transfer_count,
        bucket_volume = d.bucket_volume + EXCLUDED.bucket_volume,
        size_bucket = EXCLUDED.size_bucket;
    """)
    sqls.append(f"""
    INSERT INTO {s}.size_buckets_all AS d
        (asset, bucket_rank, size_bucket, transfer_count, bucket_volume)
    SELECT
        asset,
        {BUCKET_RANK_CASE} AS bucket_rank,
        {BUCKET_CASE} AS size_bucket,
        COUNT(*),
        SUM(amount_normalized)
    FROM ({normalized}) n
    GROUP BY asset, bucket_rank, size_bucket
    ON CONFLICT (asset, bucket_rank) DO UPDATE SET
        transfer_count = d.transfer_count + EXCLUDED.transfer_count,
        bucket_volume = d.bucket_volume + EXCLUDED.bucket_volume,
        size_bucket = EXCLUDED.size_bucket;
    """)

    with conn.cursor() as cur:
        for sql in sqls:
            cur.execute(sql)


def update_mint_burn_state(
    conn,
    schema: str,
    source_table: str,
    zero_id: int,
) -> None:
    s = qident(schema)
    src = qident(source_table)
    z = int(zero_id)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {s}.mint_burn_monthly AS d
                (asset, month,
                 mint_event_count, mint_transaction_count, raw_minted_amount,
                 burn_event_count, burn_transaction_count, raw_burned_amount)
            SELECT
                asset,
                month,
                COUNT(*) FILTER (WHERE from_id={z} AND to_id<>{z}),
                COUNT(DISTINCT (block_number, tx_index))
                    FILTER (WHERE from_id={z} AND to_id<>{z}),
                COALESCE(SUM(amount)
                    FILTER (WHERE from_id={z} AND to_id<>{z}), 0),
                COUNT(*) FILTER (WHERE to_id={z} AND from_id<>{z}),
                COUNT(DISTINCT (block_number, tx_index))
                    FILTER (WHERE to_id={z} AND from_id<>{z}),
                COALESCE(SUM(amount)
                    FILTER (WHERE to_id={z} AND from_id<>{z}), 0)
            FROM {src}
            WHERE from_id={z} OR to_id={z}
            GROUP BY asset, month
            ON CONFLICT (asset, month) DO UPDATE SET
                mint_event_count =
                    d.mint_event_count + EXCLUDED.mint_event_count,
                mint_transaction_count =
                    d.mint_transaction_count + EXCLUDED.mint_transaction_count,
                raw_minted_amount =
                    d.raw_minted_amount + EXCLUDED.raw_minted_amount,
                burn_event_count =
                    d.burn_event_count + EXCLUDED.burn_event_count,
                burn_transaction_count =
                    d.burn_transaction_count + EXCLUDED.burn_transaction_count,
                raw_burned_amount =
                    d.raw_burned_amount + EXCLUDED.raw_burned_amount
            """
        )
        cur.execute(
            f"""
            INSERT INTO {s}.mint_burn_participants(asset, month, kind, address_id)
            SELECT asset, month, 'mint_recipient', to_id
            FROM {src}
            WHERE from_id={z} AND to_id<>{z}
            GROUP BY asset, month, to_id
            ON CONFLICT DO NOTHING
            """
        )
        cur.execute(
            f"""
            INSERT INTO {s}.mint_burn_participants(asset, month, kind, address_id)
            SELECT asset, month, 'burn_sender', from_id
            FROM {src}
            WHERE to_id={z} AND from_id<>{z}
            GROUP BY asset, month, from_id
            ON CONFLICT DO NOTHING
            """
        )


def mark_progress(
    conn,
    schema: str,
    source: str,
    start: int,
    end: int,
    selected_rows: int,
) -> None:
    s = qident(schema)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {s}.progress(source, start_block, end_block, selected_rows)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (source, start_block, end_block) DO NOTHING
            """,
            (source, start, end, selected_rows),
        )


def aggregate_tokens(
    conn,
    cfg: dict,
    schema: str,
    resolved: list[dict],
    zero_id: int,
    block_ts_col: str,
) -> None:
    r = cfg.get("range") or {}
    start_block = int(r.get("start_block", 1))
    end_exclusive = int(r.get("end_block_exclusive", 116000001))
    chunk_size = int((cfg.get("settings") or {}).get("chunk_size_blocks", 100000))

    for start in range(start_block, end_exclusive, chunk_size):
        end = min(start + chunk_size, end_exclusive)
        if progress_done(conn, schema, "bep20", start, end):
            print(f"[skip] BEP20 {start:,}-{end-1:,}")
            continue
        try:
            count = create_token_chunk(conn, resolved, start, end, block_ts_col)
            if count:
                update_common_state(
                    conn, schema, "chunk_transfers",
                    exclude_zero_address=True,
                    zero_id=zero_id,
                    one_row_per_transaction=False,
                )
                update_mint_burn_state(
                    conn, schema, "chunk_transfers", zero_id
                )
            mark_progress(conn, schema, "bep20", start, end, count)
            conn.commit()
            print(
                f"[done] BEP20 {start:,}-{end-1:,} | "
                f"selected={count:,}"
            )
        except Exception:
            conn.rollback()
            raise


def aggregate_native_bnb(
    conn,
    cfg: dict,
    schema: str,
    block_ts_col: str,
) -> None:
    native = cfg.get("native_bnb") or {}
    if not native.get("enabled", True):
        return

    symbol = str(native.get("symbol", "BNB")).strip().upper()
    r = cfg.get("range") or {}
    start_block = int(r.get("start_block", 1))
    end_exclusive = int(r.get("end_block_exclusive", 116000001))
    chunk_size = int(
        (cfg.get("settings") or {}).get("native_chunk_size_blocks", 100000)
    )

    for start in range(start_block, end_exclusive, chunk_size):
        end = min(start + chunk_size, end_exclusive)
        if progress_done(conn, schema, "native_bnb", start, end):
            print(f"[skip] BNB {start:,}-{end-1:,}")
            continue
        try:
            count = create_native_chunk(conn, symbol, start, end, block_ts_col)
            if count:
                update_common_state(
                    conn, schema, "chunk_native",
                    exclude_zero_address=bool(
                        native.get("exclude_zero_address", False)
                    ),
                    zero_id=None,
                    one_row_per_transaction=True,
                )
            mark_progress(conn, schema, "native_bnb", start, end, count)
            conn.commit()
            print(
                f"[done] BNB {start:,}-{end-1:,} | "
                f"selected={count:,}"
            )
        except Exception:
            conn.rollback()
            raise


def export_query(conn, sql: str, output_path: Path, params=None) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [c.name for c in cur.description]
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=cols)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    conn.commit()
    print(f"[export] {output_path} ({len(df):,} rows)")


def export_asset(conn, schema: str, output_dir: Path, asset: str, source: str) -> None:
    s = qident(schema)
    name = safe_symbol(asset)

    # Same columns/naming as Ethereum pipeline monthly_activity().
    export_query(
        conn,
        f"""
        SELECT
            mt.month,
            mt.transfer_count,
            mt.transaction_count,
            mt.raw_volume,
            mt.raw_volume / POWER(10::numeric, a.decimals) AS token_volume,
            COUNT(*) FILTER (WHERE ma.sent) AS unique_senders,
            COUNT(*) FILTER (WHERE ma.received) AS unique_receivers,
            COUNT(*) AS active_addresses
        FROM {s}.monthly_totals mt
        JOIN {s}.assets a ON a.asset=mt.asset
        JOIN {s}.monthly_addresses ma
          ON ma.asset=mt.asset AND ma.month=mt.month
        WHERE mt.asset=%s
        GROUP BY mt.month, mt.transfer_count, mt.transaction_count,
                 mt.raw_volume, a.decimals
        ORDER BY mt.month
        """,
        output_dir / f"{name}_all_monthly_activity.csv",
        (asset,),
    )

    # Same columns/naming as Ethereum pipeline monthly_summary().
    export_query(
        conn,
        f"""
        WITH monthly_new AS (
            SELECT first_seen_month AS month, COUNT(*) AS new_addresses
            FROM {s}.first_seen
            WHERE asset=%s
            GROUP BY first_seen_month
        ),
        monthly_active AS (
            SELECT month, COUNT(*) AS unique_addresses
            FROM {s}.monthly_addresses
            WHERE asset=%s
            GROUP BY month
        )
        SELECT
            mt.month,
            mt.raw_volume / POWER(10::numeric, a.decimals) AS token_volume,
            ma.unique_addresses,
            COALESCE(mn.new_addresses, 0) AS new_addresses,
            mt.transaction_count,
            mt.transfer_count
        FROM {s}.monthly_totals mt
        JOIN {s}.assets a ON a.asset=mt.asset
        JOIN monthly_active ma ON ma.month=mt.month
        LEFT JOIN monthly_new mn ON mn.month=mt.month
        WHERE mt.asset=%s
        ORDER BY mt.month
        """,
        output_dir / f"{name}_all_monthly_summary.csv",
        (asset, asset, asset),
    )

    export_query(
        conn,
        f"""
        SELECT
            first_seen_month AS month,
            COUNT(*) AS newly_adopted_addresses
        FROM {s}.first_seen
        WHERE asset=%s
        GROUP BY first_seen_month
        ORDER BY first_seen_month
        """,
        output_dir / f"{name}_all_monthly_adoption.csv",
        (asset,),
    )

    export_query(
        conn,
        f"""
        WITH monthly_funders AS (
            SELECT
                fi.month,
                fi.funded_by_id,
                COUNT(*) AS newly_funded_addresses,
                SUM(fi.amount) AS raw_funded_volume,
                SUM(fi.amount) / POWER(10::numeric, a.decimals) AS funded_volume,
                ROW_NUMBER() OVER (
                    PARTITION BY fi.month
                    ORDER BY COUNT(*) DESC, SUM(fi.amount) DESC
                ) AS funder_rank
            FROM {s}.first_incoming fi
            JOIN {s}.assets a ON a.asset=fi.asset
            WHERE fi.asset=%s
            GROUP BY fi.month, fi.funded_by_id, a.decimals
        )
        SELECT
            m.month,
            m.funder_rank,
            m.funded_by_id,
            encode(ad.addr, 'hex') AS funded_by_address,
            m.newly_funded_addresses,
            m.raw_funded_volume,
            m.funded_volume
        FROM monthly_funders m
        JOIN public.address ad ON ad.id=m.funded_by_id
        WHERE m.funder_rank <= 100
        ORDER BY m.month, m.funder_rank
        """,
        output_dir / f"{name}_all_monthly_top100_funded_by.csv",
        (asset,),
    )

    export_query(
        conn,
        f"""
        WITH ranked AS (
            SELECT
                mu.month,
                mu.address_id,
                mu.outgoing_transfer_count,
                mu.outgoing_transaction_count,
                mu.raw_outgoing_volume,
                mu.raw_outgoing_volume / POWER(10::numeric, a.decimals)
                    AS outgoing_volume,
                ROW_NUMBER() OVER (
                    PARTITION BY mu.month
                    ORDER BY mu.raw_outgoing_volume DESC
                ) AS user_rank
            FROM {s}.monthly_users mu
            JOIN {s}.assets a ON a.asset=mu.asset
            WHERE mu.asset=%s
        )
        SELECT
            r.month,
            r.user_rank,
            r.address_id,
            encode(ad.addr, 'hex') AS address,
            r.outgoing_transfer_count,
            r.outgoing_transaction_count,
            r.raw_outgoing_volume,
            r.outgoing_volume
        FROM ranked r
        JOIN public.address ad ON ad.id=r.address_id
        WHERE r.user_rank <= 100
        ORDER BY r.month, r.user_rank
        """,
        output_dir / f"{name}_all_monthly_top100_users.csv",
        (asset,),
    )

    export_query(
        conn,
        f"""
        SELECT size_bucket, transfer_count, bucket_volume
        FROM {s}.size_buckets_all
        WHERE asset=%s
        ORDER BY bucket_rank
        """,
        output_dir / f"{name}_all_transfer_size_histogram_all_time.csv",
        (asset,),
    )

    export_query(
        conn,
        f"""
        SELECT month, size_bucket, transfer_count, bucket_volume
        FROM {s}.size_buckets_monthly
        WHERE asset=%s
        ORDER BY month, bucket_rank
        """,
        output_dir / f"{name}_all_monthly_transfer_size_buckets.csv",
        (asset,),
    )

    if source == "bep20":
        export_query(
            conn,
            f"""
            WITH p AS (
                SELECT
                    asset,
                    month,
                    COUNT(*) FILTER (WHERE kind='mint_recipient')
                        AS unique_mint_recipients,
                    COUNT(*) FILTER (WHERE kind='burn_sender')
                        AS unique_burn_senders
                FROM {s}.mint_burn_participants
                WHERE asset=%s
                GROUP BY asset, month
            ),
            n AS (
                SELECT
                    mb.asset,
                    mb.month,
                    mb.mint_event_count,
                    mb.mint_transaction_count,
                    COALESCE(p.unique_mint_recipients, 0)
                        AS unique_mint_recipients,
                    mb.raw_minted_amount,
                    mb.raw_minted_amount / POWER(10::numeric, a.decimals)
                        AS minted_volume,
                    mb.burn_event_count,
                    mb.burn_transaction_count,
                    COALESCE(p.unique_burn_senders, 0)
                        AS unique_burn_senders,
                    mb.raw_burned_amount,
                    mb.raw_burned_amount / POWER(10::numeric, a.decimals)
                        AS burned_volume
                FROM {s}.mint_burn_monthly mb
                JOIN {s}.assets a ON a.asset=mb.asset
                LEFT JOIN p ON p.asset=mb.asset AND p.month=mb.month
                WHERE mb.asset=%s
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
            FROM n
            ORDER BY month
            """,
            output_dir / f"{name}_all_monthly_mint_burn.csv",
            (asset, asset),
        )


def export_all(conn, cfg: dict, schema: str) -> None:
    settings = cfg.get("settings") or {}
    output_dir = Path(
        os.getenv(
            "BSC_OUTPUT_DIR",
            str(SCRIPT_DIR / settings.get("output_dir", "output")),
        )
    )
    with conn.cursor() as cur:
        cur.execute(f"SELECT asset, source FROM {qident(schema)}.assets ORDER BY asset")
        assets = cur.fetchall()
    conn.commit()
    for asset, source in assets:
        export_asset(conn, schema, output_dir, asset, source)


def show_status(conn, cfg: dict, schema: str) -> None:
    s = qident(schema)
    r = cfg.get("range") or {}
    start_block = int(r.get("start_block", 1))
    end_exclusive = int(r.get("end_block_exclusive", 116000001))
    settings = cfg.get("settings") or {}
    token_chunk = int(settings.get("chunk_size_blocks", 100000))
    native_chunk = int(settings.get("native_chunk_size_blocks", 100000))

    with conn.cursor() as cur:
        for source, chunk in (("bep20", token_chunk), ("native_bnb", native_chunk)):
            total = (end_exclusive - start_block + chunk - 1) // chunk
            cur.execute(
                f"""
                SELECT COUNT(*), COALESCE(SUM(selected_rows), 0),
                       MAX(end_block)
                FROM {s}.progress
                WHERE source=%s
                """,
                (source,),
            )
            done, rows, max_end = cur.fetchone()
            print(
                f"{source}: chunks {done:,}/{total:,} | "
                f"selected rows={int(rows):,} | "
                f"max completed end={max_end}"
            )
        cur.execute(
            f"""
            SELECT asset, COUNT(*)
            FROM {s}.relevant_addresses
            GROUP BY asset
            ORDER BY asset
            """
        )
        rows = cur.fetchall()
        if rows:
            print("\nUnique relevant addresses (economic transfers):")
            for asset, count in rows:
                print(f"  {asset:8s} {count:,}")
    conn.commit()


def reset_state(conn, schema: str) -> None:
    s = qident(schema)
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
    conn.commit()
    print(f"[reset] dropped analysis schema {schema}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "tokens", "bnb", "export", "status", "reset"],
    )
    args = parser.parse_args()

    if not PG_DSN:
        raise RuntimeError("Missing BSC_PG_DSN in .env/environment")

    cfg = load_config()
    settings = cfg.get("settings") or {}
    expected_db = settings.get("expected_database", "bsc1")
    schema = str(settings.get("analysis_schema", "analysis_bsc"))
    tokens = enabled_tokens(cfg)

    with psycopg.connect(PG_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, inet_server_addr()::text")
            database, user, host = cur.fetchone()
        conn.commit()
        print(f"[db] database={database} user={user} host={host}")
        if expected_db and database != expected_db:
            raise RuntimeError(
                f"Refusing to run: expected database {expected_db!r}, "
                f"connected to {database!r}"
            )

        if args.command == "reset":
            reset_state(conn, schema)
            return

        ensure_raw_schema(conn)
        block_ts_col = get_block_timestamp_column(conn)
        resolved = resolve_token_ids(conn, tokens)
        zero_id = get_zero_address_id(conn)

        print(f"[preflight] block timestamp column={block_ts_col}")
        for r in resolved:
            print(
                f"[token] {r['symbol']} token_id={r['token_id']} "
                f"decimals={r['decimals']} address={r['address']}"
            )
        print(f"[preflight] zero_address_id={zero_id}")

        create_state_schema(conn, schema)
        seed_assets(conn, schema, resolved, cfg)
        ensure_fingerprint(conn, schema, config_fingerprint(cfg))

        if args.command == "status":
            show_status(conn, cfg, schema)
            return

        if args.command in ("run", "tokens"):
            aggregate_tokens(
                conn, cfg, schema, resolved, zero_id, block_ts_col
            )

        if args.command in ("run", "bnb"):
            aggregate_native_bnb(conn, cfg, schema, block_ts_col)

        if args.command in ("run", "export"):
            export_all(conn, cfg, schema)

        show_status(conn, cfg, schema)


if __name__ == "__main__":
    main()
