from __future__ import annotations

import math
from html import escape
from pathlib import Path
from typing import Iterable

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import yaml


# Project paths based on the repository structure:
# stablecoin-pipeline/
# ├── config/plot_config.yaml
# ├── output/...
# └── plots/plot_pipeline.py
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "plot_config.yaml"


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as file:
        config = yaml.safe_load(file)
    return config or {}


def resolve_project_path(path_str: str) -> Path:
    """Resolve YAML paths relative to the repository root."""
    path = Path(path_str)
    return path if path.is_absolute() else PROJECT_ROOT / path


def filter_period(
    df: pd.DataFrame,
    start_date: str | None,
    end_date: str | None,
) -> pd.DataFrame:
    if "month" not in df.columns:
        return df.copy()

    result = df.copy()
    result["month"] = pd.to_datetime(result["month"], errors="raise")

    if start_date:
        result = result[result["month"] >= pd.to_datetime(start_date)]

    if end_date:
        # End date remains exclusive, matching the previous implementation.
        result = result[result["month"] < pd.to_datetime(end_date)]

    return result.sort_values("month")


def find_file(folder: Path, suffix: str) -> Path | None:
    matches = sorted(folder.glob(f"*{suffix}.csv"))
    return matches[0] if matches else None


def read_csv(
    input_folder: Path,
    suffix: str,
    cfg: dict,
) -> pd.DataFrame | None:
    file = find_file(input_folder, suffix)
    if file is None:
        print(f"[skip] {suffix} file not found")
        return None

    df = pd.read_csv(file)
    return filter_period(df, cfg.get("start_date"), cfg.get("end_date"))


