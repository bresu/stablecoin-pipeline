from __future__ import annotations

import argparse
import base64
import math
import mimetypes
import shutil
from html import escape
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "plot_config.yaml"


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------


def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Plot configuration not found: {path}. "
            "Expected config/plot_config.yaml."
        )
    with open(path, "r", encoding="utf-8") as file:
        config = yaml.safe_load(file)
    return config or {}


def resolve_project_path(path_str: str | Path) -> Path:
    # expanduser() allows dedicated configs to write directly to paths such as
    # ~/Desktop while preserving the existing project-relative behavior.
    path = Path(path_str).expanduser()
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
        # End date remains exclusive.
        result = result[result["month"] < pd.to_datetime(end_date)]
    return result.sort_values("month")


def find_file(
    folder: Path,
    suffix: str,
    prefix: str | None = None,
) -> Path | None:
    if prefix:
        exact = folder / f"{prefix}_{suffix}.csv"
        return exact if exact.exists() else None
    matches = sorted(folder.glob(f"*{suffix}.csv"))
    return matches[0] if matches else None


def read_csv(
    input_folder: Path,
    suffix: str,
    cfg: dict,
) -> pd.DataFrame | None:
    file = find_file(input_folder, suffix, cfg.get("file_prefix"))
    if file is None:
        prefix = cfg.get("file_prefix")
        expected = f"{prefix}_{suffix}.csv" if prefix else f"*{suffix}.csv"
        print(f"[skip] {expected} not found in {input_folder}")
        return None
    df = pd.read_csv(file)
    return filter_period(df, cfg.get("start_date"), cfg.get("end_date"))


def _asset_data_uri(path_value: str | Path | None) -> str | None:
    """Return a self-contained data URI for a local PNG/SVG logo."""
    if not path_value:
        return None
    path = resolve_project_path(path_value)
    if not path.exists():
        print(f"[logo-skip] chain logo not found: {path}")
        return None
    mime, _ = mimetypes.guess_type(path.name)
    if mime not in {"image/png", "image/svg+xml", "image/jpeg", "image/webp"}:
        print(f"[logo-skip] unsupported logo type: {path}")
        return None
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _apply_common_layout(fig: go.Figure, cfg: dict) -> None:
    # Titles and chain logos live in the surrounding HTML header, not in Plotly.
    current_margin = fig.layout.margin.to_plotly_json()
    if not current_margin:
        current_margin = dict(l=70, r=50, t=55, b=70)
    else:
        current_margin = dict(current_margin)

    fig.update_layout(
        template=cfg.get("template", "plotly_white"),
        font=dict(size=cfg.get("font_size", 13)),
        title=None,
        legend=dict(title_text=""),
        margin=current_margin,
        width=None,
        autosize=True,
    )


def _figure_header_html(title: str, cfg: dict) -> str:
    # Per-chain figures use one logo. Cross-chain figures may provide multiple
    # logos; keeping them in the external HTML header prevents Plotly layout
    # changes from moving/scaling the branding.
    configured_paths = cfg.get("chain_logo_paths")
    if configured_paths:
        paths = list(configured_paths)
    elif cfg.get("chain_logo_path"):
        paths = [cfg.get("chain_logo_path")]
    else:
        paths = []

    configured_labels = list(cfg.get("chain_logo_labels") or [])
    fallback_label = str(cfg.get("chain_label") or cfg.get("chain") or "Chain")
    images = []
    for index, path in enumerate(paths):
        logo_uri = _asset_data_uri(path)
        if not logo_uri:
            continue
        label = configured_labels[index] if index < len(configured_labels) else fallback_label
        images.append(
            f'<img class="chain-logo" src="{logo_uri}" alt="{escape(str(label))} logo">'
        )

    multi_class = " multi-logo" if len(images) > 1 else ""
    logo_html = f'<div class="chain-logo-slot{multi_class}">{"".join(images)}</div>'

    return (
        '<header class="figure-header">'
        f'<div id="editable-figure-title" class="figure-title" contenteditable="true" '
        f'spellcheck="false">{escape(title)}</div>'
        f'{logo_html}'
        '</header>'
    )


def _figure_shell_css(cfg: dict) -> str:
    logo_px = int(cfg.get("html_logo_px", 80))
    logo_slot_px = int(cfg.get("html_logo_slot_px", max(96, logo_px + 16)))
    title_px = int(cfg.get("html_title_font_px", 26))
    header_gap = int(cfg.get("html_header_gap_px", 20))
    side_pad = int(cfg.get("html_header_side_padding_px", 28))
    top_pad = int(cfg.get("html_header_top_padding_px", 22))
    bottom_pad = int(cfg.get("html_header_bottom_padding_px", 12))
    return f"""
  :root {{ --header-logo-size: {logo_px}px; --header-logo-slot: {logo_slot_px}px; }}
  html, body {{ margin: 0; padding: 0; background: white; }}
  body {{ font-family: Arial, sans-serif; color: #243b5a; }}
  .figure-shell {{ width: 100%; background: white; }}
  .figure-header {{
    box-sizing: border-box;
    width: 100%;
    display: grid;
    grid-template-columns: minmax(0, 1fr) var(--header-logo-slot);
    column-gap: {header_gap}px;
    align-items: center;
    padding: {top_pad}px {side_pad}px {bottom_pad}px {side_pad}px;
    background: white;
  }}
  .figure-title {{
    min-width: 0;
    font-size: {title_px}px;
    line-height: 1.22;
    font-weight: 500;
    color: #243b5a;
    outline: none;
    overflow-wrap: anywhere;
    cursor: text;
  }}
  .figure-title:focus {{ background: #f8fafc; border-radius: 4px; }}
  .chain-logo-slot {{
    width: var(--header-logo-slot);
    height: var(--header-logo-slot);
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
  }}
  .chain-logo {{
    width: var(--header-logo-size);
    height: var(--header-logo-size);
    object-fit: contain;
    display: block;
    flex: 0 0 auto;
  }}
  .chain-logo-slot.multi-logo .chain-logo {{
    width: min(var(--header-logo-size), calc((var(--header-logo-slot) - 12px) / 2));
    height: min(var(--header-logo-size), calc((var(--header-logo-slot) - 12px) / 2));
  }}
  .chart-wrap {{
    width: 100%;
    display: flex;
    justify-content: center;
    align-items: flex-start;
    overflow: hidden;
  }}
  .chart-wrap > div {{ width: 100% !important; max-width: none !important; }}
  .chart-wrap .plotly-graph-div {{ width: 100% !important; max-width: none !important; }}
  @media print {{
    .no-render, .address-panel, #copy-toast {{ display: none !important; }}
    .figure-header {{ break-inside: avoid; }}
  }}
"""


def _editable_header_script(default_filename: str) -> str:
    safe_filename = default_filename.replace('\\', '_').replace('"', '')
    return f"""<script>
(function() {{
  const titleEl = document.getElementById('editable-figure-title');
  if (titleEl) {{
    titleEl.addEventListener('input', () => {{
      const text = titleEl.innerText.trim();
      if (text) document.title = text;
    }});
  }}

  function saveEditedHtml() {{
    const clone = document.documentElement.cloneNode(true);
    clone.querySelectorAll('.plotly-graph-div').forEach((plot) => {{ plot.innerHTML = ''; }});
    clone.querySelectorAll('.modebar-container').forEach((node) => node.remove());
    const html = '<!doctype html>\\n' + clone.outerHTML;
    const blob = new Blob([html], {{type: 'text/html;charset=utf-8'}});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = '{safe_filename}';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }}

  document.addEventListener('keydown', (event) => {{
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {{
      event.preventDefault();
      saveEditedHtml();
    }}
  }});
}})();
</script>"""


def _wrap_standard_figure_html(plot_html: str, title: str, cfg: dict, output_path: Path) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{_figure_shell_css(cfg)}</style>
</head>
<body>
<div class="figure-shell">
{_figure_header_html(title, cfg)}
<div class="chart-wrap">{plot_html}</div>
</div>
{_editable_header_script(output_path.name)}
</body>
</html>
"""

def _static_path_for(html_path: Path, cfg: dict, *, suffix: str = ".pdf") -> Path | None:
    if not cfg.get("export_pdf", True):
        return None
    static_folder = cfg.get("static_output_folder")
    output_folder = cfg.get("output_folder")
    if not static_folder or not output_folder:
        return None

    html_root = resolve_project_path(output_folder)
    static_root = resolve_project_path(static_folder)
    try:
        relative = html_path.relative_to(html_root)
    except ValueError:
        relative = Path(html_path.name)
    return static_root / relative.with_suffix(suffix)


def _figure_contains_heatmap(fig: go.Figure) -> bool:
    """Return True when the figure contains a Plotly heatmap trace.

    Kaleido's vector PDF renderer can visually interpolate heatmap cells and
    introduce gradients that are absent from the browser rendering.  Heatmaps
    are therefore exported as high-resolution PNGs, while ordinary line/bar/
    scatter figures remain vector PDFs.
    """
    return any(getattr(trace, "type", None) in {"heatmap", "histogram2d"} for trace in fig.data)


def _save_static_pdf(fig: go.Figure, html_path: Path, cfg: dict) -> None:
    """Export the thesis/static version of a figure.

    Ordinary figures are exported as vector PDFs. Heatmaps keep the historical
    high-resolution PNG default unless `heatmap_static_format: pdf` is set in
    the active config. This lets the dedicated divergence config emit exactly
    the requested HTML + PDF pair without changing the normal plotting run.
    """
    is_heatmap = _figure_contains_heatmap(fig)
    heatmap_format = str(cfg.get("heatmap_static_format", "png")).strip().lower()
    if heatmap_format not in {"png", "pdf"}:
        raise ValueError(
            "heatmap_static_format must be either 'png' or 'pdf' "
            f"(got {heatmap_format!r})"
        )

    image_format = heatmap_format if is_heatmap else "pdf"
    suffix = f".{image_format}"
    static_path = _static_path_for(html_path, cfg, suffix=suffix)
    if static_path is None:
        return

    static_path.parent.mkdir(parents=True, exist_ok=True)
    width = int(fig.layout.width or cfg.get("static_width", 1400))
    height = int(fig.layout.height or cfg.get("static_height", 800))
    scale = float(
        cfg.get("heatmap_png_scale", 2.5)
        if is_heatmap and image_format == "png"
        else cfg.get("static_scale", 1.0)
    )

    # In the normal configuration, remove obsolete PDF heatmaps when the
    # canonical heatmap output remains PNG. The divergence-only config opts
    # into PDF and therefore skips this cleanup.
    if is_heatmap and image_format == "png":
        stale_pdf = _static_path_for(html_path, cfg, suffix=".pdf")
        if stale_pdf is not None and stale_pdf.exists():
            try:
                stale_pdf.unlink()
                print(f"[static-clean] removed stale heatmap PDF {stale_pdf}")
            except OSError:
                pass

    try:
        fig.write_image(
            str(static_path),
            format=image_format,
            width=width,
            height=height,
            scale=scale,
        )
        print(f"[{image_format}]  {static_path}")
    except Exception as exc:
        message = str(exc).strip() or repr(exc)
        print(f"[{image_format}-skip] {static_path}: {message}")
        lower = message.lower()
        if "kaleido" in lower or "chrome" in lower or "chromium" in lower:
            print(
                "           Static export dependency issue: install/upgrade "
                "Kaleido and ensure Chrome/Chromium is available."
            )


def save_fig(fig: go.Figure, output_path: Path, cfg: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    title = str(fig.layout.title.text or output_path.stem)
    _apply_common_layout(fig, cfg)

    plot_html = fig.to_html(
        full_html=False,
        include_plotlyjs=cfg.get("include_plotlyjs", "cdn"),
        div_id="plotly-chart",
        config={"displaylogo": False, "responsive": True},
    )
    html = _wrap_standard_figure_html(plot_html, title, cfg, output_path)
    output_path.write_text(html, encoding="utf-8")
    print(f"[html] {output_path}")

    # Final thesis rendering should come from the edited HTML so the external
    # title/logo header is captured exactly as seen in the browser.
    _save_static_pdf(fig, output_path, cfg)

def normalize_address(address: object) -> str:
    text = str(address).strip()
    if not text.startswith("0x"):
        text = f"0x{text}"
    return text.lower()


def short_address(address: object, leading: int = 6, trailing: int = 4) -> str:
    text = normalize_address(address)
    if len(text) <= leading + trailing + 3:
        return text
    return f"{text[: leading + 2]}…{text[-trailing:]}"


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
    output_path.parent.mkdir(parents=True, exist_ok=True)
    title = str(fig.layout.title.text or "Address chart")
    _apply_common_layout(fig, cfg)

    _save_static_pdf(fig, output_path, cfg)

    plot_html = fig.to_html(
        full_html=False,
        include_plotlyjs=cfg.get("include_plotlyjs", "cdn"),
        div_id="address-plot",
        config={"displaylogo": False, "responsive": True},
    )

    explorer_base = cfg.get("explorer_address_url", "https://etherscan.io/address/")
    explorer_label = cfg.get("explorer_label") or ("BscScan" if str(cfg.get("chain", "")).lower() == "bsc" else "Etherscan")
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
            f"<td><a href='{escape(explorer_url)}' target='_blank' rel='noopener noreferrer'>{escape(str(explorer_label))} ↗</a></td>"
            "</tr>"
        )

    extra_css = """
  .address-panel { margin: 10px 28px 30px; border: 1px solid #dfe6ee; border-radius: 8px; overflow: hidden; }
  .address-panel summary { cursor: pointer; padding: 12px 14px; font-weight: 600; background: #f7f9fb; }
  .address-controls { padding: 12px 14px; border-top: 1px solid #dfe6ee; }
  #address-search { width: min(520px, 95%); padding: 8px 10px; border: 1px solid #c8d2de; border-radius: 5px; }
  .table-scroll { overflow-x: auto; max-height: 520px; overflow-y: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 8px 10px; border-top: 1px solid #e6ebf0; text-align: left; }
  th { position: sticky; top: 0; background: #f7f9fb; z-index: 1; }
  td.numeric { text-align: right; font-variant-numeric: tabular-nums; }
  code { user-select: all; white-space: nowrap; }
  button.copy-address { cursor: pointer; border: 1px solid #b7c2cf; background: white; border-radius: 4px; padding: 5px 9px; }
  button.copy-address:hover { background: #eef4f7; }
  a { color: #126b58; text-decoration: none; }
  a:hover { text-decoration: underline; }
  #copy-toast { position: fixed; right: 20px; bottom: 20px; padding: 10px 14px; border-radius: 6px; background: #173f35; color: white; opacity: 0; pointer-events: none; transition: opacity .2s; z-index: 9999; }
"""

    address_script = """<script>
(function() {
  const plot = document.getElementById('address-plot');
  const toast = document.getElementById('copy-toast');
  function showToast(message) {
    toast.textContent = message;
    toast.style.opacity = '1';
    window.clearTimeout(window.__addressToastTimer);
    window.__addressToastTimer = window.setTimeout(() => { toast.style.opacity = '0'; }, 1400);
  }
  async function copyAddress(address) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(address);
      } else {
        const area = document.createElement('textarea');
        area.value = address;
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.focus(); area.select(); document.execCommand('copy'); area.remove();
      }
      showToast('Copied ' + address);
    } catch (error) { showToast('Copy failed — select it in the table'); }
  }
  if (plot && plot.on) {
    plot.on('plotly_click', function(eventData) {
      const point = eventData && eventData.points && eventData.points[0];
      if (!point || !point.customdata) return;
      const address = Array.isArray(point.customdata) ? point.customdata[0] : point.customdata;
      if (typeof address === 'string' && address.startsWith('0x')) copyAddress(address);
    });
  }
  document.querySelectorAll('.copy-address').forEach((button) => {
    button.addEventListener('click', () => copyAddress(button.dataset.address));
  });
  const search = document.getElementById('address-search');
  if (search) {
    search.addEventListener('input', () => {
      const needle = search.value.trim().toLowerCase();
      document.querySelectorAll('#address-table tbody tr').forEach((row) => {
        row.style.display = row.textContent.toLowerCase().includes(needle) ? '' : 'none';
      });
    });
  }
})();
</script>"""

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{_figure_shell_css(cfg)}{extra_css}</style>
</head>
<body>
<div class="figure-shell">
{_figure_header_html(title, cfg)}
<div class="chart-wrap">{plot_html}</div>
</div>
<details class="address-panel no-render">
  <summary>Addresses and lookup links ({len(rows)})</summary>
  <div class="address-controls"><input id="address-search" type="search" placeholder="Filter by address…"></div>
  <div class="table-scroll">
    <table id="address-table">
      <thead><tr><th>Rank</th><th>Full address</th><th>{escape(value_label)}</th><th>Copy</th><th>Explorer</th></tr></thead>
      <tbody>{''.join(table_rows)}</tbody>
    </table>
  </div>
</details>
<div id="copy-toast" class="no-render">Address copied</div>
{_editable_header_script(output_path.name)}
{address_script}
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")
    print(f"[html] {output_path}")

def human_number(value: float | int) -> str:
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
    formatted = f"{scaled:,.2f}".rstrip("0").rstrip(".")
    return f"{formatted}{suffix}"


def nice_tick_values(max_value: float, target_ticks: int = 6) -> list[float]:
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
    return [index * step for index in range(int(round(upper / step)) + 1)]


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


def human_symmetric_axis(values: Iterable[float], title: str) -> dict:
    numeric = pd.to_numeric(pd.Series(list(values)), errors="coerce").dropna()
    if numeric.empty:
        return {"title": title, "rangemode": "tozero"}
    max_abs = float(numeric.abs().max())
    ticks_positive = nice_tick_values(max_abs)
    ticks = [-v for v in reversed(ticks_positive[1:])] + ticks_positive
    return {
        "title": title,
        "tickmode": "array",
        "tickvals": ticks,
        "ticktext": [human_number(value) for value in ticks],
        "range": [-max_abs * 1.08, max_abs * 1.08] if max_abs else None,
        "zeroline": True,
        "zerolinewidth": 1,
    }


def _log_major_tick_axis(values: Iterable[float], title: str) -> dict:
    """Clean base-10 log axis with only evenly spaced decade ticks."""
    numeric = pd.to_numeric(pd.Series(list(values)), errors="coerce")
    numeric = numeric[np.isfinite(numeric) & (numeric > 0)]
    if numeric.empty:
        return {"title": title, "type": "log"}
    min_exp = int(math.floor(math.log10(float(numeric.min()))))
    max_exp = int(math.ceil(math.log10(float(numeric.max()))))
    tickvals = [10.0 ** exp for exp in range(min_exp, max_exp + 1)]
    return {
        "title": title,
        "type": "log",
        "tickmode": "array",
        "tickvals": tickvals,
        "ticktext": [human_number(value) for value in tickvals],
        "showgrid": True,
    }


def color_sequence(cfg: dict) -> list[str]:
    return list(cfg.get("color_sequence") or px.colors.qualitative.Safe)


def add_regulatory_events(fig: go.Figure, cfg: dict, *, annotations: bool = True) -> None:
    """Add event markers without Plotly's datetime add_vline annotation bug."""
    events = cfg.get("regulatory_events") or []
    for event in events:
        date = event.get("date")
        if not date:
            continue
        x = str(date)
        try:
            fig.add_shape(
                type="line", x0=x, x1=x, y0=0, y1=1,
                xref="x", yref="paper",
                line=dict(
                    width=float(event.get("line_width", 1.2)),
                    dash=event.get("line_dash", "dash"),
                    color=event.get("line_color", "#6B7280"),
                ),
            )
            if annotations and event.get("label"):
                fig.add_annotation(
                    x=x, y=1, xref="x", yref="paper",
                    text=event["label"], showarrow=False,
                    xanchor="left", yanchor="bottom",
                    font=dict(size=int(event.get("annotation_font_size", 10))),
                )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Transfer-size bucket helpers
# ---------------------------------------------------------------------------


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
    present = set(df["size_bucket"].astype(str))
    order = [bucket for bucket in BASE_BUCKET_ORDER if bucket in present]
    if any(bucket in present for bucket in EXPANDED_FINAL_BUCKETS):
        order.extend(bucket for bucket in EXPANDED_FINAL_BUCKETS if bucket in present)
    elif OLD_FINAL_BUCKET in present:
        order.append(OLD_FINAL_BUCKET)
    known = set(order)
    order.extend(sorted(present - known))
    return order


def add_bucket_labels(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    result = df.copy()
    raw_order = bucket_order(result)
    result["bucket_label"] = result["size_bucket"].astype(str).map(
        lambda value: BUCKET_DISPLAY_LABELS.get(value, value)
    )
    return result, [BUCKET_DISPLAY_LABELS.get(value, value) for value in raw_order]


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = color.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Expected a six-digit hex color, got: {color}")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    channels = [max(0, min(255, round(channel))) for channel in rgb]
    return "#" + "".join(f"{channel:02X}" for channel in channels)


def darken_hex(color: str, fraction: float = 0.18) -> str:
    """Darken a six-digit hex color by blending it toward black."""
    base = _hex_to_rgb(color)
    fraction = max(0.0, min(1.0, float(fraction)))
    return _rgb_to_hex(tuple(channel * (1.0 - fraction) for channel in base))


def _interpolate_rgb(start, end, fraction: float):
    return tuple(
        start_channel + (end_channel - start_channel) * fraction
        for start_channel, end_channel in zip(start, end)
    )


def monochrome_palette(base_color: str, count: int) -> list[str]:
    if count <= 0:
        return []
    if count == 1:
        return [base_color]
    base = _hex_to_rgb(base_color)
    white = (255.0, 255.0, 255.0)
    black = (0.0, 0.0, 0.0)
    light = _interpolate_rgb(base, white, 0.72)
    dark = _interpolate_rgb(base, black, 0.32)
    palette = []
    for index in range(count):
        position = index / (count - 1)
        if position <= 0.65:
            rgb = _interpolate_rgb(light, base, position / 0.65)
        else:
            rgb = _interpolate_rgb(base, dark, (position - 0.65) / 0.35)
        palette.append(_rgb_to_hex(rgb))
    return palette


def bucket_color_sequence(cfg: dict, count: int) -> list[str]:
    configured = cfg.get("bucket_color_sequence")
    if configured:
        colors = list(configured)
        if len(colors) < count:
            raise ValueError("bucket_color_sequence contains fewer colors than size buckets")
        return colors[:count]
    return monochrome_palette(
        cfg.get("bucket_base_color", cfg.get("primary_color", "#009393")),
        count,
    )


# ---------------------------------------------------------------------------
# Time-window helpers
# ---------------------------------------------------------------------------


def add_time_window(df: pd.DataFrame, months: int) -> pd.DataFrame:
    if months <= 0 or 12 % months != 0:
        raise ValueError("top_window_months must be one of 1, 2, 3, 4, 6, or 12")
    result = df.copy()
    result["month"] = pd.to_datetime(result["month"])
    start_month = ((result["month"].dt.month - 1) // months) * months + 1
    result["window_start"] = pd.to_datetime(
        {"year": result["month"].dt.year, "month": start_month, "day": 1}
    )
    periods_per_year = 12 // months
    period_number = ((start_month - 1) // months) + 1
    if months == 6:
        # Compact half-year labels such as H1 '19, H2 '19, ...
        year_short = result["window_start"].dt.strftime("%y")
        result["window_label"] = "H" + period_number.astype(str) + " '" + year_short
    elif months == 3:
        result["window_label"] = result["window_start"].dt.year.astype(str) + " Q" + period_number.astype(str)
    elif months == 12:
        result["window_label"] = result["window_start"].dt.year.astype(str)
    else:
        result["window_label"] = result["window_start"].dt.strftime("%Y-%m")
    result["window_order"] = result["window_start"].dt.year * periods_per_year + period_number
    return result


# ---------------------------------------------------------------------------
# Per-dataset plots
# ---------------------------------------------------------------------------


def plot_monthly_activity(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    df = read_csv(input_folder, "monthly_activity", cfg)
    if df is None or df.empty:
        return
    volume_col = cfg.get("volume_column", "token_volume")
    required = {volume_col, "transfer_count"}
    missing = required.difference(df.columns)
    if missing:
        print(f"[skip] monthly_activity missing columns: {sorted(missing)}")
        return

    primary = cfg.get("primary_color", "#009393")
    secondary = cfg.get("secondary_color", "#E67E22")
    transaction_color = cfg.get("transaction_color", "#4C78A8")

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df["month"], y=df[volume_col], name=f"{cfg['token_symbol']} volume",
        marker_color=primary, customdata=df[volume_col].map(human_number),
        hovertemplate="%{x|%b %Y}<br>Volume: %{customdata}<extra></extra>",
    ))

    has_transactions = "transaction_count" in df.columns
    counts_identical = has_transactions and df["transaction_count"].fillna(-1).equals(df["transfer_count"].fillna(-1))
    count_series = [df["transfer_count"]]
    if not counts_identical:
        fig.add_trace(go.Scatter(
            x=df["month"], y=df["transfer_count"], name="Transfer-event count",
            mode="lines", yaxis="y2", line=dict(color=secondary, width=2, dash=_chain_line_dash(str(cfg.get("chain", "")))),
            customdata=df["transfer_count"].map(human_number),
            hovertemplate="%{x|%b %Y}<br>Transfer events: %{customdata}<extra></extra>",
        ))
    if has_transactions:
        count_series.append(df["transaction_count"])
        fig.add_trace(go.Scatter(
            x=df["month"], y=df["transaction_count"], name="Transaction count",
            mode="lines", yaxis="y2",
            line=dict(color=transaction_color, width=2, dash=_chain_line_dash(str(cfg.get("chain", "")))),
            customdata=df["transaction_count"].map(human_number),
            hovertemplate="%{x|%b %Y}<br>Transactions: %{customdata}<extra></extra>",
        ))

    all_counts = pd.concat(count_series, ignore_index=True)
    fig.update_layout(
        title=f"{cfg['title_prefix']}: Monthly transfer activity",
        xaxis=dict(title="Month"),
        yaxis=human_axis(df[volume_col], f"{cfg['token_symbol']} volume"),
        yaxis2={**human_axis(all_counts, "Count"), "overlaying": "y", "side": "right", "showgrid": False},
        hovermode="x unified", bargap=0.08,
    )
    add_regulatory_events(fig, cfg)
    save_fig(fig, output_folder / "monthly_activity.html", cfg)


def _apply_issuance_adjustments(df: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, list[str]]:
    result = df.copy()
    notes: list[str] = []
    symbol = str(cfg.get("token_symbol", "")).upper()
    adjustments = (cfg.get("issuance_adjustments") or {}).get(symbol, [])
    for adjustment in adjustments:
        month = pd.to_datetime(adjustment["month"])
        mask = result["month"] == month
        if not mask.any():
            print(f"[warn] {symbol}: issuance adjustment month {month.date()} not present")
            continue
        minted_remove = float(adjustment.get("minted_remove", 0))
        burned_remove = float(adjustment.get("burned_remove", 0))
        current_minted = pd.to_numeric(result.loc[mask, "minted_volume"], errors="coerce").fillna(0)
        current_burned = pd.to_numeric(result.loc[mask, "burned_volume"], errors="coerce").fillna(0)
        if (current_minted < minted_remove).any() or (current_burned < burned_remove).any():
            raise ValueError(
                f"{symbol}: configured issuance adjustment exceeds observed monthly value in {month:%Y-%m}"
            )
        result.loc[mask, "minted_volume"] = current_minted - minted_remove
        result.loc[mask, "burned_volume"] = current_burned - burned_remove
        notes.append(str(adjustment.get("note", "Known anomalous issuance event excluded.")))

    result["minted_volume"] = pd.to_numeric(result["minted_volume"], errors="coerce").fillna(0.0)
    result["burned_volume"] = pd.to_numeric(result["burned_volume"], errors="coerce").fillna(0.0)
    # Recalculate from adjusted flows instead of trusting the precomputed columns.
    result["net_issuance_adjusted"] = result["minted_volume"] - result["burned_volume"]
    result["cumulative_net_issuance_adjusted"] = result["net_issuance_adjusted"].cumsum()
    return result, notes


def plot_monthly_mint_burn(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    df = read_csv(input_folder, "monthly_mint_burn", cfg)
    if df is None or df.empty:
        return
    required = {"minted_volume", "burned_volume"}
    missing = required.difference(df.columns)
    if missing:
        print(f"[skip] monthly_mint_burn missing columns: {sorted(missing)}")
        return

    df, notes = _apply_issuance_adjustments(df, cfg)
    df["burned_volume_negative"] = -df["burned_volume"]
    cumulative = df["cumulative_net_issuance_adjusted"]

    primary = cfg.get("primary_color", "#009393")
    secondary = cfg.get("secondary_color", "#E67E22")
    line_color = cfg.get("transaction_color", "#4C78A8")

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df["month"], y=df["minted_volume"], name="Monthly minting",
        marker_color=primary, customdata=df["minted_volume"].map(human_number),
        hovertemplate="%{x|%b %Y}<br>Minted: %{customdata}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=df["month"], y=df["burned_volume_negative"], name="Monthly burning",
        marker_color=secondary, customdata=df["burned_volume"].map(human_number),
        hovertemplate="%{x|%b %Y}<br>Burned: %{customdata}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=df["month"], y=cumulative, name="Cumulative net issuance",
        mode="lines", yaxis="y2", line=dict(color=line_color, width=2.5, dash=_chain_line_dash(str(cfg.get("chain", "")))),
        customdata=cumulative.map(human_number),
        hovertemplate="%{x|%b %Y}<br>Cumulative net issuance: %{customdata}<extra></extra>",
    ))

    bar_values = pd.concat([df["minted_volume"], df["burned_volume_negative"]], ignore_index=True)
    cumulative_axis = human_symmetric_axis(cumulative, f"Cumulative {cfg['token_symbol']}") if (cumulative < 0).any() else human_axis(cumulative, f"Cumulative {cfg['token_symbol']}")
    left_axis = human_symmetric_axis(bar_values, f"Monthly {cfg['token_symbol']} mint / burn volume")
    right_axis = {**cumulative_axis, "overlaying": "y", "side": "right", "showgrid": False}
    right_title = right_axis.get("title", f"Cumulative {cfg['token_symbol']}")
    right_axis["title"] = dict(text=right_title, font=dict(color=line_color))
    right_axis["tickfont"] = dict(color=line_color)
    fig.update_layout(
        title=f"{cfg['title_prefix']}: Monthly minting and burning with cumulative net issuance" + ("*" if notes else ""),
        xaxis=dict(title="Month"),
        yaxis=left_axis,
        yaxis2=right_axis,
        barmode="relative", hovermode="x unified", bargap=0.08,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, itemsizing="constant"),
    )
    add_regulatory_events(fig, cfg)
    if notes:
        fig.add_annotation(
            xref="paper", yref="paper", x=0, y=-0.19, showarrow=False,
            text="* " + " ".join(notes), align="left",
            font=dict(size=10, color="#5D6B7C"),
        )
        fig.update_layout(margin=dict(l=75, r=75, t=90, b=120))
    combined_path = output_folder / "monthly_mint_burn.html"
    save_fig(fig, combined_path, cfg)

    # Remove the obsolete separate cumulative figure from older plotting runs so
    # the website/appendix directory cannot accidentally retain both versions.
    obsolete_html = output_folder / "cumulative_net_issuance.html"
    if obsolete_html.exists():
        obsolete_html.unlink()
        print(f"[cleanup] {obsolete_html}")
    obsolete_pdf = _static_path_for(obsolete_html, cfg)
    if obsolete_pdf is not None and obsolete_pdf.exists():
        obsolete_pdf.unlink()
        print(f"[cleanup] {obsolete_pdf}")


def plot_monthly_users(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    df = read_csv(input_folder, "monthly_activity", cfg)
    if df is None or df.empty:
        return
    required = {"unique_senders", "unique_receivers", "active_addresses"}
    if not required.issubset(df.columns):
        return
    fig = go.Figure()
    for column, label in (
        ("unique_senders", "Unique senders"),
        ("unique_receivers", "Unique receivers"),
        ("active_addresses", "Active addresses"),
    ):
        fig.add_trace(go.Scatter(
            x=df["month"], y=df[column], name=label, mode="lines",
            line=dict(dash=_chain_line_dash(str(cfg.get("chain", "")))),
            customdata=df[column].map(human_number),
            hovertemplate=f"%{{x|%b %Y}}<br>{label}: %{{customdata}}<extra></extra>",
        ))
    all_values = pd.concat([df["unique_senders"], df["unique_receivers"], df["active_addresses"]])
    fig.update_layout(
        title=f"{cfg['title_prefix']}: Monthly active addresses",
        xaxis=dict(title="Month"), yaxis=human_axis(all_values, "Addresses"),
        hovermode="x unified",
    )
    add_regulatory_events(fig, cfg)
    save_fig(fig, output_folder / "monthly_users.html", cfg)


def plot_adoption(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    df = read_csv(input_folder, "monthly_adoption", cfg)
    if df is None or df.empty or "newly_adopted_addresses" not in df.columns:
        return
    primary = cfg.get("primary_color", "#009393")
    secondary = cfg.get("secondary_color", "#E67E22")
    df = df.copy()
    df["cumulative_adoption"] = df["newly_adopted_addresses"].cumsum()
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df["month"], y=df["newly_adopted_addresses"], name="First-time participants",
        marker_color=primary, customdata=df["newly_adopted_addresses"].map(human_number),
        hovertemplate="%{x|%b %Y}<br>First-time participants: %{customdata}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=df["month"], y=df["cumulative_adoption"], name="Cumulative first-time participants",
        mode="lines", yaxis="y2", line=dict(color=secondary, width=2, dash=_chain_line_dash(str(cfg.get("chain", "")))),
        customdata=df["cumulative_adoption"].map(human_number),
        hovertemplate="%{x|%b %Y}<br>Cumulative: %{customdata}<extra></extra>",
    ))
    fig.update_layout(
        title=f"{cfg['title_prefix']}: First-time participation",
        xaxis=dict(title="Month"),
        yaxis=human_axis(df["newly_adopted_addresses"], "First-time participants"),
        yaxis2={**human_axis(df["cumulative_adoption"], "Cumulative participants"), "overlaying": "y", "side": "right", "showgrid": False},
        hovermode="x unified", bargap=0.08,
    )
    add_regulatory_events(fig, cfg)
    save_fig(fig, output_folder / "monthly_adoption.html", cfg)


def plot_activity_intensity(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    """Combine three derived monthly activity-intensity measures in one figure.

    For USD-pegged stablecoins, transfer-count-dependent metrics use the
    supervisor-approved >=0.01 transfer population reconstructed from the
    existing transfer-size buckets. Transfer volume per active address remains
    based on total transfer volume.
    """
    df = read_csv(input_folder, "monthly_activity", cfg)
    if df is None or df.empty:
        return
    required = {"token_volume", "transfer_count", "active_addresses"}
    if not required.issubset(df.columns):
        return
    df = df.copy()
    volume = pd.to_numeric(df["token_volume"], errors="coerce")
    raw_transfers = pd.to_numeric(df["transfer_count"], errors="coerce")
    active = pd.to_numeric(df["active_addresses"], errors="coerce")

    transfers = raw_transfers
    average_size_volume = volume
    if _genius_dust_filter_applies(cfg, cfg):
        qualifying = _genius_qualifying_transfer_totals(cfg, cfg)
        if qualifying is not None:
            filtered = df[["month"]].copy().merge(qualifying, on="month", how="left")
            transfers = pd.to_numeric(
                filtered["genius_qualifying_transfer_count"], errors="coerce"
            ).fillna(0.0)
            average_size_volume = pd.to_numeric(
                filtered["genius_qualifying_transfer_volume"], errors="coerce"
            ).fillna(0.0)

    df["average_transfer_size"] = average_size_volume.div(transfers.where(transfers > 0))
    df["volume_per_active_address"] = volume.div(active.where(active > 0))
    df["transfers_per_active_address"] = transfers.div(active.where(active > 0))
    for log_col in ["average_transfer_size", "volume_per_active_address"]:
        df.loc[pd.to_numeric(df[log_col], errors="coerce") <= 0, log_col] = np.nan

    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.075,
        subplot_titles=("Average transfer size", "Transfer volume per active address", "Transfer events per active address"),
    )
    primary = cfg.get("primary_color", "#009393")
    metrics = [
        ("average_transfer_size", f"{cfg['token_symbol']} per transfer"),
        ("volume_per_active_address", f"{cfg['token_symbol']} per active address"),
        ("transfers_per_active_address", "Transfer events per active address"),
    ]
    for row, (column, label) in enumerate(metrics, start=1):
        fig.add_trace(go.Scatter(
            x=df["month"], y=df[column], mode="lines", name=label,
            line=dict(color=primary, width=2, dash=_chain_line_dash(str(cfg.get("chain", "")))), showlegend=False,
            hovertemplate=f"%{{x|%b %Y}}<br>{label}: %{{y:,.2f}}<extra></extra>",
        ), row=row, col=1)
        fig.update_yaxes(title_text=label, row=row, col=1)
    fig.update_yaxes(**_log_major_tick_axis(df["average_transfer_size"], metrics[0][1]), row=1, col=1)
    fig.update_yaxes(**_log_major_tick_axis(df["volume_per_active_address"], metrics[1][1]), row=2, col=1)
    # Show the month axis both at the top and at the bottom for easier reading.
    fig.update_xaxes(showticklabels=True, side="top", row=1, col=1)
    fig.update_xaxes(showticklabels=False, row=2, col=1)
    fig.update_xaxes(title_text="Month", showticklabels=True, row=3, col=1)
    fig.update_layout(
        title=f"{cfg['title_prefix']}: Monthly activity intensity",
        height=930, hovermode="x unified",
        margin=dict(l=95, r=45, t=165, b=70),
    )
    subplot_titles = list(fig.layout.annotations or [])
    if subplot_titles:
        subplot_titles[0].update(y=1.115, yanchor="bottom")
    # Regulatory-event lines across the full stacked figure plus visible labels above the top subplot.
    for event in cfg.get("regulatory_events") or []:
        if event.get("date"):
            event_x = pd.Timestamp(event["date"]).to_pydatetime()
            fig.add_vline(
                x=event_x, row="all", col=1,
                line_width=1.1, line_dash=event.get("line_dash", "dash"),
                line_color=event.get("line_color", "#6B7280"),
            )
            if event.get("label"):
                label_text = str(event["label"])
                is_genius = "GENIUS" in label_text.upper()
                fig.add_annotation(
                    x=event_x, y=1.04, xref="x", yref="paper",
                    text=label_text, showarrow=False,
                    xanchor="right" if is_genius else "left", yanchor="bottom",
                    xshift=-6 if is_genius else 6,
                    font=dict(size=int(event.get("annotation_font_size", 10)), color=event.get("line_color", "#6B7280")),
                    bgcolor="rgba(255,255,255,0.88)",
                )
    save_fig(fig, output_folder / "monthly_activity_intensity.html", cfg)



def plot_transfer_size_histogram(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    df = read_csv(input_folder, "transfer_size_histogram_all_time", cfg)
    if df is None or df.empty:
        return
    df = df.copy()
    count_total = df["transfer_count"].sum()
    volume_total = df["bucket_volume"].sum()
    df["transfer_share"] = df["transfer_count"] / count_total if count_total else 0.0
    df["volume_share"] = df["bucket_volume"] / volume_total if volume_total else 0.0
    df, order = add_bucket_labels(df)
    fig = go.Figure()
    primary = cfg.get("primary_color", "#009393")
    secondary = cfg.get("secondary_color", "#E67E22")
    fig.add_trace(go.Bar(
        x=df["bucket_label"], y=df["transfer_share"], name="Share of transfer events", marker_color=primary,
        customdata=(df["transfer_share"] * 100).round(2),
        hovertemplate="Bucket: %{x}<br>Share of transfer events: %{customdata:.2f}%<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=df["bucket_label"], y=df["volume_share"], name="Share of volume", marker_color=secondary,
        customdata=(df["volume_share"] * 100).round(2),
        hovertemplate="Bucket: %{x}<br>Share of volume: %{customdata:.2f}%<extra></extra>",
    ))
    fig.update_layout(
        title=f"{cfg['title_prefix']}: Relative transfer-size distribution",
        xaxis=dict(title=f"Transfer size ({cfg['token_symbol']})", categoryorder="array", categoryarray=order),
        yaxis=dict(title="Share", tickformat=".0%", range=[0, 1]),
        barmode="group", hovermode="x unified",
    )
    save_fig(fig, output_folder / "transfer_size_histogram_relative.html", cfg)


def plot_genius_transfer_size_pre_post(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    """Compare transfer-size count and volume shares in the six-month GENIUS windows.

    Uses a plain categorical x-axis (one category per transfer-size bucket) to avoid
    Plotly multicategory separator lines.  Each bucket has four adjacent bars:
    pre/post transfer-event share and pre/post volume share.
    """
    df = read_csv(input_folder, "monthly_transfer_size_buckets", cfg)
    if df is None or df.empty:
        return
    required = {"month", "size_bucket", "transfer_count", "bucket_volume"}
    if not required.issubset(df.columns):
        print(f"[skip] GENIUS transfer-size comparison missing columns: {sorted(required.difference(df.columns))}")
        return

    pre_start = pd.to_datetime(cfg.get("genius_pre_start", "2025-01-01"))
    pre_end = pd.to_datetime(cfg.get("genius_pre_end", "2025-07-01"))
    post_start = pd.to_datetime(cfg.get("genius_post_start", "2025-08-01"))
    post_end = pd.to_datetime(cfg.get("genius_post_end", "2026-02-01"))
    frame = df.copy()
    frame["month"] = pd.to_datetime(frame["month"], errors="raise")
    frame["transfer_count"] = pd.to_numeric(frame["transfer_count"], errors="coerce").fillna(0.0)
    frame["bucket_volume"] = pd.to_numeric(frame["bucket_volume"], errors="coerce").fillna(0.0)

    def aggregate_window(start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame, int]:
        part = frame[(frame["month"] >= start) & (frame["month"] < end)].copy()
        month_count = int(part["month"].dt.to_period("M").nunique())
        if part.empty:
            return pd.DataFrame(), month_count
        grouped = part.groupby("size_bucket", as_index=False)[["transfer_count", "bucket_volume"]].sum()
        count_total = float(grouped["transfer_count"].sum())
        volume_total = float(grouped["bucket_volume"].sum())
        grouped["transfer_share"] = grouped["transfer_count"] / count_total if count_total else 0.0
        grouped["volume_share"] = grouped["bucket_volume"] / volume_total if volume_total else 0.0
        return grouped, month_count

    pre, pre_months = aggregate_window(pre_start, pre_end)
    post, post_months = aggregate_window(post_start, post_end)
    if pre.empty or post.empty:
        print(f"[skip] {cfg.get('file_prefix')}: incomplete GENIUS transfer-size windows (pre={pre_months}, post={post_months})")
        return

    all_buckets = pd.concat([pre[["size_bucket"]], post[["size_bucket"]]], ignore_index=True).drop_duplicates()
    raw_order = bucket_order(all_buckets)
    labels = [BUCKET_DISPLAY_LABELS.get(bucket, bucket) for bucket in raw_order]

    def aligned(part: pd.DataFrame, metric: str) -> list[float]:
        lookup = dict(zip(part["size_bucket"].astype(str), part[metric]))
        return [float(lookup.get(bucket, 0.0)) for bucket in raw_order]

    primary = cfg.get("primary_color", "#009393")
    secondary = cfg.get("secondary_color", "#E67E22")
    pre_primary = darken_hex(primary, float(cfg.get("genius_pre_color_darken", 0.20)))
    pre_secondary = darken_hex(secondary, float(cfg.get("genius_pre_color_darken", 0.20)))

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=labels, y=aligned(pre, "transfer_share"),
        name="Pre-GENIUS — transfer events", marker_color=pre_primary,
        offsetgroup="transfer_pre",
        hovertemplate="Bucket: %{x}<br>Pre-GENIUS transfer-event share: %{y:.2%}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=labels, y=aligned(post, "transfer_share"),
        name="Post-GENIUS — transfer events", marker_color=primary,
        offsetgroup="transfer_post",
        hovertemplate="Bucket: %{x}<br>Post-GENIUS transfer-event share: %{y:.2%}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=labels, y=aligned(pre, "volume_share"),
        name="Pre-GENIUS — volume", marker_color=pre_secondary,
        offsetgroup="volume_pre",
        hovertemplate="Bucket: %{x}<br>Pre-GENIUS volume share: %{y:.2%}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=labels, y=aligned(post, "volume_share"),
        name="Post-GENIUS — volume", marker_color=secondary,
        offsetgroup="volume_post",
        hovertemplate="Bucket: %{x}<br>Post-GENIUS volume share: %{y:.2%}<extra></extra>",
    ))
    fig.update_layout(
        title=f"{cfg['title_prefix']}: Transfer-size distribution before and after the GENIUS Act",
        xaxis=dict(title=f"Transfer size ({cfg['token_symbol']})", categoryorder="array", categoryarray=labels, tickangle=-25),
        yaxis=dict(title="Share within six-month window", tickformat=".0%", range=[0, 1]),
        barmode="group", bargap=0.16, bargroupgap=0.05, hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        height=760, margin=dict(l=80, r=50, t=125, b=105),
    )
    save_fig(fig, output_folder / "transfer_size_histogram_genius_pre_post.html", cfg)


def _plot_monthly_bucket_stacked(df, output_folder, cfg, value_column, value_title, output_stem):
    df, order = add_bucket_labels(df)
    colors = bucket_color_sequence(cfg, len(order))
    absolute = px.bar(
        df, x="month", y=value_column, color="bucket_label",
        category_orders={"bucket_label": order}, color_discrete_sequence=colors,
        title=f"{cfg['title_prefix']}: Monthly transfer-size buckets by {value_title.lower()}",
    )
    monthly_totals = df.groupby("month")[value_column].sum()
    absolute.update_layout(
        barmode="stack", xaxis=dict(title="Month"), yaxis=human_axis(monthly_totals, value_title),
        hovermode="x unified", bargap=0.05, legend_traceorder="normal",
    )
    absolute.update_traces(hovertemplate="%{x|%b %Y}<br>%{fullData.name}: %{y:,.2f}<extra></extra>")
    save_fig(absolute, output_folder / f"{output_stem}_stacked.html", cfg)

    relative = df.copy()
    totals = relative.groupby("month")[value_column].transform("sum")
    relative["relative_share"] = relative[value_column].div(totals.where(totals != 0)).fillna(0.0)
    relative_fig = px.bar(
        relative, x="month", y="relative_share", color="bucket_label",
        category_orders={"bucket_label": order}, color_discrete_sequence=colors,
        title=f"{cfg['title_prefix']}: Relative monthly transfer-size buckets by {value_title.lower()}",
    )
    relative_fig.update_layout(
        barmode="stack", xaxis=dict(title="Month"),
        yaxis=dict(title="Monthly share", tickformat=".0%", range=[0, 1]),
        hovermode="x unified", bargap=0.05, legend_traceorder="normal",
    )
    relative_fig.update_traces(hovertemplate="%{x|%b %Y}<br>%{fullData.name}: %{y:.2%}<extra></extra>")
    save_fig(relative_fig, output_folder / f"{output_stem}_relative_stacked.html", cfg)


def plot_monthly_transfer_size_buckets_count(input_folder, output_folder, cfg):
    df = read_csv(input_folder, "monthly_transfer_size_buckets", cfg)
    if df is not None and not df.empty:
        _plot_monthly_bucket_stacked(df, output_folder, cfg, "transfer_count", "Transfer-event count", "monthly_transfer_size_buckets_count")


def plot_monthly_transfer_size_buckets_volume(input_folder, output_folder, cfg):
    df = read_csv(input_folder, "monthly_transfer_size_buckets", cfg)
    if df is not None and not df.empty:
        _plot_monthly_bucket_stacked(df, output_folder, cfg, "bucket_volume", f"{cfg['token_symbol']} volume", "monthly_transfer_size_buckets_volume")


# ---------------------------------------------------------------------------
# Top-address plots
# ---------------------------------------------------------------------------


def plot_overall_top_addresses(
    df, output_folder, cfg, *, address_column, value_column, title, axis_title, output_name
):
    top_n = int(cfg.get("top_n", 50))
    ranked = (
        df.groupby(address_column, as_index=False)[value_column].sum()
        .nlargest(top_n, value_column).sort_values(value_column, ascending=False).reset_index(drop=True)
    )
    if ranked.empty:
        return
    ranked[address_column] = ranked[address_column].map(normalize_address)
    ranked["rank"] = ranked.index + 1
    ranked["address_label"] = ranked.apply(lambda row: f"#{int(row['rank']):02d}  {short_address(row[address_column])}", axis=1)
    ranked["formatted_value"] = ranked[value_column].map(human_number)
    plotted = ranked.sort_values(value_column, ascending=True)
    fig = go.Figure(go.Bar(
        x=plotted[value_column], y=plotted["address_label"], orientation="h",
        marker_color=cfg.get("primary_color", "#009393"),
        customdata=list(zip(plotted[address_column], plotted["formatted_value"], plotted["rank"])),
        hovertemplate="Rank: %{customdata[2]}<br>Address: %{customdata[0]}<br>" + axis_title + ": %{customdata[1]}<br>Click to copy address<extra></extra>",
    ))
    fig.update_layout(
        title=title, xaxis=human_axis(plotted[value_column], axis_title),
        yaxis=dict(title="Rank and address"), height=max(680, 29 * len(plotted) + 180),
        margin=dict(l=165, r=40, t=90, b=70),
    )
    save_address_fig(fig, output_folder / output_name, cfg, ranked, address_column=address_column, value_column=value_column, value_label=axis_title)


def _square_side(value: float, max_value: float, min_size: float, max_size: float) -> float:
    """Square side in pixels; square area is exactly proportional to value."""
    if value <= 0 or max_value <= 0:
        return min_size
    return max(min_size, max_size * math.sqrt(value / max_value))


def plot_six_month_address_views(
    df, output_folder, cfg, *, address_column, value_column, title_subject, axis_title, output_name
):
    top_n = int(cfg.get("top_n", 50))
    stack_top_n = min(int(cfg.get("address_stack_top_n", 5)), top_n)
    window_months = int(cfg.get("top_window_months", 6))
    windowed = add_time_window(df, window_months)
    windowed[address_column] = windowed[address_column].map(normalize_address)
    overall_totals = (
        windowed.groupby(address_column, as_index=False)[value_column].sum()
        .nlargest(top_n, value_column).sort_values(value_column, ascending=False).reset_index(drop=True)
    )
    if overall_totals.empty:
        return
    overall_totals["rank"] = overall_totals.index + 1
    top_addresses = overall_totals[address_column].tolist()
    rank_lookup = dict(zip(overall_totals[address_column], overall_totals["rank"]))
    windowed = windowed[windowed[address_column].isin(top_addresses)]
    grouped = windowed.groupby(["window_start", "window_order", "window_label", address_column], as_index=False)[value_column].sum()
    if grouped.empty:
        return
    ordered_windows = grouped[["window_order", "window_label"]].drop_duplicates().sort_values("window_order")["window_label"].tolist()

    # Compact stacked view: top few addresses plus the rest of the displayed top-N.
    stack_addresses = set(top_addresses[:stack_top_n])
    grouped["series_address"] = grouped[address_column].where(grouped[address_column].isin(stack_addresses), "Other top addresses")
    grouped["series_label"] = grouped["series_address"].map(
        lambda address: "Other top addresses" if address == "Other top addresses" else f"#{rank_lookup[address]:02d} {short_address(address)}"
    )
    stack_grouped = grouped.groupby(["window_order", "window_label", "series_address", "series_label"], as_index=False)[value_column].sum()
    stack_grouped["formatted_value"] = stack_grouped[value_column].map(human_number)
    stack_grouped["full_address"] = stack_grouped["series_address"].map(lambda value: value if value != "Other top addresses" else "")
    series_order = [f"#{rank_lookup[address]:02d} {short_address(address)}" for address in top_addresses[:stack_top_n]] + ["Other top addresses"]
    palette = color_sequence(cfg)
    color_map = {label: palette[index % len(palette)] for index, label in enumerate(series_order[:-1])}
    color_map["Other top addresses"] = cfg.get("address_other_color", "#C9D1D9")
    stack_fig = px.bar(
        stack_grouped, x="window_label", y=value_column, color="series_label",
        custom_data=["full_address", "formatted_value"],
        category_orders={"window_label": ordered_windows, "series_label": series_order},
        color_discrete_map=color_map,
        title=f"{cfg['title_prefix']}: {stack_top_n} leading overall {title_subject} plus the remainder across {window_months}-month windows",
    )
    stack_fig.update_traces(marker_line_color="white", marker_line_width=0.5, hovertemplate="Window: %{x}<br>Series: %{fullData.name}<br>" + axis_title + ": %{customdata[1]}<extra></extra>")
    window_totals = stack_grouped.groupby("window_label")[value_column].sum()
    stack_fig.update_layout(
        barmode="stack", xaxis=dict(title=None),
        yaxis=human_axis(window_totals, axis_title), hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=-0.30, xanchor="left", x=0),
        margin=dict(l=75, r=40, t=95, b=145),
    )
    if cfg.get("plot_address_window_stacked", False):
        save_address_fig(stack_fig, output_folder / output_name, cfg, overall_totals, address_column=address_column, value_column=value_column, value_label=f"Overall {axis_title}")

    # Detailed square matrix. Marker side length is sqrt(value), hence square area
    # is exactly proportional to the underlying metric. The legend uses the exact
    # same transformation as the data markers.
    pivot = grouped.pivot_table(index=address_column, columns="window_label", values=value_column, aggfunc="sum", fill_value=0).reindex(index=top_addresses, columns=ordered_windows, fill_value=0)
    window_sums = pivot.sum(axis=0).replace(0, pd.NA)
    shares = pivot.div(window_sums, axis=1).fillna(0.0)
    overall_lookup = dict(zip(overall_totals[address_column], overall_totals[value_column]))
    rows = []
    for address in top_addresses:
        rank = int(rank_lookup[address])
        row_label = f"#{rank:02d}  {short_address(address)}"
        overall_value = float(overall_lookup[address])
        for window in ordered_windows:
            value = float(pivot.loc[address, window])
            if value <= 0:
                continue
            rows.append({
                "window_label": window, "address_label": row_label, "address": address,
                "rank": rank, "value": value, "formatted_value": human_number(value),
                "window_share": float(shares.loc[address, window]),
                "formatted_overall_value": human_number(overall_value),
            })
    square_df = pd.DataFrame(rows)
    if square_df.empty:
        return
    max_value = float(square_df["value"].max())
    row_height = float(cfg.get("address_bubble_row_height", 42))
    column_width = float(cfg.get("address_bubble_column_width", 56))
    max_marker_size = min(float(cfg.get("address_bubble_max_size", 34)), row_height * 0.80)
    min_marker_size = float(cfg.get("address_bubble_min_size", 3.0))
    opacity = float(cfg.get("address_bubble_opacity", 0.82))
    square_df["marker_size"] = square_df["value"].map(lambda value: _square_side(float(value), max_value, min_marker_size, max_marker_size))
    primary = cfg.get("primary_color", "#009393")
    border_color = cfg.get("address_bubble_border_color", "#FFFFFF")

    square_fig = go.Figure()
    square_fig.add_trace(go.Scatter(
        x=square_df["window_label"], y=square_df["address_label"], mode="markers",
        name=axis_title,
        customdata=list(zip(square_df["address"], square_df["rank"], square_df["formatted_value"], square_df["window_share"], square_df["formatted_overall_value"])),
        marker=dict(size=square_df["marker_size"], symbol="square", color=primary, opacity=opacity, line=dict(color=border_color, width=1.0)),
        hovertemplate="Window: %{x}<br>Overall rank: %{customdata[1]}<br>Address: %{customdata[0]}<br>" + axis_title + ": %{customdata[2]}<br>Share among displayed top addresses in this window: %{customdata[3]:.2%}<br>Overall " + axis_title.lower() + ": %{customdata[4]}<br>Click to copy address<extra></extra>",
    ))
    # Plotly's ordinary legend clips/normalizes large marker glyphs, which can make
    # materially different square sizes look identical.  Use a dedicated,
    # plot-coordinate size key instead.  These markers use the exact same size
    # transform as the data squares, so their *area* is genuinely proportional
    # to the underlying value.
    reference_values = []
    reference_labels = []
    reference_sizes = []
    seen_labels = set()
    for fraction in (0.10, 0.35, 1.00):
        reference = max_value * fraction
        if reference <= 0:
            continue
        label = human_number(reference)
        if label in seen_labels:
            continue
        seen_labels.add(label)
        reference_values.append(reference)
        reference_labels.append(label)
        reference_sizes.append(_square_side(reference, max_value, min_marker_size, max_marker_size))
    if reference_values:
        key_x = list(range(len(reference_values)))
        square_fig.add_trace(go.Scatter(
            x=key_x, y=[0.0] * len(key_x), mode="markers+text",
            text=reference_labels, textposition="bottom center",
            marker=dict(size=reference_sizes, symbol="square", color=primary, opacity=opacity, line=dict(color=border_color, width=1.0)),
            hovertemplate="Reference size: %{text}<extra></extra>",
            showlegend=False, xaxis="x2", yaxis="y2", name="Size key",
        ))

    row_labels = [f"#{int(rank_lookup[address]):02d}  {short_address(address)}" for address in top_addresses]
    left_margin = int(cfg.get("address_bubble_left_margin", 205))
    right_margin = int(cfg.get("address_bubble_right_margin", 55))
    min_width = int(cfg.get("address_bubble_min_width", 980))
    max_width = int(cfg.get("address_bubble_max_width", 1450))
    chart_width = int(max(min_width, min(max_width, left_margin + right_margin + column_width * len(ordered_windows))))
    chart_height = int(max(820, row_height * len(row_labels) + 260))
    square_fig.update_layout(
        title=f"{cfg['title_prefix']}: Persistence, turnover, and magnitude of the overall top {top_n} {title_subject}",
        width=chart_width, height=chart_height, autosize=False,
        # Keep the time axis at the top, but place the quantitative square-size key
        # centered below the matrix where it is easier to read and safely inside the PDF.
        xaxis=dict(title=None, side="top", categoryorder="array", categoryarray=ordered_windows, showgrid=True, gridcolor="rgba(120, 140, 160, 0.18)", tickangle=0, automargin=True, domain=[0.0, 1.0], anchor="y"),
        yaxis=dict(title="Overall rank and address", categoryorder="array", categoryarray=row_labels, autorange="reversed", showgrid=True, gridcolor="rgba(120, 140, 160, 0.14)", automargin=True, domain=[0.14, 1.0], anchor="x"),
        xaxis2=dict(domain=[0.36, 0.64], range=[-0.6, max(2.6, len(reference_values) - 0.4)], visible=False, anchor="y2", fixedrange=True),
        yaxis2=dict(domain=[0.015, 0.08], range=[-1.2, 0.8], visible=False, anchor="x2", fixedrange=True),
        margin=dict(l=left_margin, r=right_margin, t=145, b=105),
        showlegend=False,
        hovermode="closest",
    )
    square_fig.add_annotation(
        x=0.5, y=0.095, xref="paper", yref="paper", showarrow=False,
        text=f"<b>Square area = {axis_title}</b>", xanchor="center", yanchor="bottom",
        font=dict(size=11, color="#4B5563"),
    )
    matrix_name = output_name.replace("_stacked.html", "_bubble.html")
    save_address_fig(square_fig, output_folder / matrix_name, cfg, overall_totals, address_column=address_column, value_column=value_column, value_label=f"Overall {axis_title}")


def plot_yearly_top_addresses(df, output_folder, cfg, *, address_column, value_column, title_subject, axis_title, output_subfolder):
    top_n = int(cfg.get("yearly_top_n", cfg.get("top_n", 50)))
    yearly = df.copy()
    yearly["year"] = pd.to_datetime(yearly["month"]).dt.year
    for year, year_df in yearly.groupby("year", sort=True):
        ranked = year_df.groupby(address_column, as_index=False)[value_column].sum().nlargest(top_n, value_column).sort_values(value_column, ascending=False).reset_index(drop=True)
        if ranked.empty:
            continue
        ranked[address_column] = ranked[address_column].map(normalize_address)
        ranked["rank"] = ranked.index + 1
        ranked["address_label"] = ranked.apply(lambda row: f"#{int(row['rank']):02d}  {short_address(row[address_column])}", axis=1)
        ranked["formatted_value"] = ranked[value_column].map(human_number)
        plotted = ranked.sort_values(value_column, ascending=True)
        fig = go.Figure(go.Bar(
            x=plotted[value_column], y=plotted["address_label"], orientation="h",
            marker_color=cfg.get("primary_color", "#009393"),
            customdata=list(zip(plotted[address_column], plotted["formatted_value"], plotted["rank"])),
            hovertemplate="Rank: %{customdata[2]}<br>Address: %{customdata[0]}<br>" + axis_title + ": %{customdata[1]}<br>Click to copy address<extra></extra>",
        ))
        fig.update_layout(title=f"{cfg['title_prefix']}: Top {top_n} {title_subject} in {year}", xaxis=human_axis(plotted[value_column], axis_title), yaxis=dict(title="Rank and address"), height=max(680, 29 * len(plotted) + 180), margin=dict(l=165, r=40, t=90, b=70))
        save_address_fig(fig, output_folder / output_subfolder / f"{int(year)}.html", cfg, ranked, address_column=address_column, value_column=value_column, value_label=axis_title)


def plot_top100_users(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    df = read_csv(input_folder, "monthly_top100_users", cfg)
    if df is None or df.empty:
        return
    top_n = int(cfg.get("top_n", 50))
    plot_overall_top_addresses(df, output_folder, cfg, address_column="address", value_column="outgoing_volume", title=f"{cfg['title_prefix']}: Top {top_n} addresses by outgoing volume", axis_title=f"Outgoing {cfg['token_symbol']} volume", output_name="top20_users_total_volume.html")
    plot_six_month_address_views(df, output_folder, cfg, address_column="address", value_column="outgoing_volume", title_subject="addresses by outgoing volume", axis_title=f"Outgoing {cfg['token_symbol']} volume", output_name="top20_users_6month_stacked.html")
    # Ethereum stablecoins receive a single combined all+EOA/SC concentration figure
    # in the chain analysis output. Keep the standalone all-only chart for BSC and
    # comparison/native assets, where no EOA/SC subset comparison exists.
    if not (str(cfg.get("chain", "")).lower() == "ethereum" and cfg.get("asset_role", "stablecoin") == "stablecoin"):
        plot_monthly_top_sender_concentration(input_folder, output_folder, cfg, df)


def plot_top100_funded_by(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    df = read_csv(input_folder, "monthly_top100_funded_by", cfg)
    if df is None or df.empty:
        return
    top_n = int(cfg.get("top_n", 50))
    plot_overall_top_addresses(df, output_folder, cfg, address_column="funded_by_address", value_column="newly_funded_addresses", title=f"{cfg['title_prefix']}: Top {top_n} funding addresses by number of first-time recipients", axis_title="First-time recipients funded", output_name="top20_funders_new_addresses.html")
    plot_six_month_address_views(df, output_folder, cfg, address_column="funded_by_address", value_column="newly_funded_addresses", title_subject="funding addresses", axis_title="First-time recipients funded", output_name="top20_funders_6month_stacked.html")


def plot_monthly_top_sender_concentration(
    input_folder: Path, output_folder: Path, cfg: dict, top_users: pd.DataFrame
) -> None:
    """Plot the share of total monthly transfer volume sent by the top N senders."""
    activity = read_csv(input_folder, "monthly_activity", cfg)
    if activity is None or activity.empty or "token_volume" not in activity.columns:
        return
    top_n = int(cfg.get("sender_concentration_top_n", 50))
    frame = top_users.copy()
    frame["month"] = pd.to_datetime(frame["month"], errors="raise")
    frame["outgoing_volume"] = pd.to_numeric(frame["outgoing_volume"], errors="coerce").fillna(0.0)
    rows = []
    for month, part in frame.groupby("month", sort=True):
        top = part.nlargest(top_n, "outgoing_volume")
        rows.append({"month": month, "top_volume": float(top["outgoing_volume"].sum()), "retained_senders": int(len(top))})
    concentration = pd.DataFrame(rows)
    activity = activity[["month", "token_volume"]].copy()
    activity["month"] = pd.to_datetime(activity["month"], errors="raise")
    activity["token_volume"] = pd.to_numeric(activity["token_volume"], errors="coerce")
    concentration = concentration.merge(activity, on="month", how="inner")
    concentration["share"] = concentration["top_volume"].div(concentration["token_volume"].where(concentration["token_volume"] > 0))
    concentration = concentration.dropna(subset=["share"]).sort_values("month")
    if concentration.empty:
        return
    if (concentration["share"] > 1.001).any():
        worst = float(concentration["share"].max())
        print(f"[warn] {cfg.get('file_prefix')}: top-{top_n} sender volume share exceeds 100% (max {worst:.3f}); check aggregate semantics")
    fig = go.Figure(go.Scatter(
        x=concentration["month"], y=concentration["share"], mode="lines+markers",
        name=f"Top {top_n} sender share", line=dict(color=cfg.get("primary_color", "#009393"), width=2.2, dash=_chain_line_dash(str(cfg.get("chain", "")))),
        marker=dict(size=5),
        customdata=list(zip(concentration["top_volume"].map(human_number), concentration["token_volume"].map(human_number), concentration["retained_senders"])),
        hovertemplate=f"%{{x|%b %Y}}<br>Top {top_n} sender concentration: %{{y:.2%}}<br>Top {top_n} outgoing volume: %{{customdata[0]}}<br>Total transfer volume: %{{customdata[1]}}<extra></extra>",
    ))
    fig.update_layout(
        title=f"{cfg['title_prefix']}: Monthly top-{top_n} sender concentration by transfer volume",
        xaxis=dict(title="Month"), yaxis=dict(title=f"Share of monthly {cfg['token_symbol']} transfer volume", tickformat=".0%", rangemode="tozero"),
        hovermode="x unified", height=650,
    )
    add_regulatory_events(fig, cfg)
    save_fig(fig, output_folder / f"top{top_n}_sender_volume_concentration.html", cfg)


def cleanup_obsolete_top_address_outputs(output_folder: Path, cfg: dict) -> None:
    """Remove stale address plots that v5 intentionally no longer produces."""
    is_all = cfg.get("transfer_filter") == "all"
    html_names = [
        "top20_users_6month_stacked.html",
        "top20_funders_6month_stacked.html",
    ]
    if not is_all:
        html_names += [
            "top20_users_total_volume.html",
            "top20_users_6month_bubble.html",
            "top20_funders_new_addresses.html",
            "top20_funders_6month_bubble.html",
            "top50_sender_volume_concentration.html",
        ]
    for name in html_names:
        path = output_folder / name
        if path.exists():
            path.unlink()
            print(f"[cleanup] {path}")
        pdf = _static_path_for(path, cfg)
        if pdf is not None and pdf.exists():
            pdf.unlink()
            print(f"[cleanup] {pdf}")

    for dirname in ("yearly_top20_users", "yearly_top20_funders"):
        html_dir = output_folder / dirname
        if html_dir.exists():
            shutil.rmtree(html_dir)
            print(f"[cleanup] {html_dir}")
        static_root = cfg.get("static_output_folder")
        if static_root:
            pdf_dir = resolve_project_path(static_root) / dirname
            if pdf_dir.exists():
                shutil.rmtree(pdf_dir)
                print(f"[cleanup] {pdf_dir}")


def _dataset_has_input(dataset_cfg: dict) -> bool:
    """Return True when at least one expected aggregate exists for a dataset.

    This prevents the plotting run from creating hundreds of empty output
    directories when an input root is configured incorrectly.
    """
    input_folder = resolve_project_path(dataset_cfg["input_folder"])
    prefix = dataset_cfg.get("file_prefix")
    for suffix in (
        "monthly_activity",
        "monthly_adoption",
        "transfer_size_histogram_all_time",
        "monthly_top100_users",
        "monthly_top100_funded_by",
        "monthly_mint_burn",
    ):
        if find_file(input_folder, suffix, prefix) is not None:
            return True
    return False


def cleanup_reduced_subset_outputs(output_folder: Path, cfg: dict) -> None:
    """Remove legacy subset figures that are no longer generated.

    EOA/SC subset datasets intentionally retain only monthly activity and monthly
    transfer-size-bucket plots.  This cleanup prevents stale files from older runs
    from remaining in the output tree.
    """
    if cfg.get("transfer_filter") in (None, "all"):
        return

    obsolete_names = [
        "monthly_users.html",
        "monthly_adoption.html",
        "monthly_activity_intensity.html",
        "transfer_size_histogram_relative.html",
        "transfer_size_histogram_genius_pre_post.html",
        "monthly_mint_burn.html",
        "top20_users_total_volume.html",
        "top20_users_6month_stacked.html",
        "top20_users_6month_bubble.html",
        "top20_funders_new_addresses.html",
        "top20_funders_6month_stacked.html",
        "top20_funders_6month_bubble.html",
        "top50_sender_volume_concentration.html",
    ]

    for name in obsolete_names:
        path = output_folder / name
        if path.exists():
            path.unlink()
            print(f"[cleanup-subset] {path}")
        # Remove stale static exports as well, even when export_pdf is currently
        # disabled in the config.
        static_root = cfg.get("static_output_folder")
        if static_root:
            static_base = resolve_project_path(static_root) / Path(name).with_suffix("")
            for suffix in (".pdf", ".png"):
                static_path = static_base.with_suffix(suffix)
                if static_path.exists():
                    static_path.unlink()
                    print(f"[cleanup-subset] {static_path}")

    for dirname in ("yearly_top20_users", "yearly_top20_funders"):
        html_dir = output_folder / dirname
        if html_dir.exists():
            shutil.rmtree(html_dir)
            print(f"[cleanup-subset] {html_dir}")
        static_root = cfg.get("static_output_folder")
        if static_root:
            static_dir = resolve_project_path(static_root) / dirname
            if static_dir.exists():
                shutil.rmtree(static_dir)
                print(f"[cleanup-subset] {static_dir}")


def plot_dataset(dataset_cfg: dict) -> None:
    input_folder = resolve_project_path(dataset_cfg["input_folder"])
    output_folder = resolve_project_path(dataset_cfg["output_folder"])
    print(f"\n=== Plotting {dataset_cfg['name']} ===")
    print(f"[input]  {input_folder}")
    if not _dataset_has_input(dataset_cfg):
        print(f"[skip-dataset] no aggregate CSVs found for {dataset_cfg.get('file_prefix')} in {input_folder}")
        return
    output_folder.mkdir(parents=True, exist_ok=True)
    print(f"[output] {output_folder}")

    is_subset = dataset_cfg.get("transfer_filter") not in (None, "all")

    # Every dataset keeps the two plot families needed for the thesis:
    # monthly activity (volume + counts) and monthly transfer-size buckets.
    plot_monthly_activity(input_folder, output_folder, dataset_cfg)
    plot_monthly_transfer_size_buckets_count(input_folder, output_folder, dataset_cfg)
    plot_monthly_transfer_size_buckets_volume(input_folder, output_folder, dataset_cfg)

    if is_subset:
        # EOA/SC subset folders are deliberately kept compact.  The richer
        # user/adoption/intensity/top-address plots remain available for the
        # complete `all` dataset only.
        cleanup_reduced_subset_outputs(output_folder, dataset_cfg)
        return

    if dataset_cfg.get("show_mint_burn", False):
        plot_monthly_mint_burn(input_folder, output_folder, dataset_cfg)
    plot_monthly_users(input_folder, output_folder, dataset_cfg)
    plot_adoption(input_folder, output_folder, dataset_cfg)
    plot_activity_intensity(input_folder, output_folder, dataset_cfg)
    plot_transfer_size_histogram(input_folder, output_folder, dataset_cfg)
    plot_top100_users(input_folder, output_folder, dataset_cfg)
    plot_top100_funded_by(input_folder, output_folder, dataset_cfg)
    plot_genius_transfer_size_pre_post(input_folder, output_folder, dataset_cfg)
    cleanup_obsolete_top_address_outputs(output_folder, dataset_cfg)


# ---------------------------------------------------------------------------
# Dataset configuration
# ---------------------------------------------------------------------------


FILTER_LABELS = {
    "all": "all transfers",
    "eoa_eoa": "EOA → EOA",
    "eoa_sc": "EOA → SC",
    "sc_eoa": "SC → EOA",
    "sc_sc": "SC → SC",
}


def _safe_prefix(text: str) -> str:
    return text.lower().replace("-", "_")


def _expand_one_dataset_matrix(matrix: dict, global_cfg: dict | None = None) -> list[dict]:
    """Expand one chain-specific dataset matrix."""
    if not matrix:
        return []

    input_folder = matrix.get("input_folder", "output/final")
    output_root = matrix.get("output_root", "plots/plot_output/final")
    static_root = matrix.get("static_output_root", "plots/plot_output_pdf/final")
    filters = matrix.get("filters", ["all", "eoa_eoa", "eoa_sc", "sc_eoa", "sc_sc"])
    chain = str(matrix.get("chain", "ethereum")).strip().lower()
    chain_label = str(matrix.get("chain_label", chain.title()))
    chain_badge = str(matrix.get("chain_badge", chain_label))
    chain_logo_path = matrix.get("chain_logo_path")
    explorer_address_url = matrix.get(
        "explorer_address_url",
        "https://bscscan.com/address/" if chain == "bsc" else "https://etherscan.io/address/",
    )

    datasets: list[dict] = []
    for token in matrix.get("tokens", []):
        if not token.get("enabled", True):
            continue
        symbol = str(token["symbol"]).upper()
        token_filters = token.get("filters", filters)
        for transfer_filter in token_filters:
            prefix = f"{_safe_prefix(symbol)}_{transfer_filter}"
            label = FILTER_LABELS.get(transfer_filter, transfer_filter)
            datasets.append({
                "name": f"{symbol} {label} [{chain_label}]",
                "token_symbol": symbol,
                "title_prefix": f"{symbol} ({label})",
                "input_folder": input_folder,
                "output_folder": f"{output_root}/{prefix}",
                "static_output_folder": f"{static_root}/{prefix}",
                "file_prefix": prefix,
                "transfer_filter": transfer_filter,
                "chain": chain,
                "chain_label": chain_label,
                "chain_badge": chain_badge,
                "chain_logo_path": chain_logo_path,
                "explorer_address_url": explorer_address_url,
                "primary_color": token.get("primary_color", "#009393"),
                "secondary_color": token.get("secondary_color", "#E67E22"),
                "transaction_color": token.get("transaction_color", "#4C78A8"),
                "bucket_color_sequence": token.get("bucket_color_sequence"),
                "volume_column": token.get("volume_column", "token_volume"),
                "show_mint_burn": bool(matrix.get("show_mint_burn", True)) and transfer_filter == "all",
                "asset_role": token.get("asset_role", "stablecoin"),
                "peg": token.get("peg"),
                "genius_aligned": token.get("genius_aligned"),
            })

    # Ethereum native transfers remain supported. BSC can simply omit this
    # block when native BNB was not aggregated/exported.
    native = matrix.get("native_eth") or {}
    if native.get("enabled", False):
        symbol = str(native.get("symbol", "ETH")).upper()
        prefix = f"{_safe_prefix(symbol)}_all"
        datasets.append({
            "name": f"{symbol} native top-level transfers [{chain_label}]",
            "token_symbol": symbol,
            "title_prefix": f"{symbol} (native top-level transfers)",
            "input_folder": input_folder,
            "output_folder": f"{output_root}/{prefix}",
            "static_output_folder": f"{static_root}/{prefix}",
            "file_prefix": prefix,
            "transfer_filter": "all",
            "chain": chain,
            "chain_label": chain_label,
            "chain_badge": chain_badge,
            "chain_logo_path": chain_logo_path,
            "explorer_address_url": explorer_address_url,
            "primary_color": native.get("primary_color", "#627EEA"),
            "secondary_color": native.get("secondary_color", "#E67E22"),
            "transaction_color": native.get("transaction_color", "#4C78A8"),
            "volume_column": native.get("volume_column", "token_volume"),
            "show_mint_burn": False,
            "asset_role": "comparison",
            "peg": native.get("peg", "ETH"),
            "genius_aligned": False,
        })
    return datasets


def expand_dataset_matrices(cfg: dict) -> list[dict]:
    """Expand all configured chains, with backward compatibility."""
    matrices = cfg.get("dataset_matrices") or {}
    datasets: list[dict] = []
    if isinstance(matrices, dict) and matrices:
        for key, matrix in matrices.items():
            if not isinstance(matrix, dict) or not matrix.get("enabled", True):
                continue
            local = dict(matrix)
            local.setdefault("chain", key)
            datasets.extend(_expand_one_dataset_matrix(local, cfg.get("global", {})))
        return datasets

    # Backward compatibility with the older single-chain key.
    matrix = cfg.get("dataset_matrix") or {}
    if matrix:
        datasets.extend(_expand_one_dataset_matrix(matrix, cfg.get("global", {})))
    return datasets


def dataset_lookup(datasets: list[dict]) -> dict[tuple[str, str], dict]:
    return {(str(d["token_symbol"]).upper(), str(d.get("transfer_filter", "all"))): d for d in datasets}


def analysis_cfg_for_chain(global_cfg: dict, cfg: dict, chain: str) -> dict | None:
    analysis = cfg.get("analysis", {}) or {}
    chains = analysis.get("chains") or {}
    chain_cfg = chains.get(chain) if isinstance(chains, dict) else None
    if chain_cfg is None:
        return None
    if not chain_cfg.get("enabled", True):
        return None
    return {
        **global_cfg,
        **chain_cfg,
        "name": f"{chain_cfg.get('chain_label', chain.title())} cross-token analysis",
        "token_symbol": "",
        "title_prefix": "",
        "chain": chain,
        "chain_label": chain_cfg.get("chain_label", chain.title()),
        "chain_badge": chain_cfg.get("chain_badge", chain_cfg.get("chain_label", chain.title())),
        "chain_logo_path": chain_cfg.get("chain_logo_path"),
        "output_folder": chain_cfg.get("output_folder", f"plots/plot_output/analysis/{chain}"),
        "static_output_folder": chain_cfg.get("static_output_folder", f"plots/plot_output_pdf/analysis/{chain}"),
        "data_output_folder": chain_cfg.get("data_output_folder", f"plots/analysis_output/{chain}"),
    }


def _read_dataset_file(ds: dict, suffix: str) -> pd.DataFrame | None:
    return read_csv(resolve_project_path(ds["input_folder"]), suffix, ds)


# ---------------------------------------------------------------------------
# Cross-token transfer-size comparison
# ---------------------------------------------------------------------------


def plot_cross_token_transfer_size(datasets: list[dict], cfg: dict) -> None:
    lookup = dataset_lookup(datasets)
    symbols = [str(s).upper() for s in cfg.get("stablecoins", [])]
    rows = []
    raw_buckets = set()
    for symbol in symbols:
        ds = lookup.get((symbol, "all"))
        if ds is None:
            continue
        df = _read_dataset_file(ds, "transfer_size_histogram_all_time")
        if df is None or df.empty:
            continue
        count_total = float(pd.to_numeric(df["transfer_count"], errors="coerce").fillna(0).sum())
        volume_total = float(pd.to_numeric(df["bucket_volume"], errors="coerce").fillna(0).sum())
        for _, row in df.iterrows():
            bucket = str(row["size_bucket"])
            raw_buckets.add(bucket)
            rows.append({
                "symbol": symbol, "bucket": bucket,
                "count_share": float(row["transfer_count"]) / count_total if count_total else 0.0,
                "volume_share": float(row["bucket_volume"]) / volume_total if volume_total else 0.0,
            })
    if not rows:
        return
    data = pd.DataFrame(rows)
    order = bucket_order(pd.DataFrame({"size_bucket": list(raw_buckets)}))
    colors = monochrome_palette(cfg.get("bucket_base_color", "#2775CA"), len(order))

    valid_symbols = [symbol for symbol in symbols if symbol in set(data["symbol"])]
    categories = []
    for symbol in valid_symbols:
        categories.extend([f"{symbol}<br>Transfer events", f"{symbol}<br>Volume"])

    fig = go.Figure()
    for bucket, color in zip(order, colors):
        y = []
        for symbol in valid_symbols:
            symbol_df = data[data["symbol"] == symbol]
            match = symbol_df[symbol_df["bucket"] == bucket]
            count_share = float(match["count_share"].iloc[0]) if not match.empty else 0.0
            volume_share = float(match["volume_share"].iloc[0]) if not match.empty else 0.0
            y.extend([count_share, volume_share])
        fig.add_trace(go.Bar(
            x=categories, y=y, name=BUCKET_DISPLAY_LABELS.get(bucket, bucket),
            marker_color=color,
            hovertemplate="%{x}<br>" + BUCKET_DISPLAY_LABELS.get(bucket, bucket) + ": %{y:.2%}<extra></extra>",
        ))
    fig.update_layout(
        title="Stablecoin transfer-size distributions: transfer-event share versus volume share",
        barmode="stack", yaxis=dict(title="Share", tickformat=".0%", range=[0, 1]),
        xaxis=dict(title="Asset and measure", categoryorder="array", categoryarray=categories),
        hovermode="x unified", height=760, margin=dict(l=75, r=40, t=100, b=90),
    )
    save_fig(fig, resolve_project_path(cfg["output_folder"]) / "cross_token_transfer_size_distribution.html", cfg)


# ---------------------------------------------------------------------------
# EOA/SC composition
# ---------------------------------------------------------------------------


SUBSET_FILTERS = ["eoa_eoa", "eoa_sc", "sc_eoa", "sc_sc"]
SUBSET_LABELS = {
    "eoa_eoa": "EOA → EOA",
    "eoa_sc": "EOA → SC",
    "sc_eoa": "SC → EOA",
    "sc_sc": "SC → SC",
}
SUBSET_COLORS = ["#4C78A8", "#F58518", "#54A24B", "#B279A2"]


def _load_subset_monthly(datasets: list[dict], symbol: str, value_column: str) -> pd.DataFrame | None:
    lookup = dataset_lookup(datasets)
    frames = []
    for subset in SUBSET_FILTERS:
        ds = lookup.get((symbol, subset))
        if ds is None:
            return None
        df = _read_dataset_file(ds, "monthly_activity")
        if df is None or df.empty or value_column not in df.columns:
            return None
        part = df[["month", value_column]].copy()
        part["subset"] = subset
        frames.append(part)
    return pd.concat(frames, ignore_index=True) if frames else None


def _load_subset_monthly_for_genius(
    datasets: list[dict],
    symbol: str,
    value_column: str,
    cfg: dict,
) -> pd.DataFrame | None:
    """Load subset values using the sub-cent rule for GENIUS transfer counts."""
    lookup = dataset_lookup(datasets)
    frames = []
    for subset in SUBSET_FILTERS:
        ds = lookup.get((symbol, subset))
        if ds is None:
            return None

        if value_column == "transfer_count" and _genius_dust_filter_applies(ds, cfg):
            qualifying = _genius_qualifying_transfer_totals(ds, cfg)
            if qualifying is None or qualifying.empty:
                return None
            part = qualifying[["month", "genius_qualifying_transfer_count"]].copy()
            part = part.rename(columns={"genius_qualifying_transfer_count": "transfer_count"})
        else:
            df = _read_dataset_file(ds, "monthly_activity")
            if df is None or df.empty or value_column not in df.columns:
                return None
            part = df[["month", value_column]].copy()

        part["subset"] = subset
        frames.append(part)
    return pd.concat(frames, ignore_index=True) if frames else None



def _compute_sender_concentration_for_dataset(ds: dict, top_n: int) -> pd.DataFrame | None:
    """Return monthly top-N sender-volume concentration for one retained dataset.

    The aggregation retains the monthly top 100 senders. Therefore top-N is exact
    for N <= the retained source limit (100 in the current exports).
    """
    top_users = _read_dataset_file(ds, "monthly_top100_users")
    activity = _read_dataset_file(ds, "monthly_activity")
    if top_users is None or top_users.empty or activity is None or activity.empty:
        return None
    if "outgoing_volume" not in top_users.columns or "token_volume" not in activity.columns:
        return None

    source_limit = int(ds.get("top_source_limit", 100))
    if top_n > source_limit:
        raise ValueError(
            f"Requested top-{top_n} sender concentration for {ds.get('file_prefix')}, "
            f"but only monthly top-{source_limit} senders were retained."
        )

    frame = top_users[["month", "address", "outgoing_volume"]].copy()
    frame["month"] = pd.to_datetime(frame["month"], errors="raise")
    frame["outgoing_volume"] = pd.to_numeric(frame["outgoing_volume"], errors="coerce").fillna(0.0)
    rows: list[dict] = []
    for month, part in frame.groupby("month", sort=True):
        top = part.nlargest(top_n, "outgoing_volume")
        rows.append({
            "month": month,
            "top_volume": float(top["outgoing_volume"].sum()),
            "retained_senders": int(len(top)),
        })
    if not rows:
        return None

    concentration = pd.DataFrame(rows)
    total = activity[["month", "token_volume"]].copy()
    total["month"] = pd.to_datetime(total["month"], errors="raise")
    total["token_volume"] = pd.to_numeric(total["token_volume"], errors="coerce")
    concentration = concentration.merge(total, on="month", how="inner")
    concentration["share"] = concentration["top_volume"].div(
        concentration["token_volume"].where(concentration["token_volume"] > 0)
    )
    concentration = concentration.dropna(subset=["share"]).sort_values("month")
    if concentration.empty:
        return None
    if (concentration["share"] > 1.001).any():
        worst = float(concentration["share"].max())
        print(
            f"[warn] {ds.get('file_prefix')}: top-{top_n} sender volume share "
            f"exceeds 100% (max {worst:.3f}); check aggregate semantics"
        )
    return concentration


def plot_monthly_subset_sender_concentration(datasets: list[dict], cfg: dict) -> None:
    """One line chart per Ethereum stablecoin: all + four EOA/SC concentration series."""
    symbols = [str(s).upper() for s in cfg.get("stablecoins", [])]
    lookup = dataset_lookup(datasets)
    top_n = int(cfg.get("sender_concentration_top_n", 50))
    output_root = resolve_project_path(cfg["output_folder"]) / "sender_concentration"
    static_root = resolve_project_path(cfg["static_output_folder"]) / "sender_concentration"

    series_order = ["all", *SUBSET_FILTERS]
    series_labels = {"all": "All transfers", **SUBSET_LABELS}

    for symbol in symbols:
        all_ds = lookup.get((symbol, "all"))
        if all_ds is None:
            continue
        # Only build the combined plot where at least one subset exists. This keeps
        # BSC and native/comparison-only assets on their simpler all-only chart.
        if not any(lookup.get((symbol, subset)) is not None for subset in SUBSET_FILTERS):
            continue

        frames: list[pd.DataFrame] = []
        missing: list[str] = []
        for subset in series_order:
            ds = lookup.get((symbol, subset))
            if ds is None:
                missing.append(subset)
                continue
            part = _compute_sender_concentration_for_dataset(ds, top_n)
            if part is None or part.empty:
                missing.append(subset)
                continue
            part = part.copy()
            part["subset"] = subset
            frames.append(part)
        if not frames:
            continue
        if missing:
            print(f"[subset-concentration] {symbol}: missing series {', '.join(missing)}")

        data = pd.concat(frames, ignore_index=True)
        local_cfg = {
            **cfg,
            "output_folder": str(output_root / symbol.lower()),
            "static_output_folder": str(static_root / symbol.lower()),
            "token_symbol": symbol,
            "title_prefix": symbol,
            "chain_logo_path": all_ds.get("chain_logo_path", cfg.get("chain_logo_path")),
        }

        token_color = all_ds.get("primary_color", "#009393")
        color_map = {
            "all": token_color,
            **{subset: color for subset, color in zip(SUBSET_FILTERS, SUBSET_COLORS)},
        }
        # Ethereum interaction types are distinguished by color only. Dotted data
        # lines are reserved for BSC in explicit cross-chain comparisons.
        dash_map = {subset: "solid" for subset in series_order}

        fig = go.Figure()
        for subset in series_order:
            part = data[data["subset"] == subset].sort_values("month")
            if part.empty:
                continue
            fig.add_trace(go.Scatter(
                x=part["month"],
                y=part["share"],
                mode="lines",
                name=series_labels[subset],
                line=dict(
                    color=color_map[subset],
                    width=3.0 if subset == "all" else 2.0,
                    dash=dash_map[subset],
                ),
                customdata=list(zip(
                    part["top_volume"].map(human_number),
                    part["token_volume"].map(human_number),
                    part["retained_senders"],
                )),
                hovertemplate=(
                    "%{x|%b %Y}<br>" + series_labels[subset] +
                    f" — top {top_n} concentration: %{{y:.2%}}<br>" +
                    f"Top {top_n} outgoing volume: %{{customdata[0]}}<br>" +
                    "Total volume in this transfer set: %{customdata[1]}<extra></extra>"
                ),
            ))

        fig.update_layout(
            title=f"{symbol}: Monthly top-{top_n} sender volume concentration by interaction type",
            xaxis=dict(title="Month"),
            yaxis=dict(
                title=f"Share of monthly {symbol} transfer volume",
                tickformat=".0%",
                rangemode="tozero",
            ),
            hovermode="x unified",
            height=700,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
            margin=dict(l=85, r=55, t=125, b=65),
        )
        add_regulatory_events(fig, local_cfg)
        save_fig(
            fig,
            resolve_project_path(local_cfg["output_folder"]) / f"top{top_n}_sender_volume_concentration_by_subset.html",
            local_cfg,
        )


def plot_monthly_subset_absolute(datasets: list[dict], cfg: dict) -> None:
    """Absolute monthly EOA/SC composition.

    Bar height is the total transfer-event count or token volume for the month;
    the four mutually exclusive EOA/SC categories form the stacked sections.
    A reconciliation check compares the subset sum with the corresponding `all`
    aggregate and reports the largest relative mismatch.
    """
    symbols = [str(s).upper() for s in cfg.get("stablecoins", [])]
    output_root = resolve_project_path(cfg["output_folder"]) / "subset_composition"
    static_root = resolve_project_path(cfg["static_output_folder"]) / "subset_composition"
    lookup = dataset_lookup(datasets)

    for symbol in symbols:
        all_ds = lookup.get((symbol, "all"))
        for value_column, metric_label, stem in (
            ("transfer_count", "Transfer-event count", "monthly_subset_transfer_count_stacked"),
            ("token_volume", f"{symbol} transferred volume", "monthly_subset_volume_stacked"),
        ):
            df = _load_subset_monthly(datasets, symbol, value_column)
            if df is None or df.empty:
                continue
            df = df.copy()
            df[value_column] = pd.to_numeric(df[value_column], errors="coerce").fillna(0.0)

            # Reconcile against the complete transfer set.  This is diagnostic only;
            # the chart itself is built from the four mutually exclusive subsets.
            if all_ds is not None:
                all_df = _read_dataset_file(all_ds, "monthly_activity")
                if all_df is not None and not all_df.empty and value_column in all_df.columns:
                    subset_total = df.groupby("month", as_index=False)[value_column].sum().rename(columns={value_column: "subset_total"})
                    check = all_df[["month", value_column]].copy().rename(columns={value_column: "all_total"})
                    check["all_total"] = pd.to_numeric(check["all_total"], errors="coerce").fillna(0.0)
                    merged = check.merge(subset_total, on="month", how="inner")
                    denom = merged["all_total"].abs().where(merged["all_total"].abs() > 0)
                    rel = (merged["subset_total"] - merged["all_total"]).abs().div(denom).fillna(0.0)
                    if not rel.empty:
                        max_mismatch = float(rel.max())
                        if max_mismatch > 1e-6:
                            print(f"[subset-check] {symbol} {value_column}: max relative subset/all mismatch = {max_mismatch:.4%}")

            local_cfg = {**cfg, "output_folder": str(output_root / symbol.lower()), "static_output_folder": str(static_root / symbol.lower())}
            fig = go.Figure()
            month_totals = df.groupby("month")[value_column].sum()
            for subset, color in zip(SUBSET_FILTERS, SUBSET_COLORS):
                part = df[df["subset"] == subset].sort_values("month")
                fig.add_trace(go.Bar(
                    x=part["month"], y=part[value_column], name=SUBSET_LABELS[subset], marker_color=color,
                    customdata=part[value_column].map(human_number),
                    hovertemplate="%{x|%b %Y}<br>" + SUBSET_LABELS[subset] + ": %{customdata}<extra></extra>",
                ))
            fig.update_layout(
                title=f"{symbol}: Monthly activity by EOA/SC interaction type",
                barmode="stack", xaxis=dict(title="Month"),
                yaxis=human_axis(month_totals, metric_label),
                hovermode="x unified", bargap=0.05,
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
            )
            add_regulatory_events(fig, local_cfg)
            save_fig(fig, resolve_project_path(local_cfg["output_folder"]) / f"{stem}.html", local_cfg)


def plot_monthly_subset_composition(datasets: list[dict], cfg: dict) -> None:
    symbols = [str(s).upper() for s in cfg.get("stablecoins", [])]
    output_root = resolve_project_path(cfg["output_folder"]) / "subset_composition"
    static_root = resolve_project_path(cfg["static_output_folder"]) / "subset_composition"
    for symbol in symbols:
        for value_column, metric_label, stem in (
            ("transfer_count", "Transfer-event share", "monthly_subset_transfer_share"),
            ("token_volume", "Transferred-volume share", "monthly_subset_volume_share"),
        ):
            df = _load_subset_monthly_for_genius(datasets, symbol, value_column, cfg)
            if df is None or df.empty:
                continue
            totals = df.groupby("month")[value_column].transform("sum")
            df["share"] = pd.to_numeric(df[value_column], errors="coerce").fillna(0).div(totals.where(totals != 0)).fillna(0.0)
            local_cfg = {**cfg, "output_folder": str(output_root / symbol.lower()), "static_output_folder": str(static_root / symbol.lower())}
            fig = go.Figure()
            for subset, color in zip(SUBSET_FILTERS, SUBSET_COLORS):
                part = df[df["subset"] == subset]
                fig.add_trace(go.Bar(
                    x=part["month"], y=part["share"], name=SUBSET_LABELS[subset], marker_color=color,
                    hovertemplate="%{x|%b %Y}<br>" + SUBSET_LABELS[subset] + ": %{y:.2%}<extra></extra>",
                ))
            fig.update_layout(
                title=f"{symbol}: Monthly EOA/SC composition by {metric_label.lower()}",
                barmode="stack", xaxis=dict(title="Month"),
                yaxis=dict(title=metric_label, tickformat=".0%", range=[0, 1]),
                hovermode="x unified", bargap=0.05,
            )
            add_regulatory_events(fig, local_cfg)
            save_fig(fig, resolve_project_path(local_cfg["output_folder"]) / f"{stem}.html", local_cfg)


def plot_genius_subset_pre_post(datasets: list[dict], cfg: dict) -> None:
    symbols = [str(s).upper() for s in cfg.get("stablecoins", [])]
    pre_start = pd.to_datetime(cfg["genius_pre_start"])
    pre_end = pd.to_datetime(cfg["genius_pre_end"])
    post_start = pd.to_datetime(cfg["genius_post_start"])
    post_end = pd.to_datetime(cfg["genius_post_end"])
    for value_column, title_metric, stem in (
        ("transfer_count", "transfer-event composition", "genius_pre_post_subset_transfer_composition"),
        ("token_volume", "transferred-volume composition", "genius_pre_post_subset_volume_composition"),
    ):
        data = {}
        valid_symbols = []
        for symbol in symbols:
            df = _load_subset_monthly_for_genius(datasets, symbol, value_column, cfg)
            if df is None or df.empty:
                continue
            valid_symbols.append(symbol)
            data[symbol] = {}
            for period, start, end in (("Pre-GENIUS", pre_start, pre_end), ("Post-GENIUS", post_start, post_end)):
                period_df = df[(df["month"] >= start) & (df["month"] < end)]
                sums = period_df.groupby("subset")[value_column].sum().reindex(SUBSET_FILTERS, fill_value=0.0)
                total = float(sums.sum())
                data[symbol][period] = {subset: (float(sums[subset]) / total if total else 0.0) for subset in SUBSET_FILTERS}
        if not valid_symbols:
            continue

        categories = []
        for symbol in valid_symbols:
            categories.extend([f"{symbol}<br>Pre", f"{symbol}<br>Post"])
        fig = go.Figure()
        for subset, color in zip(SUBSET_FILTERS, SUBSET_COLORS):
            y = []
            for symbol in valid_symbols:
                y.extend([data[symbol]["Pre-GENIUS"][subset], data[symbol]["Post-GENIUS"][subset]])
            fig.add_trace(go.Bar(
                x=categories, y=y, name=SUBSET_LABELS[subset], marker_color=color,
                hovertemplate="%{x}<br>" + SUBSET_LABELS[subset] + ": %{y:.2%}<extra></extra>",
            ))
        fig.update_layout(
            title=f"EOA/SC {title_metric}: six months before versus after GENIUS",
            barmode="stack", yaxis=dict(title="Share", tickformat=".0%", range=[0, 1]),
            xaxis=dict(title="Stablecoin and period", categoryorder="array", categoryarray=categories),
            height=760, hovermode="x unified", margin=dict(l=75, r=45, t=100, b=95),
        )
        save_fig(fig, resolve_project_path(cfg["output_folder"]) / f"{stem}.html", cfg)


def _reconstructed_top_n(df: pd.DataFrame, address_column: str, value_column: str, n: int) -> pd.DataFrame:
    result = df.copy()
    result[address_column] = result[address_column].map(normalize_address)
    aggregate_columns = {value_column: "sum"}
    if "funded_volume" in result.columns and value_column != "funded_volume":
        aggregate_columns["funded_volume"] = "sum"
    ranked = result.groupby(address_column, as_index=False).agg(aggregate_columns)
    ranked = ranked.nlargest(n, value_column).sort_values(value_column, ascending=False).reset_index(drop=True)
    ranked["rank"] = ranked.index + 1
    return ranked


def _write_analysis_csv(df: pd.DataFrame, cfg: dict, name: str) -> None:
    folder = resolve_project_path(cfg.get("data_output_folder", "plots/analysis_output"))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    df.to_csv(path, index=False)
    print(f"[data] {path}")


def plot_overlap_matrix(matrix: pd.DataFrame, title: str, output_name: str, cfg: dict, top_n: int) -> None:
    matrix = matrix.copy()
    z = matrix.astype(float).to_numpy(copy=True)
    diag_idx = np.diag_indices_from(z)
    z[diag_idx] = np.nan
    off_diag = pd.Series(z.flatten()).dropna()
    zmax = float(off_diag.max()) if not off_diag.empty else float(max(1, top_n))
    zmax = max(1.0, zmax)
    text = matrix.astype(object).to_numpy(copy=True)
    for i in range(min(text.shape[0], text.shape[1])):
        text[i, i] = ""
    fig = go.Figure(go.Heatmap(
        z=z, x=matrix.columns.tolist(), y=matrix.index.tolist(),
        zmin=0, zmax=zmax,
        colorscale=[
            [0.00, "#FFFFFF"],
            [0.15, "#EEF5FB"],
            [0.35, "#D7E8F6"],
            [0.55, "#A9CFE8"],
            [0.75, "#6BAED6"],
            [1.00, "#2171B5"],
        ],
        text=text, texttemplate="%{text}",
        colorbar=dict(title="Shared<br>addresses"),
        hovertemplate="%{y} ∩ %{x}: %{z} shared addresses<extra></extra>",
        hoverongaps=False,
    ))
    fig.update_layout(
        title=title, xaxis=dict(title="Asset", side="bottom"),
        yaxis=dict(title="Asset", autorange="reversed"),
        width=950, height=900, margin=dict(l=90, r=90, t=105, b=90),
    )
    save_fig(fig, resolve_project_path(cfg["output_folder"]) / output_name, cfg)


def plot_cross_token_address_overlaps(datasets: list[dict], cfg: dict) -> None:
    requested_n = int(cfg.get("cross_token_top_n", 100))
    source_limit = int(cfg.get("top_source_limit", 100))
    if requested_n > source_limit:
        print(f"[warn] cross_token_top_n={requested_n} exceeds retained monthly top-{source_limit}; capping at {source_limit}")
    top_n = min(requested_n, source_limit)
    symbols = [str(s).upper() for s in cfg.get("overlap_assets", cfg.get("stablecoins", []))]
    lookup = dataset_lookup(datasets)

    analyses = [
        ("senders", "monthly_top100_users", "address", "outgoing_volume"),
        ("funders", "monthly_top100_funded_by", "funded_by_address", "newly_funded_addresses"),
    ]
    for role, suffix, address_col, value_col in analyses:
        top_sets: dict[str, set[str]] = {}
        detail_frames = []
        for symbol in symbols:
            ds = lookup.get((symbol, "all"))
            if ds is None:
                continue
            df = _read_dataset_file(ds, suffix)
            if df is None or df.empty:
                continue
            ranked = _reconstructed_top_n(df, address_col, value_col, top_n)
            top_sets[symbol] = set(ranked[address_col])
            ranked.insert(0, "symbol", symbol)
            detail_frames.append(ranked)
        valid = [s for s in symbols if s in top_sets]
        if not valid:
            continue
        matrix = pd.DataFrame(index=valid, columns=valid, dtype=int)
        for left in valid:
            for right in valid:
                matrix.loc[left, right] = len(top_sets[left] & top_sets[right])
        matrix = matrix.astype(int)
        _write_analysis_csv(matrix.reset_index(names="asset"), cfg, f"top_{role}_overlap_top{top_n}.csv")
        if detail_frames:
            _write_analysis_csv(pd.concat(detail_frames, ignore_index=True), cfg, f"reconstructed_all_time_top{top_n}_{role}.csv")
        title = f"Cross-asset overlap of reconstructed all-time top {top_n} {role}"
        plot_overlap_matrix(matrix, title, f"top_{role}_overlap_top{top_n}.html", cfg, top_n)


# ---------------------------------------------------------------------------
# GENIUS Act analysis
# ---------------------------------------------------------------------------


GENIUS_METRICS = {
    "token_volume": "Transfer volume",
    "transfer_count": "Transfer-event count",
    "active_addresses": "Active addresses",
    "newly_adopted_addresses": "First-time participants",
    "average_transfer_size": "Average transfer size",
    "volume_per_active_address": "Volume per active address",
    "transfers_per_active_address": "Transfer events per active address",
}

# January 2026 is excluded from the post-GENIUS average whenever the metric is
# address-dependent.  The ordinary pre/post window remains unchanged for the
# transfer-volume/count and average-transfer-size metrics.
GENIUS_ADDRESS_RELATED_METRICS = {
    "active_addresses",
    "newly_adopted_addresses",
    "volume_per_active_address",
    "transfers_per_active_address",
}


def _genius_dust_filter_applies(ds: dict, cfg: dict) -> bool:
    """Return True when the sub-cent filter should be used for this dataset.

    The current thesis rule applies the 0.01 threshold only to USD-pegged
    stablecoins.  It must not be applied mechanically to comparison assets such
    as ETH, WBTC, BTCB, or BNB because 0.01 token units is not USD 0.01.
    """
    if not bool(cfg.get("genius_dust_filter_enabled", True)):
        return False
    if str(ds.get("asset_role", "stablecoin")).lower() != "stablecoin":
        return False
    allowed_pegs = {
        str(value).upper()
        for value in (cfg.get("genius_dust_filter_pegs") or ["USD"])
    }
    return str(ds.get("peg", "")).upper() in allowed_pegs


def _genius_qualifying_transfer_totals(ds: dict, cfg: dict) -> pd.DataFrame | None:
    """Reconstruct monthly >=0.01 transfer counts/volume from existing buckets.

    The aggregation's transfer-size bucket exports contain positive transfers.
    Summing all buckets except the configured sub-cent bucket therefore removes
    both positive sub-cent dust and zero-value transfer events from the
    transfer-count-dependent GENIUS metrics without any chain re-aggregation.
    """
    buckets = _read_dataset_file(ds, "monthly_transfer_size_buckets")
    if buckets is None or buckets.empty:
        if _genius_dust_filter_applies(ds, cfg):
            raise FileNotFoundError(
                f"{ds.get('file_prefix')}: monthly transfer-size buckets are required "
                "for the configured GENIUS sub-cent filter"
            )
        return None

    required = {"month", "size_bucket", "transfer_count", "bucket_volume"}
    missing = required.difference(buckets.columns)
    if missing:
        raise ValueError(
            f"{ds.get('file_prefix')}: monthly_transfer_size_buckets missing "
            f"columns required for the GENIUS sub-cent filter: {sorted(missing)}"
        )

    frame = buckets[list(required)].copy()
    frame["month"] = pd.to_datetime(frame["month"], errors="raise")
    frame["transfer_count"] = pd.to_numeric(frame["transfer_count"], errors="coerce").fillna(0.0)
    frame["bucket_volume"] = pd.to_numeric(frame["bucket_volume"], errors="coerce").fillna(0.0)
    excluded_buckets = {
        str(value)
        for value in (cfg.get("genius_dust_excluded_buckets") or ["<0.01"])
    }
    qualifying = frame[~frame["size_bucket"].astype(str).isin(excluded_buckets)].copy()
    return (
        qualifying.groupby("month", as_index=False)[["transfer_count", "bucket_volume"]]
        .sum()
        .rename(
            columns={
                "transfer_count": "genius_qualifying_transfer_count",
                "bucket_volume": "genius_qualifying_transfer_volume",
            }
        )
    )


def _genius_post_end_for_metric(cfg: dict, metric: str) -> pd.Timestamp:
    if metric in GENIUS_ADDRESS_RELATED_METRICS:
        # Exclusive end: 2026-01-01 means Aug-Dec 2025, i.e. five post months.
        return pd.to_datetime(cfg.get("genius_address_post_end", "2026-01-01"))
    return pd.to_datetime(cfg.get("genius_post_end", "2026-02-01"))


def _genius_post_period_label(cfg: dict, metric: str) -> str:
    if metric in GENIUS_ADDRESS_RELATED_METRICS:
        return str(cfg.get("genius_address_post_label", "Aug-Dec 2025"))
    return str(cfg.get("genius_post_label", "Aug 2025-Jan 2026"))


def build_cross_token_monthly_metrics(datasets: list[dict], cfg: dict, symbols: list[str]) -> pd.DataFrame:
    """Build monthly analysis metrics without modifying the raw exported CSVs.

    For configured USD-pegged stablecoins, `transfer_count` is replaced inside
    this analysis frame by the count reconstructed from transfer-size buckets
    >= 0.01. `token_volume` remains the full transfer volume. Average transfer
    size uses qualifying volume / qualifying count so numerator and denominator
    describe the same filtered transfer population.
    """
    lookup = dataset_lookup(datasets)
    frames = []
    for symbol in symbols:
        ds = lookup.get((symbol, "all"))
        if ds is None:
            continue
        activity = _read_dataset_file(ds, "monthly_activity")
        adoption = _read_dataset_file(ds, "monthly_adoption")
        if activity is None or activity.empty:
            continue

        columns = [
            c for c in ["month", "token_volume", "transfer_count", "active_addresses"]
            if c in activity.columns
        ]
        frame = activity[columns].copy()
        if adoption is not None and not adoption.empty and "newly_adopted_addresses" in adoption.columns:
            frame = frame.merge(adoption[["month", "newly_adopted_addresses"]], on="month", how="left")
        else:
            frame["newly_adopted_addresses"] = pd.NA

        for col in ["token_volume", "transfer_count", "active_addresses", "newly_adopted_addresses"]:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")

        frame["raw_transfer_count"] = frame["transfer_count"]
        frame["raw_token_volume"] = frame["token_volume"]
        frame["genius_dust_filter_applied"] = _genius_dust_filter_applies(ds, cfg)
        frame["genius_qualifying_transfer_volume"] = frame["token_volume"]

        if _genius_dust_filter_applies(ds, cfg):
            qualifying = _genius_qualifying_transfer_totals(ds, cfg)
            frame = frame.merge(qualifying, on="month", how="left", suffixes=("", "_bucket"))
            # A missing bucket row for an activity month means that no positive
            # transfer reached the 0.01 threshold in that month.
            frame["genius_qualifying_transfer_count"] = pd.to_numeric(
                frame["genius_qualifying_transfer_count"], errors="coerce"
            ).fillna(0.0)
            frame["genius_qualifying_transfer_volume"] = pd.to_numeric(
                frame["genius_qualifying_transfer_volume_bucket"], errors="coerce"
            ).fillna(0.0)
            frame = frame.drop(columns=["genius_qualifying_transfer_volume_bucket"])
            frame["transfer_count"] = frame["genius_qualifying_transfer_count"]
        else:
            frame["genius_qualifying_transfer_count"] = frame["transfer_count"]

        frame["genius_excluded_transfer_count"] = (
            frame["raw_transfer_count"] - frame["transfer_count"]
        )

        frame["average_transfer_size"] = frame["genius_qualifying_transfer_volume"].div(
            frame["transfer_count"].where(frame["transfer_count"] > 0)
        )
        frame["volume_per_active_address"] = frame["token_volume"].div(
            frame["active_addresses"].where(frame["active_addresses"] > 0)
        )
        frame["transfers_per_active_address"] = frame["transfer_count"].div(
            frame["active_addresses"].where(frame["active_addresses"] > 0)
        )
        frame["symbol"] = symbol
        frame["genius_aligned"] = bool(ds.get("genius_aligned", False))
        frames.append(frame)

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _genius_vline(fig: go.Figure, cfg: dict) -> None:
    x = str(cfg.get("genius_date", "2025-07-18"))
    fig.add_shape(
        type="line", x0=x, x1=x, y0=0, y1=1,
        xref="x", yref="paper",
        line=dict(dash="dash", width=1.5, color="#6B7280"),
    )
    fig.add_annotation(
        x=x, y=1, xref="x", yref="paper",
        text="GENIUS Act enacted", showarrow=False,
        xanchor="left", yanchor="bottom", font=dict(size=10),
    )


def _chain_line_dash(chain: str) -> str:
    """Use longer dashes for BSC so chain distinction stays readable."""
    return "longdash" if str(chain).lower() == "bsc" else "solid"


def plot_genius_normalized_metrics(datasets: list[dict], cfg: dict) -> None:
    symbols = [str(s).upper() for s in cfg.get("stablecoins", [])]
    data = build_cross_token_monthly_metrics(datasets, cfg, symbols)
    if data.empty:
        return
    pre_start = pd.to_datetime(cfg["genius_pre_start"])
    pre_end = pd.to_datetime(cfg["genius_pre_end"])
    plot_start = pd.to_datetime(cfg.get("genius_plot_start", "2024-07-01"))
    plot_end = pd.to_datetime(cfg.get("genius_plot_end", "2026-07-01"))
    colors = _asset_primary_colors(datasets, symbols)
    for metric, label in GENIUS_METRICS.items():
        fig = go.Figure()
        used = 0
        for index, symbol in enumerate(symbols):
            part = data[data["symbol"] == symbol].sort_values("month")
            if part.empty:
                continue
            baseline_values = part[(part["month"] >= pre_start) & (part["month"] < pre_end)][metric].dropna()
            if baseline_values.empty or float(baseline_values.mean()) == 0:
                continue
            baseline = float(baseline_values.mean())
            plotted = part[(part["month"] >= plot_start) & (part["month"] < plot_end)].copy()
            plotted["normalized"] = 100.0 * plotted[metric] / baseline
            analysis_dash = _chain_line_dash(str(cfg.get("chain", "")))
            fig.add_trace(go.Scatter(
                x=plotted["month"], y=plotted["normalized"], mode="lines",
                name=symbol,
                line=dict(color=colors[symbol], width=2, dash=analysis_dash),
                hovertemplate=f"%{{x|%b %Y}}<br>{symbol}: %{{y:.1f}}<extra></extra>",
            ))
            used += 1
        if not used:
            continue
        _genius_vline(fig, cfg)
        fig.add_hline(y=100, line_dash="dot", line_width=1, line_color="#A0A0A0")
        fig.update_layout(
            title=f"{label} around the GENIUS Act (pre-GENIUS monthly average = 100)",
            xaxis=dict(title="Month"), yaxis=dict(title="Index (Jan–Jun 2025 monthly average = 100)"),
            hovermode="x unified", height=720,
        )
        save_fig(fig, resolve_project_path(cfg["output_folder"]) / f"genius_normalized_{metric}.html", cfg)


def _plot_genius_pre_post_average_levels(levels: pd.DataFrame, cfg: dict) -> None:
    """Plot raw monthly averages for the two address metrics most useful in prose."""
    if levels.empty:
        return
    address_post_label = _genius_post_period_label(cfg, "active_addresses")
    for metric, label, stem in (
        ("active_addresses", "Average monthly active addresses", "genius_pre_post_average_monthly_active_addresses"),
        ("newly_adopted_addresses", "Average monthly first-time participants", "genius_pre_post_average_monthly_first_time_participants"),
    ):
        part = levels[levels["metric"] == metric].copy()
        if part.empty:
            continue
        symbols = part["symbol"].tolist()
        pre_color = cfg.get("genius_pre_level_color", "#4B5563")
        post_color = cfg.get("genius_post_level_color", "#4C78A8")
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=symbols, y=part["pre_mean"], name="Pre-GENIUS (Jan-Jun 2025)",
            marker_color=pre_color, customdata=[human_number(v) for v in part["pre_mean"]],
            hovertemplate="%{x}<br>Pre-GENIUS monthly average: %{customdata}<extra></extra>",
        ))
        fig.add_trace(go.Bar(
            x=symbols, y=part["post_mean"],
            name=f"Post-GENIUS ({address_post_label}; Jan 2026 excluded)",
            marker_color=post_color, customdata=[human_number(v) for v in part["post_mean"]],
            hovertemplate="%{x}<br>Post-GENIUS monthly average: %{customdata}<extra></extra>",
        ))
        all_values = pd.concat([part["pre_mean"], part["post_mean"]], ignore_index=True)
        fig.update_layout(
            title=f"{label}: pre/post-GENIUS comparison",
            xaxis=dict(title="Stablecoin"), yaxis=human_axis(all_values, label),
            barmode="group", hovermode="x unified", height=690,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        )
        save_fig(fig, resolve_project_path(cfg["output_folder"]) / f"{stem}.html", cfg)



def plot_genius_pre_post_changes(datasets: list[dict], cfg: dict) -> None:
    symbols = [str(s).upper() for s in cfg.get("stablecoins", [])]
    data = build_cross_token_monthly_metrics(datasets, cfg, symbols)
    if data.empty:
        return

    # Small reproducibility/audit table showing exactly how the sub-cent rule
    # changed the monthly transfer-event input used by the GENIUS calculations.
    audit_columns = [
        column for column in [
            "month", "symbol", "token_volume",
            "raw_transfer_count", "transfer_count", "genius_excluded_transfer_count",
            "genius_qualifying_transfer_volume", "genius_dust_filter_applied",
            "active_addresses", "newly_adopted_addresses",
            "average_transfer_size", "volume_per_active_address",
            "transfers_per_active_address",
        ]
        if column in data.columns
    ]
    _write_analysis_csv(
        data[audit_columns].sort_values(["symbol", "month"]),
        cfg,
        "genius_monthly_metric_inputs.csv",
    )

    pre_start = pd.to_datetime(cfg["genius_pre_start"])
    pre_end = pd.to_datetime(cfg["genius_pre_end"])
    post_start = pd.to_datetime(cfg["genius_post_start"])
    rows = []
    level_rows = []
    for symbol in symbols:
        part = data[data["symbol"] == symbol]
        if part.empty:
            continue
        row = {"symbol": symbol, "genius_aligned": bool(part["genius_aligned"].iloc[0])}
        for metric, metric_label in GENIUS_METRICS.items():
            metric_post_end = _genius_post_end_for_metric(cfg, metric)
            pre = part[(part["month"] >= pre_start) & (part["month"] < pre_end)][metric].dropna()
            post = part[(part["month"] >= post_start) & (part["month"] < metric_post_end)][metric].dropna()
            pre_mean = float(pre.mean()) if not pre.empty else math.nan
            post_mean = float(post.mean()) if not post.empty else math.nan
            pct_change = ((post_mean / pre_mean) - 1.0) * 100.0 if math.isfinite(pre_mean) and pre_mean != 0 and math.isfinite(post_mean) else math.nan

            # Keep the metric-named percentage column for the heatmap, while
            # also exporting the exact window sizes used in every cell.
            row[metric] = pct_change
            row[f"{metric}_pre_mean"] = pre_mean
            row[f"{metric}_post_mean"] = post_mean
            row[f"{metric}_pct_change"] = pct_change
            row[f"{metric}_pre_months"] = len(pre)
            row[f"{metric}_post_months"] = len(post)
            level_rows.append({
                "symbol": symbol,
                "genius_aligned": bool(part["genius_aligned"].iloc[0]),
                "metric": metric,
                "metric_label": metric_label,
                "pre_mean": pre_mean,
                "post_mean": post_mean,
                "pct_change": pct_change,
                "pre_months": len(pre),
                "post_months": len(post),
                "post_period": _genius_post_period_label(cfg, metric),
                "sub_cent_transfer_filter": bool(
                    metric in {"transfer_count", "average_transfer_size", "transfers_per_active_address"}
                    and part["genius_dust_filter_applied"].fillna(False).any()
                ),
            })
        rows.append(row)
    changes = pd.DataFrame(rows)
    levels = pd.DataFrame(level_rows)
    if changes.empty:
        return
    _write_analysis_csv(changes, cfg, "genius_pre_post_metric_changes.csv")
    if not levels.empty:
        _write_analysis_csv(levels, cfg, "genius_pre_post_monthly_average_levels.csv")
        _plot_genius_pre_post_average_levels(levels, cfg)

    metric_keys = list(GENIUS_METRICS.keys())
    z = changes[metric_keys].to_numpy(dtype=float)
    finite = pd.Series(z.flatten()).dropna().abs()
    color_limit = max(50.0, min(300.0, float(finite.quantile(0.90)) if not finite.empty else 100.0))
    text = [["" if pd.isna(value) else f"{value:+.0f}%" for value in row] for row in z]
    y_labels = [str(row.symbol) for row in changes.itertuples()]
    fig = go.Figure(go.Heatmap(
        z=z, x=[GENIUS_METRICS[k] for k in metric_keys], y=y_labels,
        zmid=0, zmin=-color_limit, zmax=color_limit, colorscale="RdBu",
        text=text, texttemplate="%{text}", colorbar=dict(title="Change"),
        hovertemplate="%{y}<br>%{x}: %{z:+.1f}%<extra></extra>",
    ))
    fig.update_layout(
        title="Change in monthly activity: pre/post-GENIUS average",
        xaxis=dict(title="Metric", tickangle=-25), yaxis=dict(title="Stablecoin", autorange="reversed"),
        width=1250, height=820, margin=dict(l=105, r=90, t=110, b=120),
    )
    save_fig(fig, resolve_project_path(cfg["output_folder"]) / "genius_pre_post_metric_changes.html", cfg)



def plot_usd_stablecoin_market_shares(datasets: list[dict], cfg: dict) -> None:
    symbols = [str(s).upper() for s in cfg.get("usd_stablecoins", [])]
    data = build_cross_token_monthly_metrics(datasets, cfg, symbols)
    if data.empty:
        return
    palette = px.colors.qualitative.Safe
    for metric, label, stem in (
        ("token_volume", "Transfer-volume share", "usd_stablecoin_volume_share"),
        ("transfer_count", "Transfer-event share", "usd_stablecoin_transfer_count_share"),
    ):
        pivot = data.pivot_table(index="month", columns="symbol", values=metric, aggfunc="sum", fill_value=0.0).reindex(columns=[s for s in symbols if s in data["symbol"].unique()], fill_value=0.0)
        totals = pivot.sum(axis=1).replace(0, pd.NA)
        shares = pivot.div(totals, axis=0).fillna(0.0)
        fig = go.Figure()
        for index, symbol in enumerate(shares.columns):
            fig.add_trace(go.Scatter(
                x=shares.index, y=shares[symbol], name=symbol, mode="lines",
                stackgroup="one", groupnorm="fraction", line=dict(width=0.7, color=palette[index % len(palette)]),
                hovertemplate=f"%{{x|%b %Y}}<br>{symbol}: %{{y:.2%}}<extra></extra>",
            ))
        _genius_vline(fig, cfg)
        fig.update_layout(
            title=f"USD-pegged stablecoin {label.lower()} over time",
            xaxis=dict(title="Month"), yaxis=dict(title=label, tickformat=".0%", range=[0, 1]),
            hovermode="x unified", height=720,
        )
        save_fig(fig, resolve_project_path(cfg["output_folder"]) / f"{stem}.html", cfg)


def plot_genius_benchmark(datasets: list[dict], cfg: dict) -> None:
    symbols = [str(s).upper() for s in cfg.get("genius_benchmark_assets", ["USDT", "USDC", "DAI", "PYUSD", "ETH", "WBTC"])]
    data = build_cross_token_monthly_metrics(datasets, cfg, symbols)
    if data.empty:
        return
    pre_start = pd.to_datetime(cfg["genius_pre_start"])
    pre_end = pd.to_datetime(cfg["genius_pre_end"])
    plot_start = pd.to_datetime(cfg.get("genius_plot_start", "2024-07-01"))
    plot_end = pd.to_datetime(cfg.get("genius_plot_end", "2026-07-01"))
    colors = _asset_primary_colors(datasets, symbols)
    for metric, label in (("token_volume", "Transfer volume"), ("transfer_count", "Transfer-event count")):
        fig = go.Figure()
        for symbol in symbols:
            part = data[data["symbol"] == symbol].sort_values("month")
            if part.empty:
                continue
            baseline_values = part[(part["month"] >= pre_start) & (part["month"] < pre_end)][metric].dropna()
            if baseline_values.empty or float(baseline_values.mean()) == 0:
                continue
            baseline = float(baseline_values.mean())
            plotted = part[(part["month"] >= plot_start) & (part["month"] < plot_end)].copy()
            plotted["normalized"] = 100.0 * plotted[metric] / baseline
            fig.add_trace(go.Scatter(x=plotted["month"], y=plotted["normalized"], mode="lines", name=symbol, line=dict(color=colors[symbol], width=2.2, dash=_chain_line_dash(str(cfg.get("chain", "")))), hovertemplate=f"%{{x|%b %Y}}<br>{symbol}: %{{y:.1f}}<extra></extra>"))
        _genius_vline(fig, cfg)
        fig.add_hline(y=100, line_dash="dot", line_width=1, line_color="#A0A0A0")
        fig.update_layout(
            title=f"GENIUS benchmark comparison: {label.lower()} (pre-GENIUS monthly average = 100)",
            xaxis=dict(title="Month"), yaxis=dict(title="Index (Jan–Jun 2025 monthly average = 100)"),
            hovermode="x unified", height=680,
        )
        save_fig(fig, resolve_project_path(cfg["output_folder"]) / f"genius_benchmark_{metric}.html", cfg)


def run_cross_token_analysis(datasets: list[dict], cfg: dict) -> None:
    print("\n=== Cross-token / regulatory analysis ===")
    resolve_project_path(cfg["output_folder"]).mkdir(parents=True, exist_ok=True)
    plot_cross_token_transfer_size(datasets, cfg)
    plot_monthly_subset_absolute(datasets, cfg)
    plot_monthly_subset_composition(datasets, cfg)
    plot_monthly_subset_sender_concentration(datasets, cfg)
    plot_cross_token_address_overlaps(datasets, cfg)
    plot_genius_normalized_metrics(datasets, cfg)
    plot_genius_pre_post_changes(datasets, cfg)
    plot_usd_stablecoin_market_shares(datasets, cfg)
    plot_genius_subset_pre_post(datasets, cfg)
    plot_genius_benchmark(datasets, cfg)


# ---------------------------------------------------------------------------
# Direct Ethereum / BSC comparison around GENIUS
# ---------------------------------------------------------------------------


def _cross_chain_cfg(global_cfg: dict, cfg: dict) -> dict | None:
    cc = cfg.get("cross_chain_analysis", {}) or {}
    if not cc.get("enabled", False):
        return None
    return {
        **global_cfg,
        **cc,
        "name": "Ethereum / BNB Smart Chain comparison",
        "token_symbol": "",
        "title_prefix": "",
        "chain_badge": cc.get("chain_badge", "Ethereum ↔ BNB Smart Chain"),
        "chain_logo_path": cc.get("chain_logo_path"),
        "chain_logo_paths": cc.get("chain_logo_paths"),
        "chain_logo_labels": cc.get("chain_logo_labels", ["Ethereum", "BNB Smart Chain"]),
        "output_folder": cc.get("output_folder", "plots/plot_output/analysis/cross_chain"),
        "static_output_folder": cc.get("static_output_folder", "plots/plot_output_pdf/analysis/cross_chain"),
        "data_output_folder": cc.get("data_output_folder", "plots/analysis_output/cross_chain"),
    }


def _datasets_for_chain(datasets: list[dict], chain: str) -> list[dict]:
    return [d for d in datasets if str(d.get("chain", "")).lower() == chain.lower()]


def _asset_primary_colors(datasets: list[dict], symbols: list[str]) -> dict[str, str]:
    """Resolve one consistent configured primary color per asset across chains."""
    colors: dict[str, str] = {}
    for symbol in symbols:
        for preferred_chain in ("ethereum", "bsc"):
            for ds in datasets:
                if (
                    str(ds.get("token_symbol", "")).upper() == symbol
                    and str(ds.get("transfer_filter", "all")) == "all"
                    and str(ds.get("chain", "")).lower() == preferred_chain
                    and ds.get("primary_color")
                ):
                    colors[symbol] = str(ds["primary_color"])
                    break
            if symbol in colors:
                break
    fallback = px.colors.qualitative.Dark24
    for index, symbol in enumerate(symbols):
        colors.setdefault(symbol, fallback[index % len(fallback)])
    return colors


def plot_cross_chain_genius_normalized(datasets: list[dict], cfg: dict) -> None:
    symbols = [str(s).upper() for s in cfg.get("common_assets", cfg.get("common_stablecoins", []))]
    pre_start = pd.to_datetime(cfg.get("genius_pre_start", "2025-01-01"))
    pre_end = pd.to_datetime(cfg.get("genius_pre_end", "2025-07-01"))
    plot_start = pd.to_datetime(cfg.get("genius_plot_start", "2024-07-01"))
    plot_end = pd.to_datetime(cfg.get("genius_plot_end", "2026-07-01"))
    colors = _asset_primary_colors(datasets, symbols)
    bsc_yellow = str(cfg.get("bsc_accent_color", "#F3BA2F"))

    chain_data: dict[str, pd.DataFrame] = {}
    for chain in ("ethereum", "bsc"):
        part = build_cross_token_monthly_metrics(_datasets_for_chain(datasets, chain), cfg, symbols)
        if not part.empty:
            part["chain"] = chain
            chain_data[chain] = part
    if len(chain_data) < 2:
        print("[skip] cross-chain GENIUS comparison requires both Ethereum and BSC data")
        return

    for metric, label in (
        ("token_volume", "Transfer volume"),
        ("transfer_count", "Transfer-event count"),
        ("active_addresses", "Active addresses"),
        ("newly_adopted_addresses", "First-time participants"),
    ):
        fig = go.Figure()
        for symbol in symbols:
            token_color = colors[symbol]
            for chain, chain_label in (("ethereum", "Ethereum"), ("bsc", "BSC")):
                data = chain_data[chain]
                part = data[data["symbol"] == symbol].sort_values("month")
                if part.empty or metric not in part.columns:
                    continue
                baseline_values = part[(part["month"] >= pre_start) & (part["month"] < pre_end)][metric].dropna()
                if baseline_values.empty or float(baseline_values.mean()) == 0:
                    continue
                baseline = float(baseline_values.mean())
                plotted = part[(part["month"] >= plot_start) & (part["month"] < plot_end)].copy()
                plotted["normalized"] = 100.0 * plotted[metric] / baseline
                if chain == "ethereum":
                    fig.add_trace(go.Scatter(
                        x=plotted["month"], y=plotted["normalized"], mode="lines",
                        name=f"{symbol} — Ethereum",
                        line=dict(color=token_color, width=2.6, dash="solid"),
                        hovertemplate=f"%{{x|%b %Y}}<br>{symbol} — Ethereum: %{{y:.1f}}<extra></extra>",
                    ))
                else:
                    # Same asset color as Ethereum, but a dotted line so chain identity remains visible
                    # without adding marker clutter.
                    fig.add_trace(go.Scatter(
                        x=plotted["month"], y=plotted["normalized"], mode="lines",
                        name=f"{symbol} — BSC",
                        line=dict(color=token_color, width=2.2, dash="longdash"),
                        hovertemplate=f"%{{x|%b %Y}}<br>{symbol} — BSC: %{{y:.1f}}<extra></extra>",
                    ))
        if not fig.data:
            continue
        _genius_vline(fig, cfg)
        fig.add_hline(y=100, line_dash="dot", line_width=1, line_color="#A0A0A0")
        fig.update_layout(
            title=f"Ethereum vs BNB Smart Chain: {label.lower()} around GENIUS (pre-GENIUS monthly average = 100)",
            xaxis=dict(title="Month"),
            yaxis=dict(title="Index (Jan–Jun 2025 monthly average = 100)"),
            hovermode="x unified", height=780, margin=dict(l=85, r=55, t=105, b=75),
        )
        save_fig(fig, resolve_project_path(cfg["output_folder"]) / f"genius_cross_chain_{metric}.html", cfg)


def plot_cross_chain_genius_pre_post(datasets: list[dict], cfg: dict) -> None:
    symbols = [str(s).upper() for s in cfg.get("common_assets", cfg.get("common_stablecoins", []))]
    pre_start = pd.to_datetime(cfg.get("genius_pre_start", "2025-01-01"))
    pre_end = pd.to_datetime(cfg.get("genius_pre_end", "2025-07-01"))
    post_start = pd.to_datetime(cfg.get("genius_post_start", "2025-08-01"))
    rows: list[dict[str, object]] = []
    level_rows: list[dict[str, object]] = []

    # Asset-major ordering keeps each Ethereum/BSC pair adjacent in the heatmap.
    chain_monthly: dict[str, pd.DataFrame] = {}
    for chain in ("ethereum", "bsc"):
        chain_monthly[chain] = build_cross_token_monthly_metrics(
            _datasets_for_chain(datasets, chain), cfg, symbols
        )

    for symbol in symbols:
        for chain, chain_label in (("ethereum", "Ethereum"), ("bsc", "BSC")):
            data = chain_monthly.get(chain, pd.DataFrame())
            if data.empty:
                continue
            part = data[data["symbol"] == symbol]
            if part.empty:
                continue
            row: dict[str, object] = {"chain": chain_label, "symbol": symbol}
            for metric, metric_label in GENIUS_METRICS.items():
                metric_post_end = _genius_post_end_for_metric(cfg, metric)
                pre = part[(part["month"] >= pre_start) & (part["month"] < pre_end)][metric].dropna()
                post = part[(part["month"] >= post_start) & (part["month"] < metric_post_end)][metric].dropna()
                pre_mean = float(pre.mean()) if not pre.empty else math.nan
                post_mean = float(post.mean()) if not post.empty else math.nan
                pct_change = ((post_mean / pre_mean) - 1.0) * 100.0 if math.isfinite(pre_mean) and pre_mean != 0 and math.isfinite(post_mean) else math.nan
                row[metric] = pct_change
                row[f"{metric}_pre_mean"] = pre_mean
                row[f"{metric}_post_mean"] = post_mean
                row[f"{metric}_pre_months"] = len(pre)
                row[f"{metric}_post_months"] = len(post)
                level_rows.append({
                    "chain": chain_label,
                    "symbol": symbol,
                    "metric": metric,
                    "metric_label": metric_label,
                    "pre_mean": pre_mean,
                    "post_mean": post_mean,
                    "pct_change": pct_change,
                    "pre_months": len(pre),
                    "post_months": len(post),
                    "post_period": _genius_post_period_label(cfg, metric),
                    "sub_cent_transfer_filter": bool(
                        metric in {"transfer_count", "average_transfer_size", "transfers_per_active_address"}
                        and part["genius_dust_filter_applied"].fillna(False).any()
                    ),
                })
            rows.append(row)

    changes = pd.DataFrame(rows)
    if changes.empty:
        return
    _write_analysis_csv(changes, cfg, "genius_cross_chain_pre_post_metric_changes.csv")
    if level_rows:
        _write_analysis_csv(
            pd.DataFrame(level_rows),
            cfg,
            "genius_cross_chain_pre_post_monthly_average_levels.csv",
        )
    metric_keys = list(GENIUS_METRICS.keys())
    z = changes[metric_keys].to_numpy(dtype=float)
    finite = pd.Series(z.flatten()).dropna().abs()
    color_limit = max(
        50.0,
        min(300.0, float(finite.quantile(0.90)) if not finite.empty else 100.0),
    )
    text = [["" if pd.isna(v) else f"{v:+.0f}%" for v in row] for row in z]
    labels = [f"{r.symbol} — {r.chain}" for r in changes.itertuples()]
    fig = go.Figure(go.Heatmap(
        z=z,
        x=[GENIUS_METRICS[k] for k in metric_keys],
        y=labels,
        zmid=0,
        zmin=-color_limit,
        zmax=color_limit,
        colorscale="RdBu",
        text=text,
        texttemplate="%{text}",
        colorbar=dict(title="Change"),
        hovertemplate="%{y}<br>%{x}: %{z:+.1f}%<extra></extra>",
    ))
    fig.update_layout(
        title="Ethereum vs BNB Smart Chain: pre/post-GENIUS activity change",
        xaxis=dict(title="Metric", tickangle=-25),
        yaxis=dict(title="Asset and chain", autorange="reversed"),
        width=1300,
        height=max(760, 55 * len(labels) + 260),
        margin=dict(l=165, r=90, t=115, b=125),
    )
    save_fig(
        fig,
        resolve_project_path(cfg["output_folder"])
        / "genius_cross_chain_pre_post_metric_changes.html",
        cfg,
    )



def plot_cross_chain_genius_divergence(datasets: list[dict], cfg: dict) -> None:
    """Plot Ethereum-minus-BSC divergence in pre/post-GENIUS metric changes.

    Each cell is measured in percentage points:

        divergence = Ethereum % change - BSC % change

    Positive values therefore indicate stronger growth (or a smaller decline)
    on Ethereum; negative values indicate stronger growth (or a smaller decline)
    on BSC. The function intentionally writes only the requested figure and does
    not emit auxiliary CSV files.
    """
    symbols = [
        str(s).upper()
        for s in cfg.get(
            "divergence_assets",
            cfg.get("common_stablecoins", ["USDT", "USDC", "DAI", "USDE", "USD1"]),
        )
    ]
    if not symbols:
        return

    pre_start = pd.to_datetime(cfg.get("genius_pre_start", "2025-01-01"))
    pre_end = pd.to_datetime(cfg.get("genius_pre_end", "2025-07-01"))
    post_start = pd.to_datetime(cfg.get("genius_post_start", "2025-08-01"))

    chain_monthly: dict[str, pd.DataFrame] = {}
    for chain in ("ethereum", "bsc"):
        monthly = build_cross_token_monthly_metrics(
            _datasets_for_chain(datasets, chain), cfg, symbols
        )
        if monthly.empty:
            print(f"[skip] divergence heatmap: no {chain} monthly metrics found")
            return
        chain_monthly[chain] = monthly

    metric_keys = list(GENIUS_METRICS.keys())
    eth_values: list[list[float]] = []
    bsc_values: list[list[float]] = []
    valid_symbols: list[str] = []

    for symbol in symbols:
        per_chain: dict[str, list[float]] = {}
        complete = True
        for chain in ("ethereum", "bsc"):
            part = chain_monthly[chain]
            part = part[part["symbol"] == symbol]
            if part.empty:
                complete = False
                break

            values: list[float] = []
            for metric in metric_keys:
                metric_post_end = _genius_post_end_for_metric(cfg, metric)
                pre = part[
                    (part["month"] >= pre_start) & (part["month"] < pre_end)
                ][metric].dropna()
                post = part[
                    (part["month"] >= post_start) & (part["month"] < metric_post_end)
                ][metric].dropna()

                pre_mean = float(pre.mean()) if not pre.empty else math.nan
                post_mean = float(post.mean()) if not post.empty else math.nan
                pct_change = (
                    ((post_mean / pre_mean) - 1.0) * 100.0
                    if math.isfinite(pre_mean)
                    and pre_mean != 0
                    and math.isfinite(post_mean)
                    else math.nan
                )
                values.append(pct_change)
            per_chain[chain] = values

        if not complete:
            print(f"[skip-asset] divergence heatmap: {symbol} is not available on both chains")
            continue

        valid_symbols.append(symbol)
        eth_values.append(per_chain["ethereum"])
        bsc_values.append(per_chain["bsc"])

    if not valid_symbols:
        print("[skip] divergence heatmap: no common assets available")
        return

    eth = np.asarray(eth_values, dtype=float)
    bsc = np.asarray(bsc_values, dtype=float)
    divergence = eth - bsc

    finite = pd.Series(divergence.flatten()).dropna().abs()
    configured_limit = cfg.get("divergence_color_limit")
    if configured_limit is not None:
        color_limit = float(configured_limit)
    else:
        # Robust scale: one very young asset should not flatten the rest of the
        # heatmap. Extreme values remain visible in the cell text/hover.
        color_limit = max(
            50.0,
            min(300.0, float(finite.quantile(0.90)) if not finite.empty else 100.0),
        )

    text_values = [
        ["" if pd.isna(value) else f"{value:+.0f} pp" for value in row]
        for row in divergence
    ]
    customdata = np.dstack((eth, bsc))

    fig = go.Figure(go.Heatmap(
        z=divergence,
        x=[GENIUS_METRICS[key] for key in metric_keys],
        y=valid_symbols,
        zmid=0,
        zmin=-color_limit,
        zmax=color_limit,
        colorscale="RdBu",
        zsmooth=False,
        xgap=1,
        ygap=1,
        text=text_values,
        texttemplate="%{text}",
        customdata=customdata,
        colorbar=dict(title="ETH - BSC<br>(percentage points)"),
        hovertemplate=(
            "%{y}<br>%{x}"
            "<br>Ethereum change: %{customdata[0]:+.1f}%"
            "<br>BSC change: %{customdata[1]:+.1f}%"
            "<br>Difference: %{z:+.1f} pp<extra></extra>"
        ),
    ))
    fig.update_layout(
        title="Ethereum - BNB Smart Chain: divergence in pre/post-GENIUS metric changes",
        xaxis=dict(title="Metric", tickangle=-25),
        yaxis=dict(title="Stablecoin", autorange="reversed"),
        width=int(cfg.get("divergence_width", 1300)),
        height=int(cfg.get("divergence_height", max(600, 62 * len(valid_symbols) + 260))),
        margin=dict(l=105, r=125, t=115, b=125),
    )
    save_fig(
        fig,
        resolve_project_path(cfg["output_folder"])
        / "genius_cross_chain_divergence_heatmap.html",
        cfg,
    )

def build_cross_chain_combined_monthly_metrics(
    datasets: list[dict],
    cfg: dict,
    symbols: list[str],
) -> pd.DataFrame:
    """Pool Ethereum and BSC monthly metrics before deriving cross-chain ratios.

    Additive quantities (transfer volume, qualifying transfer count, active-address
    count, first-time-participant count, and qualifying transfer volume) are summed
    across chains month by month. The three ratio metrics are then recalculated from
    those pooled quantities. This avoids averaging chain-specific ratios or percentage
    changes, which would give equal weight to chains with very different activity.

    Address counts are intentionally summed rather than deduplicated. They therefore
    represent combined chain-level address activity, not unique cross-chain users.
    """
    chain_frames: list[pd.DataFrame] = []
    for chain, chain_label in (("ethereum", "Ethereum"), ("bsc", "BSC")):
        monthly = build_cross_token_monthly_metrics(
            _datasets_for_chain(datasets, chain), cfg, symbols
        )
        if monthly.empty:
            continue

        required = [
            "month",
            "symbol",
            "token_volume",
            "transfer_count",
            "active_addresses",
            "newly_adopted_addresses",
            "genius_qualifying_transfer_volume",
        ]
        missing = [column for column in required if column not in monthly.columns]
        if missing:
            raise ValueError(
                f"{chain_label}: combined cross-chain metrics require columns {missing}"
            )
        part = monthly[required].copy()
        part["chain"] = chain_label
        chain_frames.append(part)

    if len(chain_frames) < 2:
        print("[skip] combined-chain comparison requires both Ethereum and BSC data")
        return pd.DataFrame()

    source = pd.concat(chain_frames, ignore_index=True)
    numeric_columns = [
        "token_volume",
        "transfer_count",
        "active_addresses",
        "newly_adopted_addresses",
        "genius_qualifying_transfer_volume",
    ]
    for column in numeric_columns:
        source[column] = pd.to_numeric(source[column], errors="coerce")

    # Keep only assets that are actually represented on both chains.
    chain_counts = source.groupby("symbol")["chain"].nunique()
    valid_symbols = set(chain_counts[chain_counts >= 2].index)
    missing_symbols = [symbol for symbol in symbols if symbol not in valid_symbols]
    for symbol in missing_symbols:
        print(f"[skip-asset] combined-chain heatmap: {symbol} is not available on both chains")
    source = source[source["symbol"].isin(valid_symbols)].copy()
    if source.empty:
        return pd.DataFrame()

    def _sum_with_nan(series: pd.Series) -> float:
        return series.sum(min_count=1)

    combined = (
        source.groupby(["month", "symbol"], as_index=False)
        .agg({column: _sum_with_nan for column in numeric_columns})
        .sort_values(["symbol", "month"])
    )

    # Recalculate ratios from pooled numerators/denominators. Do NOT average the
    # Ethereum and BSC ratio columns.
    combined["average_transfer_size"] = combined[
        "genius_qualifying_transfer_volume"
    ].div(combined["transfer_count"].where(combined["transfer_count"] > 0))
    combined["volume_per_active_address"] = combined["token_volume"].div(
        combined["active_addresses"].where(combined["active_addresses"] > 0)
    )
    combined["transfers_per_active_address"] = combined["transfer_count"].div(
        combined["active_addresses"].where(combined["active_addresses"] > 0)
    )
    return combined


def plot_cross_chain_combined_pre_post(datasets: list[dict], cfg: dict) -> None:
    """Plot the seven pooled Ethereum+BSC pre/post-GENIUS metric changes."""
    symbols = [
        str(s).upper()
        for s in cfg.get(
            "combined_metric_assets",
            cfg.get(
                "combined_volume_assets",
                cfg.get("common_stablecoins", ["USDT", "USDC", "DAI", "USDE", "USD1"]),
            ),
        )
    ]
    if not symbols:
        return

    combined = build_cross_chain_combined_monthly_metrics(datasets, cfg, symbols)
    if combined.empty:
        return

    # Export the pooled monthly series so every heatmap cell is auditable.
    _write_analysis_csv(
        combined,
        cfg,
        "genius_cross_chain_combined_monthly_metrics.csv",
    )

    pre_start = pd.to_datetime(cfg.get("genius_pre_start", "2025-01-01"))
    pre_end = pd.to_datetime(cfg.get("genius_pre_end", "2025-07-01"))
    post_start = pd.to_datetime(cfg.get("genius_post_start", "2025-08-01"))

    rows: list[dict[str, object]] = []
    level_rows: list[dict[str, object]] = []
    for symbol in symbols:
        part = combined[combined["symbol"] == symbol]
        if part.empty:
            continue

        row: dict[str, object] = {"symbol": symbol}
        for metric, metric_label in GENIUS_METRICS.items():
            metric_post_end = _genius_post_end_for_metric(cfg, metric)
            pre = part[
                (part["month"] >= pre_start) & (part["month"] < pre_end)
            ][metric].dropna()
            post = part[
                (part["month"] >= post_start) & (part["month"] < metric_post_end)
            ][metric].dropna()

            pre_mean = float(pre.mean()) if not pre.empty else math.nan
            post_mean = float(post.mean()) if not post.empty else math.nan
            pct_change = (
                ((post_mean / pre_mean) - 1.0) * 100.0
                if math.isfinite(pre_mean)
                and pre_mean != 0
                and math.isfinite(post_mean)
                else math.nan
            )

            row[metric] = pct_change
            row[f"{metric}_pre_mean"] = pre_mean
            row[f"{metric}_post_mean"] = post_mean
            row[f"{metric}_pre_months"] = len(pre)
            row[f"{metric}_post_months"] = len(post)
            level_rows.append({
                "symbol": symbol,
                "metric": metric,
                "metric_label": metric_label,
                "pre_mean": pre_mean,
                "post_mean": post_mean,
                "pct_change": pct_change,
                "pre_months": len(pre),
                "post_months": len(post),
                "post_period": _genius_post_period_label(cfg, metric),
                "address_counts_are_cross_chain_deduplicated": False,
            })
        rows.append(row)

    changes = pd.DataFrame(rows)
    if changes.empty:
        return

    _write_analysis_csv(
        changes,
        cfg,
        "genius_cross_chain_combined_pre_post_metric_changes.csv",
    )
    if level_rows:
        _write_analysis_csv(
            pd.DataFrame(level_rows),
            cfg,
            "genius_cross_chain_combined_pre_post_monthly_average_levels.csv",
        )

    metric_keys = list(GENIUS_METRICS.keys())
    z = changes[metric_keys].to_numpy(dtype=float)
    finite = pd.Series(z.flatten()).dropna().abs()
    configured_limit = cfg.get("combined_color_limit")
    if configured_limit is not None:
        color_limit = float(configured_limit)
    else:
        color_limit = max(
            50.0,
            min(300.0, float(finite.quantile(0.90)) if not finite.empty else 100.0),
        )

    text_values = [
        ["" if pd.isna(value) else f"{value:+.0f}%" for value in row]
        for row in z
    ]
    fig = go.Figure(go.Heatmap(
        z=z,
        x=[GENIUS_METRICS[key] for key in metric_keys],
        y=changes["symbol"].tolist(),
        zmid=0,
        zmin=-color_limit,
        zmax=color_limit,
        colorscale="RdBu",
        zsmooth=False,
        xgap=1,
        ygap=1,
        text=text_values,
        texttemplate="%{text}",
        colorbar=dict(title="Change"),
        hovertemplate=(
            "%{y}<br>%{x}: %{z:+.1f}%"
            "<br>Ethereum and BSC pooled before pre/post averaging<extra></extra>"
        ),
    ))
    fig.update_layout(
        title="Ethereum + BNB Smart Chain: combined pre/post-GENIUS metric changes",
        xaxis=dict(title="Metric", tickangle=-25),
        yaxis=dict(title="Stablecoin", autorange="reversed"),
        width=int(cfg.get("combined_width", 1300)),
        height=int(cfg.get("combined_height", max(600, 62 * len(changes) + 260))),
        margin=dict(l=105, r=105, t=115, b=125),
    )
    save_fig(
        fig,
        resolve_project_path(cfg["output_folder"])
        / "genius_cross_chain_combined_pre_post_metric_changes.html",
        cfg,
    )

def _cross_chain_alias_datasets(datasets: list[dict], cfg: dict) -> list[dict]:
    """Add display aliases for cross-chain comparable assets.

    Example: Ethereum WBTC and BSC BTCB can both be exposed as BTC for the
    cross-chain comparison while still reading their original CSV prefixes.
    """
    result = list(datasets)
    aliases = cfg.get("asset_aliases") or []
    for alias in aliases:
        if not isinstance(alias, dict) or not alias.get("label"):
            continue
        label = str(alias["label"]).upper()
        for chain in ("ethereum", "bsc"):
            source = alias.get(chain)
            if not source:
                continue
            source = str(source).upper()
            match = next((
                d for d in datasets
                if str(d.get("chain", "")).lower() == chain
                and str(d.get("transfer_filter", "all")) == "all"
                and str(d.get("token_symbol", "")).upper() == source
            ), None)
            if match is None:
                print(f"[alias-skip] {label}: {source} not found on {chain}")
                continue
            cloned = dict(match)
            cloned["token_symbol"] = label
            # Keep file_prefix/input paths untouched so the alias reads the
            # original WBTC/BTCB aggregates.
            result.append(cloned)
    return result


def run_cross_chain_analysis(datasets: list[dict], cfg: dict) -> None:
    print("\n=== Ethereum / BSC comparison ===")
    resolve_project_path(cfg["output_folder"]).mkdir(parents=True, exist_ok=True)
    comparison_datasets = _cross_chain_alias_datasets(datasets, cfg)

    if bool(cfg.get("divergence_only", False)):
        plot_cross_chain_genius_divergence(comparison_datasets, cfg)
        return

    if bool(cfg.get("combined_only", False)):
        plot_cross_chain_combined_pre_post(comparison_datasets, cfg)
        return

    plot_cross_chain_genius_normalized(comparison_datasets, cfg)
    plot_cross_chain_genius_pre_post(comparison_datasets, cfg)
    plot_cross_chain_combined_pre_post(comparison_datasets, cfg)



# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_plot_pipeline(config_path: Path = CONFIG_PATH) -> None:
    cfg = load_config(config_path)
    global_cfg = cfg.get("global", {})
    datasets = list(cfg.get("datasets", []) or [])
    datasets.extend(expand_dataset_matrices(cfg))
    if not datasets:
        raise ValueError("No datasets defined in the selected plot config")

    run_mode = str(cfg.get("run_mode", "full")).strip().lower()

    # Dedicated thesis helper: build only the cross-chain divergence heatmap.
    # No per-dataset plots, cross-token analysis, normalized series, CSV audit
    # tables, or combined heatmaps are produced in this mode.
    if run_mode == "cross_chain_divergence_only":
        print(f"[config] divergence-only mode with {len(datasets)} configured datasets")
        merged_datasets = [{**global_cfg, **dataset} for dataset in datasets]
        ccfg = _cross_chain_cfg(global_cfg, cfg)
        if ccfg is None:
            raise ValueError(
                "cross_chain_analysis must be enabled for cross_chain_divergence_only mode"
            )
        ccfg["divergence_only"] = True
        run_cross_chain_analysis(merged_datasets, ccfg)
        return

    if run_mode == "cross_chain_combined_only":
        print(f"[config] combined-only mode with {len(datasets)} configured datasets")
        merged_datasets = [{**global_cfg, **dataset} for dataset in datasets]
        ccfg = _cross_chain_cfg(global_cfg, cfg)
        if ccfg is None:
            raise ValueError(
                "cross_chain_analysis must be enabled for cross_chain_combined_only mode"
            )
        ccfg["combined_only"] = True
        run_cross_chain_analysis(merged_datasets, ccfg)
        return

    print(f"[config] plotting {len(datasets)} configured datasets")
    merged_datasets: list[dict] = []
    for dataset in datasets:
        dataset_cfg = {**global_cfg, **dataset}
        merged_datasets.append(dataset_cfg)
        plot_dataset(dataset_cfg)

    analysis = cfg.get("analysis", {}) or {}
    if analysis.get("enabled", True):
        for chain in (analysis.get("chains") or {}):
            chain_cfg = analysis_cfg_for_chain(global_cfg, cfg, str(chain))
            if chain_cfg is None:
                continue
            chain_datasets = _datasets_for_chain(merged_datasets, str(chain))
            if chain_datasets:
                run_cross_token_analysis(chain_datasets, chain_cfg)

    ccfg = _cross_chain_cfg(global_cfg, cfg)
    if ccfg is not None:
        run_cross_chain_analysis(merged_datasets, ccfg)



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate stablecoin thesis plots from aggregate CSV exports.")
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help="Path to a plot config YAML file (default: config/plot_config.yaml).",
    )
    args = parser.parse_args()
    config_path = args.config if args.config.is_absolute() else PROJECT_ROOT / args.config
    print(f"[config] using {config_path}")
    run_plot_pipeline(config_path)