def save_fig(fig: go.Figure, output_path: Path, cfg: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    current_margin = fig.layout.margin.to_plotly_json()
    if not current_margin:
        current_margin = dict(l=70, r=50, t=85, b=70)

    fig.update_layout(
        template=cfg.get("template", "plotly_white"),
        font=dict(size=cfg.get("font_size", 13)),
        title=dict(x=0.02, xanchor="left"),
        legend=dict(title_text=""),
        margin=current_margin,
    )

    # CDN keeps each HTML file small. Set include_plotlyjs: true in YAML for
    # fully self-contained HTML files that also work without internet access.
    include_plotlyjs = cfg.get("include_plotlyjs", "cdn")
    fig.write_html(
        output_path,
        include_plotlyjs=include_plotlyjs,
        full_html=True,
    )
    print(f"[plot] {output_path}")


def normalize_address(address: object) -> str:
    """Return a lower-case Ethereum address with a 0x prefix."""
    text = str(address).strip()
    if not text.startswith("0x"):
        text = f"0x{text}"
    return text.lower()


def short_address(address: object, leading: int = 6, trailing: int = 4) -> str:
    text = normalize_address(address)
    if len(text) <= leading + trailing + 3:
        return text
    return f"{text[: leading + 2]}…{text[-trailing:]}"


def _apply_common_layout(fig: go.Figure, cfg: dict) -> None:
    current_margin = fig.layout.margin.to_plotly_json()
    if not current_margin:
        current_margin = dict(l=70, r=50, t=85, b=70)

    fig.update_layout(
        template=cfg.get("template", "plotly_white"),
        font=dict(size=cfg.get("font_size", 13)),
        title=dict(x=0.02, xanchor="left"),
        legend=dict(title_text=""),
        margin=current_margin,
    )


def save_address_fig(
    fig: go.Figure,
    output_path: Path,
    cfg: dict,
    address_rows: pd.DataFrame,
    *,
    address_column: str,
    value_column: str,
    value_label: str,
) -> None:
    """Save an address chart with copy controls and an Etherscan table."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _apply_common_layout(fig, cfg)

    include_plotlyjs = cfg.get("include_plotlyjs", "cdn")
    plot_html = fig.to_html(
        full_html=False,
        include_plotlyjs=include_plotlyjs,
        div_id="address-plot",
        config={"displaylogo": False, "responsive": True},
    )

    explorer_base = cfg.get(
        "explorer_address_url",
        "https://etherscan.io/address/",
    )

    rows = address_rows.copy()
    rows[address_column] = rows[address_column].map(normalize_address)
    rows = rows.sort_values(value_column, ascending=False).reset_index(drop=True)

    table_rows: list[str] = []
    for rank, row in rows.iterrows():
        address = row[address_column]
        formatted_value = human_number(row[value_column])
        explorer_url = f"{explorer_base}{address}"
        table_rows.append(
            "<tr>"
            f"<td>{rank + 1}</td>"
            f"<td><code>{escape(address)}</code></td>"
            f"<td class='numeric'>{escape(formatted_value)}</td>"
            f"<td><button class='copy-address' data-address='{escape(address)}'>Copy</button></td>"
            f"<td><a href='{escape(explorer_url)}' target='_blank' rel='noopener noreferrer'>Etherscan ↗</a></td>"
            "</tr>"
        )

    title = escape(str(fig.layout.title.text or "Address chart"))
    html = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ margin: 0; font-family: Arial, sans-serif; color: #243b5a; background: white; }}
  .chart-wrap {{ width: 100%; }}
  .interaction-note {{ margin: 2px 24px 12px; font-size: 13px; color: #5d6b7c; }}
  .address-panel {{ margin: 10px 28px 30px; border: 1px solid #dfe6ee; border-radius: 8px; overflow: hidden; }}
  .address-panel summary {{ cursor: pointer; padding: 12px 14px; font-weight: 600; background: #f7f9fb; }}
  .address-controls {{ padding: 12px 14px; border-top: 1px solid #dfe6ee; }}
  #address-search {{ width: min(520px, 95%); padding: 8px 10px; border: 1px solid #c8d2de; border-radius: 5px; }}
  .table-scroll {{ overflow-x: auto; max-height: 520px; overflow-y: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 8px 10px; border-top: 1px solid #e6ebf0; text-align: left; }}
  th {{ position: sticky; top: 0; background: #f7f9fb; z-index: 1; }}
  td.numeric {{ text-align: right; font-variant-numeric: tabular-nums; }}
  code {{ user-select: all; white-space: nowrap; }}
  button.copy-address {{ cursor: pointer; border: 1px solid #b7c2cf; background: white; border-radius: 4px; padding: 5px 9px; }}
  button.copy-address:hover {{ background: #eef4f7; }}
  a {{ color: #126b58; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  #copy-toast {{ position: fixed; right: 20px; bottom: 20px; padding: 10px 14px; border-radius: 6px; background: #173f35; color: white; opacity: 0; pointer-events: none; transition: opacity .2s; z-index: 9999; }}
</style>
</head>
<body>
<div class="chart-wrap">{plot_html}</div>
<div class="interaction-note">Click a bar or bubble to copy its full address. The searchable table below also provides copy buttons and Etherscan links.</div>
<details class="address-panel">
  <summary>Addresses and lookup links ({len(rows)})</summary>
  <div class="address-controls"><input id="address-search" type="search" placeholder="Filter by address…"></div>
  <div class="table-scroll">
    <table id="address-table">
      <thead><tr><th>Rank</th><th>Full address</th><th>{escape(value_label)}</th><th>Copy</th><th>Explorer</th></tr></thead>
      <tbody>{''.join(table_rows)}</tbody>
    </table>
  </div>
</details>
<div id="copy-toast">Address copied</div>
<script>
(function() {{
  const plot = document.getElementById('address-plot');
  const toast = document.getElementById('copy-toast');

  function showToast(message) {{
    toast.textContent = message;
    toast.style.opacity = '1';
    window.clearTimeout(window.__addressToastTimer);
    window.__addressToastTimer = window.setTimeout(() => {{ toast.style.opacity = '0'; }}, 1400);
  }}

  async function copyAddress(address) {{
    try {{
      if (navigator.clipboard && window.isSecureContext) {{
        await navigator.clipboard.writeText(address);
      }} else {{
        const area = document.createElement('textarea');
        area.value = address;
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.focus();
        area.select();
        document.execCommand('copy');
        area.remove();
      }}
      showToast('Copied ' + address);
    }} catch (error) {{
      showToast('Copy failed — select it in the table');
    }}
  }}

  if (plot && plot.on) {{
    plot.on('plotly_click', function(eventData) {{
      const point = eventData && eventData.points && eventData.points[0];
      if (!point || !point.customdata) return;
      const address = Array.isArray(point.customdata) ? point.customdata[0] : point.customdata;
      if (typeof address === 'string' && address.startsWith('0x')) copyAddress(address);
    }});
  }}

  document.querySelectorAll('.copy-address').forEach((button) => {{
    button.addEventListener('click', () => copyAddress(button.dataset.address));
  }});

  const search = document.getElementById('address-search');
  if (search) {{
    search.addEventListener('input', () => {{
      const needle = search.value.trim().toLowerCase();
      document.querySelectorAll('#address-table tbody tr').forEach((row) => {{
        row.style.display = row.textContent.toLowerCase().includes(needle) ? '' : 'none';
      }});
    }});
  }}
}})();
</script>
</body>
</html>
'''
    output_path.write_text(html, encoding="utf-8")
    print(f"[plot] {output_path}")

def human_number(value: float | int) -> str:
    """
    Format values using K, M, and B.

    Deliberately never switches to T, so 500,000,000,000 is shown as 500B
    instead of 0.5T, as requested by the supervisors.
    """
    if pd.isna(value):
        return ""

    number = float(value)
    absolute = abs(number)

    if absolute >= 1_000_000_000:
        scaled, suffix = number / 1_000_000_000, "B"
    elif absolute >= 1_000_000:
        scaled, suffix = number / 1_000_000, "M"
    elif absolute >= 1_000:
        scaled, suffix = number / 1_000, "K"
    else:
        return f"{number:,.0f}" if number.is_integer() else f"{number:,.2f}"

    if abs(scaled) >= 100:
        return f"{scaled:,.0f}{suffix}"
    if abs(scaled) >= 10:
        formatted = f"{scaled:,.1f}".rstrip("0").rstrip(".")
        return f"{formatted}{suffix}"
    return _trim_scaled(scaled, suffix)


def _trim_scaled(scaled: float, suffix: str) -> str:
    formatted = f"{scaled:,.2f}".rstrip("0").rstrip(".")
    return f"{formatted}{suffix}"


def nice_tick_values(max_value: float, target_ticks: int = 6) -> list[float]:
    """Return readable numeric ticks between zero and max_value."""
    if not math.isfinite(max_value) or max_value <= 0:
        return [0.0]

    raw_step = max_value / max(target_ticks - 1, 1)
    exponent = math.floor(math.log10(raw_step))
    fraction = raw_step / (10**exponent)

    if fraction <= 1:
        nice_fraction = 1
    elif fraction <= 2:
        nice_fraction = 2
    elif fraction <= 2.5:
        nice_fraction = 2.5
    elif fraction <= 5:
        nice_fraction = 5
    else:
        nice_fraction = 10

    step = nice_fraction * (10**exponent)
    upper = math.ceil(max_value / step) * step
    count = int(round(upper / step))
    return [index * step for index in range(count + 1)]


def human_axis(values: Iterable[float], title: str) -> dict:
    numeric = pd.to_numeric(pd.Series(list(values)), errors="coerce").dropna()
    max_value = float(numeric.max()) if not numeric.empty else 0.0
    ticks = nice_tick_values(max_value)
    return {
        "title": title,
        "tickmode": "array",
        "tickvals": ticks,
        "ticktext": [human_number(value) for value in ticks],
        "rangemode": "tozero",
    }


def color_sequence(cfg: dict) -> list[str]:
    configured = cfg.get("color_sequence")
    if configured:
        return list(configured)
    return px.colors.qualitative.Safe


# Canonical order for the existing transfer-size buckets. The plotting code
# also already understands the two additional buckets planned for the next
# server-side aggregation run.
BASE_BUCKET_ORDER = [
    "<0.01",
    "0.01-0.1",
    "0.1-1",
    "1-10",
    "10-100",
    "100-1000",
    "1000-10000",
    "10000-100000",
]

OLD_FINAL_BUCKET = ">=100000"
EXPANDED_FINAL_BUCKETS = [
    "100000-1000000",
    "1000000-10000000",
    ">=10000000",
]

BUCKET_DISPLAY_LABELS = {
    "<0.01": "<0.01",
    "0.01-0.1": "0.01–0.1",
    "0.1-1": "0.1–1",
    "1-10": "1–10",
    "10-100": "10–100",
    "100-1000": "100–1K",
    "1000-10000": "1K–10K",
    "10000-100000": "10K–100K",
    ">=100000": "≥100K",
    "100000-1000000": "100K–1M",
    "1000000-10000000": "1M–10M",
    ">=10000000": "≥10M",
}


def bucket_order(df: pd.DataFrame) -> list[str]:
    """Return a fixed numeric bucket order, independent of missing months."""
    present = set(df["size_bucket"].astype(str))
    order = [bucket for bucket in BASE_BUCKET_ORDER if bucket in present]

    # Support both the current CSVs (one >=100K bucket) and future CSVs with
    # 100K–1M, 1M–10M, and >=10M split out separately.
    if any(bucket in present for bucket in EXPANDED_FINAL_BUCKETS):
        order.extend(
            bucket for bucket in EXPANDED_FINAL_BUCKETS if bucket in present
        )
    elif OLD_FINAL_BUCKET in present:
        order.append(OLD_FINAL_BUCKET)

    # Keep the script robust if another bucket is introduced later.
    known = set(order)
    order.extend(sorted(present - known))
    return order


def add_bucket_labels(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Add readable labels while preserving the canonical numeric order."""
    result = df.copy()
    raw_order = bucket_order(result)
    result["bucket_label"] = result["size_bucket"].astype(str).map(
        lambda value: BUCKET_DISPLAY_LABELS.get(value, value)
    )
    display_order = [BUCKET_DISPLAY_LABELS.get(value, value) for value in raw_order]
    return result, display_order


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = color.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Expected a six-digit hex color, got: {color}")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    channels = [max(0, min(255, round(channel))) for channel in rgb]
    return "#" + "".join(f"{channel:02X}" for channel in channels)


def _interpolate_rgb(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    fraction: float,
) -> tuple[float, float, float]:
    return tuple(
        start_channel + (end_channel - start_channel) * fraction
        for start_channel, end_channel in zip(start, end)
    )


def monochrome_palette(base_color: str, count: int) -> list[str]:
    """
    Generate ordered shades from light to dark around one token brand color.

    Smaller transfer buckets receive lighter shades and larger buckets darker
    shades, reinforcing their numeric ordering visually.
    """
    if count <= 0:
        return []
    if count == 1:
        return [base_color]

    base = _hex_to_rgb(base_color)
    white = (255.0, 255.0, 255.0)
    black = (0.0, 0.0, 0.0)
    light = _interpolate_rgb(base, white, 0.72)
    dark = _interpolate_rgb(base, black, 0.32)

    palette: list[str] = []
    for index in range(count):
        position = index / (count - 1)
        if position <= 0.65:
            rgb = _interpolate_rgb(light, base, position / 0.65)
        else:
            rgb = _interpolate_rgb(base, dark, (position - 0.65) / 0.35)
        palette.append(_rgb_to_hex(rgb))
    return palette


def bucket_color_sequence(cfg: dict, count: int) -> list[str]:
    """Return configured bucket colors or derive shades from one base color."""
    configured = cfg.get("bucket_color_sequence")
    if configured:
        colors = list(configured)
        if len(colors) < count:
            raise ValueError(
                "bucket_color_sequence contains fewer colors than size buckets"
            )
        return colors[:count]

    base_color = cfg.get(
        "bucket_base_color",
        cfg.get("primary_color", "#009393"),
    )
    return monochrome_palette(base_color, count)


def add_time_window(df: pd.DataFrame, months: int) -> pd.DataFrame:
    """
    Add non-overlapping calendar windows, e.g. H1/H2 for six months.

    The number of months must divide 12 so windows do not cross year boundaries.
    """
    if months <= 0 or 12 % months != 0:
        raise ValueError("top_window_months must be one of 1, 2, 3, 4, 6, or 12")

    result = df.copy()
    result["month"] = pd.to_datetime(result["month"])
    start_month = ((result["month"].dt.month - 1) // months) * months + 1
    result["window_start"] = pd.to_datetime(
        {
            "year": result["month"].dt.year,
            "month": start_month,
            "day": 1,
        }
    )

    periods_per_year = 12 // months
    period_number = ((start_month - 1) // months) + 1

    if months == 6:
        result["window_label"] = (
            result["window_start"].dt.year.astype(str)
            + " H"
            + period_number.astype(str)
        )
    elif months == 3:
        result["window_label"] = (
            result["window_start"].dt.year.astype(str)
            + " Q"
            + period_number.astype(str)
        )
    elif months == 12:
        result["window_label"] = result["window_start"].dt.year.astype(str)
    else:
        result["window_label"] = result["window_start"].dt.strftime("%Y-%m")

    result["window_order"] = (
        result["window_start"].dt.year * periods_per_year + period_number
    )
    return result


# ---------------------------------------------------------------------------
# General activity and adoption plots
# ---------------------------------------------------------------------------


def plot_monthly_activity(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    df = read_csv(input_folder, "monthly_activity", cfg)
    if df is None or df.empty:
        return

    volume_col = cfg.get("volume_column", "token_volume")
    primary = cfg.get("primary_color", "#009393")
    secondary = cfg.get("secondary_color", "#E67E22")

    volume_labels = df[volume_col].map(human_number)
    count_labels = df["transfer_count"].map(human_number)

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=df["month"],
            y=df[volume_col],
            name=f"{cfg['token_symbol']} volume",
            marker_color=primary,
            customdata=volume_labels,
            hovertemplate="%{x|%b %Y}<br>Volume: %{customdata}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=df["month"],
            y=df["transfer_count"],
            name="Transfer count",
            mode="lines",
            yaxis="y2",
            line=dict(color=secondary, width=2),
            customdata=count_labels,
            hovertemplate="%{x|%b %Y}<br>Transfers: %{customdata}<extra></extra>",
        )
    )

    fig.update_layout(
        title=f"{cfg['title_prefix']}: Monthly transfer activity",
        xaxis=dict(title="Month"),
        yaxis=human_axis(df[volume_col], f"{cfg['token_symbol']} volume"),
        yaxis2={
            **human_axis(df["transfer_count"], "Transfer count"),
            "overlaying": "y",
            "side": "right",
            "showgrid": False,
        },
        hovermode="x unified",
        bargap=0.08,
    )
    save_fig(fig, output_folder / "monthly_activity.html", cfg)


def plot_monthly_users(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    df = read_csv(input_folder, "monthly_activity", cfg)
    if df is None or df.empty:
        return

    fig = go.Figure()
    for column, label in (
        ("unique_senders", "Unique senders"),
        ("unique_receivers", "Unique receivers"),
        ("active_addresses", "Active addresses"),
    ):
        fig.add_trace(
            go.Scatter(
                x=df["month"],
                y=df[column],
                name=label,
                mode="lines",
                customdata=df[column].map(human_number),
                hovertemplate=(
                    "%{x|%b %Y}<br>"
                    + label
                    + ": %{customdata}<extra></extra>"
                ),
            )
        )

    all_values = pd.concat(
        [df["unique_senders"], df["unique_receivers"], df["active_addresses"]]
    )
    fig.update_layout(
        title=f"{cfg['title_prefix']}: Monthly active addresses",
        xaxis=dict(title="Month"),
        yaxis=human_axis(all_values, "Addresses"),
        hovermode="x unified",
    )
    save_fig(fig, output_folder / "monthly_users.html", cfg)


def plot_adoption(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    df = read_csv(input_folder, "monthly_adoption", cfg)
    if df is None or df.empty:
        return

    primary = cfg.get("primary_color", "#009393")
    secondary = cfg.get("secondary_color", "#E67E22")
    df = df.copy()
    df["cumulative_adoption"] = df["newly_adopted_addresses"].cumsum()

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=df["month"],
            y=df["newly_adopted_addresses"],
            name="New addresses",
            marker_color=primary,
            customdata=df["newly_adopted_addresses"].map(human_number),
            hovertemplate="%{x|%b %Y}<br>New addresses: %{customdata}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=df["month"],
            y=df["cumulative_adoption"],
            name="Cumulative addresses",
            mode="lines",
            yaxis="y2",
            line=dict(color=secondary, width=2),
            customdata=df["cumulative_adoption"].map(human_number),
            hovertemplate=(
                "%{x|%b %Y}<br>Cumulative addresses: %{customdata}<extra></extra>"
            ),
        )
    )

    fig.update_layout(
        title=f"{cfg['title_prefix']}: Token adoption",
        xaxis=dict(title="Month"),
        yaxis=human_axis(df["newly_adopted_addresses"], "New addresses"),
        yaxis2={
            **human_axis(df["cumulative_adoption"], "Cumulative addresses"),
            "overlaying": "y",
            "side": "right",
            "showgrid": False,
        },
        hovermode="x unified",
        bargap=0.08,
    )
    save_fig(fig, output_folder / "monthly_adoption.html", cfg)


# ---------------------------------------------------------------------------
# Transfer-size distributions
# ---------------------------------------------------------------------------


def plot_transfer_size_histogram(
    input_folder: Path,
    output_folder: Path,
    cfg: dict,
) -> None:
    """
    Plot relative rather than absolute transfer-size distributions.

    Both bars sum to 100% independently:
    - share of all transfer events
    - share of all transferred token volume
    """
    df = read_csv(input_folder, "transfer_size_histogram_all_time", cfg)
    if df is None or df.empty:
        return

    df = df.copy()
    count_total = df["transfer_count"].sum()
    volume_total = df["bucket_volume"].sum()

    df["transfer_share"] = (
        df["transfer_count"] / count_total if count_total else 0.0
    )
    df["volume_share"] = df["bucket_volume"] / volume_total if volume_total else 0.0

    df, order = add_bucket_labels(df)
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=df["bucket_label"],
            y=df["transfer_share"],
            name="Share of transfers",
            customdata=(df["transfer_share"] * 100).round(2),
            hovertemplate=(
                "Bucket: %{x}<br>Share of transfers: %{customdata:.2f}%<extra></extra>"
            ),
        )
    )
    fig.add_trace(
        go.Bar(
            x=df["bucket_label"],
            y=df["volume_share"],
            name="Share of volume",
            customdata=(df["volume_share"] * 100).round(2),
            hovertemplate=(
                "Bucket: %{x}<br>Share of volume: %{customdata:.2f}%<extra></extra>"
            ),
        )
    )

    fig.update_layout(
        title=f"{cfg['title_prefix']}: Relative transfer-size distribution",
        xaxis=dict(
            title=f"Transfer size ({cfg['token_symbol']})",
            categoryorder="array",
            categoryarray=order,
        ),
        yaxis=dict(title="Share", tickformat=".0%", rangemode="tozero"),
        barmode="group",
        hovermode="x unified",
    )
    save_fig(fig, output_folder / "transfer_size_histogram_relative.html", cfg)


def _plot_monthly_bucket_stacked(
    df: pd.DataFrame,
    output_folder: Path,
    cfg: dict,
    value_column: str,
    value_title: str,
    output_stem: str,
) -> None:
    df, order = add_bucket_labels(df)
    colors = bucket_color_sequence(cfg, len(order))

    absolute = px.bar(
        df,
        x="month",
        y=value_column,
        color="bucket_label",
        category_orders={"bucket_label": order},
        color_discrete_sequence=colors,
        title=f"{cfg['title_prefix']}: Monthly transfer-size buckets by {value_title.lower()}",
    )
    monthly_totals = df.groupby("month")[value_column].sum()
    absolute.update_layout(
        barmode="stack",
        xaxis=dict(title="Month"),
        yaxis=human_axis(monthly_totals, value_title),
        hovermode="x unified",
        bargap=0.05,
        legend_traceorder="normal",
    )
    absolute.update_traces(hovertemplate="%{x|%b %Y}<br>%{fullData.name}: %{y:,.2f}<extra></extra>")
    save_fig(absolute, output_folder / f"{output_stem}_stacked.html", cfg)

    relative = df.copy()
    totals = relative.groupby("month")[value_column].transform("sum")
    relative["relative_share"] = relative[value_column].div(totals.where(totals != 0))
    relative["relative_share"] = relative["relative_share"].fillna(0.0)

    relative_fig = px.bar(
        relative,
        x="month",
        y="relative_share",
        color="bucket_label",
        category_orders={"bucket_label": order},
        color_discrete_sequence=colors,
        title=(
            f"{cfg['title_prefix']}: Relative monthly transfer-size buckets "
            f"by {value_title.lower()}"
        ),
    )
    relative_fig.update_layout(
        barmode="stack",
        xaxis=dict(title="Month"),
        yaxis=dict(title="Monthly share", tickformat=".0%", range=[0, 1]),
        hovermode="x unified",
        bargap=0.05,
        legend_traceorder="normal",
    )
    relative_fig.update_traces(
        hovertemplate="%{x|%b %Y}<br>%{fullData.name}: %{y:.2%}<extra></extra>"
    )
    save_fig(
        relative_fig,
        output_folder / f"{output_stem}_relative_stacked.html",
        cfg,
    )


def plot_monthly_transfer_size_buckets_count(
    input_folder: Path,
    output_folder: Path,
    cfg: dict,
) -> None:
    df = read_csv(input_folder, "monthly_transfer_size_buckets", cfg)
    if df is None or df.empty:
        return

    _plot_monthly_bucket_stacked(
        df=df,
        output_folder=output_folder,
        cfg=cfg,
        value_column="transfer_count",
        value_title="Transfer count",
        output_stem="monthly_transfer_size_buckets_count",
    )


def plot_monthly_transfer_size_buckets_volume(
    input_folder: Path,
    output_folder: Path,
    cfg: dict,
) -> None:
    df = read_csv(input_folder, "monthly_transfer_size_buckets", cfg)
    if df is None or df.empty:
        return

    _plot_monthly_bucket_stacked(
        df=df,
        output_folder=output_folder,
        cfg=cfg,
        value_column="bucket_volume",
        value_title=f"{cfg['token_symbol']} volume",
        output_stem="monthly_transfer_size_buckets_volume",
    )


# ---------------------------------------------------------------------------
# Top-address plots
# ---------------------------------------------------------------------------


def plot_overall_top_addresses(
    df: pd.DataFrame,
    output_folder: Path,
    cfg: dict,
    *,
    address_column: str,
    value_column: str,
    title: str,
    axis_title: str,
    output_name: str,
) -> None:
    top_n = int(cfg.get("top_n", 20))
    primary = cfg.get("primary_color", "#009393")

    ranked = (
        df.groupby(address_column, as_index=False)[value_column]
        .sum()
        .nlargest(top_n, value_column)
        .sort_values(value_column, ascending=False)
        .reset_index(drop=True)
    )
    if ranked.empty:
        return

    ranked[address_column] = ranked[address_column].map(normalize_address)
    ranked["rank"] = ranked.index + 1
    ranked["address_label"] = ranked.apply(
        lambda row: f"#{int(row['rank']):02d}  {short_address(row[address_column])}",
        axis=1,
    )
    ranked["formatted_value"] = ranked[value_column].map(human_number)
    plotted = ranked.sort_values(value_column, ascending=True)

    fig = go.Figure(
        go.Bar(
            x=plotted[value_column],
            y=plotted["address_label"],
            orientation="h",
            marker_color=primary,
            customdata=list(zip(
                plotted[address_column],
                plotted["formatted_value"],
                plotted["rank"],
            )),
            hovertemplate=(
                "Rank: %{customdata[2]}<br>"
                "Address: %{customdata[0]}<br>"
                + axis_title
                + ": %{customdata[1]}<br>"
                "Click to copy address<extra></extra>"
            ),
        )
    )
    fig.update_layout(
        title=title,
        xaxis=human_axis(plotted[value_column], axis_title),
        yaxis=dict(title="Rank and address"),
        height=max(640, 31 * len(plotted) + 180),
        margin=dict(l=155, r=40, t=90, b=70),
    )
    save_address_fig(
        fig,
        output_folder / output_name,
        cfg,
        ranked,
        address_column=address_column,
        value_column=value_column,
        value_label=axis_title,
    )



def plot_six_month_address_views(
    df: pd.DataFrame,
    output_folder: Path,
    cfg: dict,
    *,
    address_column: str,
    value_column: str,
    title_subject: str,
    axis_title: str,
    output_name: str,
) -> None:
    """Create stacked and bubble-matrix views for funders or top senders."""
    top_n = int(cfg.get("top_n", 20))
    stack_top_n = min(int(cfg.get("address_stack_top_n", 5)), top_n)
    window_months = int(cfg.get("top_window_months", 6))

    windowed = add_time_window(df, window_months)
    windowed[address_column] = windowed[address_column].map(normalize_address)
    overall_totals = (
        windowed.groupby(address_column, as_index=False)[value_column]
        .sum()
        .nlargest(top_n, value_column)
        .sort_values(value_column, ascending=False)
        .reset_index(drop=True)
    )
    if overall_totals.empty:
        return

    overall_totals["rank"] = overall_totals.index + 1
    top_addresses = overall_totals[address_column].tolist()
    rank_lookup = dict(zip(overall_totals[address_column], overall_totals["rank"]))
    windowed = windowed[windowed[address_column].isin(top_addresses)]

    grouped = (
        windowed.groupby(
            ["window_start", "window_order", "window_label", address_column],
            as_index=False,
        )[value_column]
        .sum()
    )
    if grouped.empty:
        return

    ordered_windows = (
        grouped[["window_order", "window_label"]]
        .drop_duplicates()
        .sort_values("window_order")["window_label"]
        .tolist()
    )

    # Readable stacked chart: only the top few addresses plus one remainder.
    stack_addresses = set(top_addresses[:stack_top_n])
    grouped["series_address"] = grouped[address_column].where(
        grouped[address_column].isin(stack_addresses),
        "Other top addresses",
    )
    grouped["series_label"] = grouped["series_address"].map(
        lambda address: (
            "Other top addresses"
            if address == "Other top addresses"
            else f"#{rank_lookup[address]:02d} {short_address(address)}"
        )
    )
    stack_grouped = (
        grouped.groupby(
            ["window_order", "window_label", "series_address", "series_label"],
            as_index=False,
        )[value_column]
        .sum()
    )
    stack_grouped["formatted_value"] = stack_grouped[value_column].map(human_number)
    stack_grouped["full_address"] = stack_grouped["series_address"].map(
        lambda value: value if value != "Other top addresses" else ""
    )

    series_order = [
        f"#{rank_lookup[address]:02d} {short_address(address)}"
        for address in top_addresses[:stack_top_n]
    ] + ["Other top addresses"]
    palette = color_sequence(cfg)
    color_map = {
        label: palette[index % len(palette)]
        for index, label in enumerate(series_order[:-1])
    }
    color_map["Other top addresses"] = cfg.get("address_other_color", "#C9D1D9")

    stack_fig = px.bar(
        stack_grouped,
        x="window_label",
        y=value_column,
        color="series_label",
        custom_data=["full_address", "formatted_value"],
        category_orders={
            "window_label": ordered_windows,
            "series_label": series_order,
        },
        color_discrete_map=color_map,
        title=(
            f"{cfg['title_prefix']}: {stack_top_n} leading overall {title_subject} "
            f"plus the remainder across {window_months}-month windows"
        ),
    )
    stack_fig.update_traces(
        marker_line_color="white",
        marker_line_width=0.5,
        hovertemplate=(
            "Window: %{x}<br>"
            "Series: %{fullData.name}<br>"
            + axis_title
            + ": %{customdata[1]}<extra></extra>"
        ),
    )
    window_totals = stack_grouped.groupby("window_label")[value_column].sum()
    stack_fig.update_layout(
        barmode="stack",
        xaxis=dict(title=f"{window_months}-month window"),
        yaxis=human_axis(window_totals, axis_title),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=-0.30, xanchor="left", x=0),
        margin=dict(l=75, r=40, t=95, b=145),
    )
    save_address_fig(
        stack_fig,
        output_folder / output_name,
        cfg,
        overall_totals,
        address_column=address_column,
        value_column=value_column,
        value_label=f"Overall {axis_title}",
    )

    # Detailed view: one bubble per address/window observation.
    # Bubble AREA is proportional to the absolute value, while every bubble
    # uses the same token-specific color. This keeps the visual encoding
    # straightforward: larger bubble = more activity.
    pivot = grouped.pivot_table(
        index=address_column,
        columns="window_label",
        values=value_column,
        aggfunc="sum",
        fill_value=0,
    ).reindex(index=top_addresses, columns=ordered_windows, fill_value=0)

    window_sums = pivot.sum(axis=0).replace(0, pd.NA)
    shares = pivot.div(window_sums, axis=1).fillna(0.0)
    overall_lookup = dict(
        zip(overall_totals[address_column], overall_totals[value_column])
    )

    bubble_rows: list[dict[str, object]] = []
    for address in top_addresses:
        rank = int(rank_lookup[address])
        row_label = f"#{rank:02d}  {short_address(address)}"
        overall_value = float(overall_lookup[address])

        for window in ordered_windows:
            value = float(pivot.loc[address, window])
            if value <= 0:
                continue

            bubble_rows.append(
                {
                    "window_label": window,
                    "address_label": row_label,
                    "address": address,
                    "rank": rank,
                    "value": value,
                    "formatted_value": human_number(value),
                    "window_share": float(shares.loc[address, window]),
                    "overall_value": overall_value,
                    "formatted_overall_value": human_number(overall_value),
                }
            )

    bubble_df = pd.DataFrame(bubble_rows)
    if bubble_df.empty:
        return

    max_value = float(bubble_df["value"].max())
    # Keep circles smaller than the vertical row spacing so adjacent rows do
    # not visually overlap. All values remain configurable in plot_config.yaml.
    row_height = float(cfg.get("address_bubble_row_height", 48))
    column_width = float(cfg.get("address_bubble_column_width", 56))
    max_marker_size = float(cfg.get("address_bubble_max_size", 40))
    max_marker_size = min(max_marker_size, row_height * 0.82)
    min_marker_size = float(cfg.get("address_bubble_min_size", 3.5))
    bubble_opacity = float(cfg.get("address_bubble_opacity", 0.82))

    # Plotly's area mode makes marker area—not diameter—proportional to the
    # underlying metric. This is the least misleading way to encode magnitude
    # with circles.
    size_ref = (
        2.0 * max_value / (max_marker_size**2)
        if max_value > 0 and max_marker_size > 0
        else 1.0
    )

    primary = cfg.get("primary_color", "#009393")
    border_color = cfg.get("address_bubble_border_color", "#FFFFFF")

    bubble_fig = go.Figure()
    bubble_fig.add_trace(
        go.Scatter(
            x=bubble_df["window_label"],
            y=bubble_df["address_label"],
            mode="markers",
            name=axis_title,
            customdata=list(
                zip(
                    bubble_df["address"],
                    bubble_df["rank"],
                    bubble_df["formatted_value"],
                    bubble_df["window_share"],
                    bubble_df["formatted_overall_value"],
                )
            ),
            marker=dict(
                size=bubble_df["value"],
                sizemode="area",
                sizeref=size_ref,
                sizemin=min_marker_size,
                color=primary,
                opacity=bubble_opacity,
                line=dict(color=border_color, width=1.0),
            ),
            hovertemplate=(
                "Window: %{x}<br>"
                "Overall rank: %{customdata[1]}<br>"
                "Address: %{customdata[0]}<br>"
                + axis_title
                + ": %{customdata[2]}<br>"
                "Share among displayed top addresses in this window: "
                "%{customdata[3]:.2%}<br>"
                "Overall "
                + axis_title.lower()
                + ": %{customdata[4]}<br>"
                "Click to copy address<extra></extra>"
            ),
        )
    )

    # Add a compact size key. These traces do not create visible data points;
    # they only show reference bubble sizes in the legend.
    reference_values = []
    for fraction in (0.10, 0.35, 1.00):
        value = max_value * fraction
        if value > 0:
            reference_values.append(value)

    # Deduplicate labels after compact number formatting.
    seen_reference_labels: set[str] = set()
    for value in reference_values:
        label = human_number(value)
        if label in seen_reference_labels:
            continue
        seen_reference_labels.add(label)
        legend_size = max(
            min_marker_size,
            math.sqrt(value / size_ref) if size_ref > 0 else min_marker_size,
        )
        bubble_fig.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                name=label,
                marker=dict(
                    size=legend_size,
                    color=primary,
                    opacity=bubble_opacity,
                    line=dict(color=border_color, width=1.0),
                ),
                hoverinfo="skip",
                showlegend=True,
            )
        )

    row_labels = [
        f"#{int(rank_lookup[address]):02d}  {short_address(address)}"
        for address in top_addresses
    ]
    # A fixed, compact width keeps half-year columns close together, while
    # generous row height gives large bubbles enough vertical breathing room.
    left_margin = int(cfg.get("address_bubble_left_margin", 205))
    right_margin = int(cfg.get("address_bubble_right_margin", 55))
    min_width = int(cfg.get("address_bubble_min_width", 980))
    max_width = int(cfg.get("address_bubble_max_width", 1450))
    chart_width = int(
        max(
            min_width,
            min(
                max_width,
                left_margin + right_margin + column_width * len(ordered_windows),
            ),
        )
    )
    chart_height = int(
        max(
            820,
            row_height * len(row_labels) + 260,
        )
    )

    bubble_fig.update_layout(
        title=(
            f"{cfg['title_prefix']}: Persistence, turnover, and magnitude of "
            f"the overall top {top_n} {title_subject}"
        ),
        width=chart_width,
        height=chart_height,
        autosize=False,
        xaxis=dict(
            title=f"{window_months}-month window",
            categoryorder="array",
            categoryarray=ordered_windows,
            showgrid=True,
            gridcolor="rgba(120, 140, 160, 0.18)",
            tickangle=0,
            tickfont=dict(size=int(cfg.get("address_bubble_x_tick_size", 11))),
            automargin=True,
        ),
        yaxis=dict(
            title="Overall rank and address",
            categoryorder="array",
            categoryarray=row_labels,
            autorange="reversed",
            showgrid=True,
            gridcolor="rgba(120, 140, 160, 0.14)",
            tickfont=dict(size=int(cfg.get("address_bubble_y_tick_size", 11))),
            automargin=True,
        ),
        margin=dict(l=left_margin, r=right_margin, t=105, b=110),
        legend=dict(
            title=f"Bubble area = {axis_title}",
            orientation="h",
            yanchor="bottom",
            y=-0.22,
            xanchor="left",
            x=0,
            itemsizing="trace",
        ),
        hovermode="closest",
    )

    bubble_name = output_name.replace("_stacked.html", "_bubble.html")
    save_address_fig(
        bubble_fig,
        output_folder / bubble_name,
        cfg,
        overall_totals,
        address_column=address_column,
        value_column=value_column,
        value_label=f"Overall {axis_title}",
    )


def plot_yearly_top_addresses(
    df: pd.DataFrame,
    output_folder: Path,
    cfg: dict,
    *,
    address_column: str,
    value_column: str,
    title_subject: str,
    axis_title: str,
    output_subfolder: str,
) -> None:
    """Write one horizontal top-N bar chart for each calendar year."""
    top_n = int(cfg.get("yearly_top_n", cfg.get("top_n", 20)))
    primary = cfg.get("primary_color", "#009393")
    yearly = df.copy()
    yearly["year"] = pd.to_datetime(yearly["month"]).dt.year

    for year, year_df in yearly.groupby("year", sort=True):
        ranked = (
            year_df.groupby(address_column, as_index=False)[value_column]
            .sum()
            .nlargest(top_n, value_column)
            .sort_values(value_column, ascending=False)
            .reset_index(drop=True)
        )
        if ranked.empty:
            continue

        ranked[address_column] = ranked[address_column].map(normalize_address)
        ranked["rank"] = ranked.index + 1
        ranked["address_label"] = ranked.apply(
            lambda row: f"#{int(row['rank']):02d}  {short_address(row[address_column])}",
            axis=1,
        )
        ranked["formatted_value"] = ranked[value_column].map(human_number)
        plotted = ranked.sort_values(value_column, ascending=True)

        fig = go.Figure(
            go.Bar(
                x=plotted[value_column],
                y=plotted["address_label"],
                orientation="h",
                marker_color=primary,
                customdata=list(zip(
                    plotted[address_column],
                    plotted["formatted_value"],
                    plotted["rank"],
                )),
                hovertemplate=(
                    "Rank: %{customdata[2]}<br>"
                    "Address: %{customdata[0]}<br>"
                    + axis_title
                    + ": %{customdata[1]}<br>"
                    "Click to copy address<extra></extra>"
                ),
            )
        )
        fig.update_layout(
            title=f"{cfg['title_prefix']}: Top {top_n} {title_subject} in {year}",
            xaxis=human_axis(plotted[value_column], axis_title),
            yaxis=dict(title="Rank and address"),
            height=max(640, 31 * len(plotted) + 180),
            margin=dict(l=155, r=40, t=90, b=70),
        )
        save_address_fig(
            fig,
            output_folder / output_subfolder / f"{int(year)}.html",
            cfg,
            ranked,
            address_column=address_column,
            value_column=value_column,
            value_label=axis_title,
        )

def plot_top100_users(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    df = read_csv(input_folder, "monthly_top100_users", cfg)
    if df is None or df.empty:
        return

    plot_overall_top_addresses(
        df,
        output_folder,
        cfg,
        address_column="address",
        value_column="outgoing_volume",
        title=(
            f"{cfg['title_prefix']}: Top {cfg.get('top_n', 20)} addresses "
            "by outgoing volume"
        ),
        axis_title=f"Outgoing {cfg['token_symbol']} volume",
        output_name="top20_users_total_volume.html",
    )

    plot_six_month_address_views(
        df,
        output_folder,
        cfg,
        address_column="address",
        value_column="outgoing_volume",
        title_subject="addresses by outgoing volume",
        axis_title=f"Outgoing {cfg['token_symbol']} volume",
        output_name="top20_users_6month_stacked.html",
    )

    plot_yearly_top_addresses(
        df,
        output_folder,
        cfg,
        address_column="address",
        value_column="outgoing_volume",
        title_subject="addresses by outgoing volume",
        axis_title=f"Outgoing {cfg['token_symbol']} volume",
        output_subfolder="yearly_top20_users",
    )


def plot_top100_funded_by(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    df = read_csv(input_folder, "monthly_top100_funded_by", cfg)
    if df is None or df.empty:
        return

    # Each newly_funded_addresses observation represents an address receiving
    # the token for the first time. This is the available "number of fundings"
    # measure in the current aggregation output.
    plot_overall_top_addresses(
        df,
        output_folder,
        cfg,
        address_column="funded_by_address",
        value_column="newly_funded_addresses",
        title=(
            f"{cfg['title_prefix']}: Top {cfg.get('top_n', 20)} funding "
            "addresses by number of newly funded addresses"
        ),
        axis_title="Newly funded addresses",
        output_name="top20_funders_new_addresses.html",
    )

    plot_six_month_address_views(
        df,
        output_folder,
        cfg,
        address_column="funded_by_address",
        value_column="newly_funded_addresses",
        title_subject="funding addresses",
        axis_title="Newly funded addresses",
        output_name="top20_funders_6month_stacked.html",
    )

    plot_yearly_top_addresses(
        df,
        output_folder,
        cfg,
        address_column="funded_by_address",
        value_column="newly_funded_addresses",
        title_subject="funding addresses",
        axis_title="Newly funded addresses",
        output_subfolder="yearly_top20_funders",
    )


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------


def plot_dataset(dataset_cfg: dict) -> None:
    input_folder = resolve_project_path(dataset_cfg["input_folder"])
    output_folder = resolve_project_path(dataset_cfg["output_folder"])
    output_folder.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Plotting {dataset_cfg['name']} ===")
    print(f"[input]  {input_folder}")
    print(f"[output] {output_folder}")

    plot_monthly_activity(input_folder, output_folder, dataset_cfg)
    plot_monthly_users(input_folder, output_folder, dataset_cfg)
    plot_adoption(input_folder, output_folder, dataset_cfg)
    plot_transfer_size_histogram(input_folder, output_folder, dataset_cfg)
    plot_monthly_transfer_size_buckets_count(
        input_folder, output_folder, dataset_cfg
    )
    plot_monthly_transfer_size_buckets_volume(
        input_folder, output_folder, dataset_cfg
    )
    plot_top100_users(input_folder, output_folder, dataset_cfg)
    plot_top100_funded_by(input_folder, output_folder, dataset_cfg)


def run_plot_pipeline() -> None:
    cfg = load_config(CONFIG_PATH)
    global_cfg = cfg.get("global", {})
    datasets = cfg.get("datasets", [])

    if not datasets:
        raise ValueError("No datasets defined in config/plot_config.yaml")

    for dataset in datasets:
        dataset_cfg = {**global_cfg, **dataset}
        plot_dataset(dataset_cfg)


if __name__ == "__main__":
    run_plot_pipeline()
