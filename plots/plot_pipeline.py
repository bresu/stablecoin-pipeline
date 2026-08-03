from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import yaml


# Project paths based on your folder structure
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "plot_config.yaml"


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_project_path(path_str: str) -> Path:
    """
    Allows paths in YAML like:
      output/usdt_all
      plots/plot_output/usdt_all
    and resolves them relative to project root.
    """
    path = Path(path_str)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def filter_period(
    df: pd.DataFrame,
    start_date: str | None,
    end_date: str | None,
) -> pd.DataFrame:
    if "month" not in df.columns:
        return df

    df = df.copy()
    df["month"] = pd.to_datetime(df["month"])

    if start_date:
        df = df[df["month"] >= pd.to_datetime(start_date)]

    if end_date:
        df = df[df["month"] < pd.to_datetime(end_date)]

    return df.sort_values("month")


def find_file(folder: Path, suffix: str) -> Path | None:
    matches = list(folder.glob(f"*{suffix}.csv"))
    return matches[0] if matches else None


def save_fig(fig: go.Figure, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(output_path)
    print(f"[plot] {output_path}")


def plot_monthly_activity(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    file = find_file(input_folder, "monthly_activity")
    if file is None:
        print("[skip] monthly_activity file not found")
        return

    df = pd.read_csv(file)
    df = filter_period(df, cfg.get("start_date"), cfg.get("end_date"))

    volume_col = cfg.get("volume_column", "token_volume")
    primary = cfg.get("primary_color", "#009393")
    secondary = cfg.get("secondary_color", "orange")

    fig = go.Figure()

    fig.add_trace(go.Bar(
        x=df["month"],
        y=df[volume_col],
        name=f"{cfg['token_symbol']} Volume",
        marker_color=primary
    ))

    fig.add_trace(go.Scatter(
        x=df["month"],
        y=df["transfer_count"],
        name="Transfer Count",
        mode="lines",
        yaxis="y2",
        line=dict(color=secondary)
    ))

    fig.update_layout(
        title=f"{cfg['title_prefix']}: Monthly Transfer Activity",
        xaxis=dict(title="Month"),
        yaxis=dict(title=f"{cfg['token_symbol']} Volume"),
        yaxis2=dict(
            title="Transfer Count",
            overlaying="y",
            side="right"
        ),
        hovermode="x unified"
    )

    save_fig(fig, output_folder / "monthly_activity.html")


def plot_monthly_users(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    file = find_file(input_folder, "monthly_activity")
    if file is None:
        print("[skip] monthly_users source file not found")
        return

    df = pd.read_csv(file)
    df = filter_period(df, cfg.get("start_date"), cfg.get("end_date"))

    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=df["month"],
        y=df["unique_senders"],
        name="Unique Senders",
        mode="lines"
    ))

    fig.add_trace(go.Scatter(
        x=df["month"],
        y=df["unique_receivers"],
        name="Unique Receivers",
        mode="lines"
    ))

    fig.add_trace(go.Scatter(
        x=df["month"],
        y=df["active_addresses"],
        name="Active Addresses",
        mode="lines"
    ))

    fig.update_layout(
        title=f"{cfg['title_prefix']}: Monthly Users",
        xaxis=dict(title="Month"),
        yaxis=dict(title="Addresses"),
        hovermode="x unified"
    )

    save_fig(fig, output_folder / "monthly_users.html")


def plot_adoption(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    file = find_file(input_folder, "monthly_adoption")
    if file is None:
        print("[skip] monthly_adoption file not found")
        return

    df = pd.read_csv(file)
    df = filter_period(df, cfg.get("start_date"), cfg.get("end_date"))

    primary = cfg.get("primary_color", "#009393")
    secondary = cfg.get("secondary_color", "orange")

    df["cumulative_adoption"] = df["newly_adopted_addresses"].cumsum()

    fig = go.Figure()

    fig.add_trace(go.Bar(
        x=df["month"],
        y=df["newly_adopted_addresses"],
        name="New Addresses",
        marker_color=primary
    ))

    fig.add_trace(go.Scatter(
        x=df["month"],
        y=df["cumulative_adoption"],
        name="Cumulative Addresses",
        mode="lines",
        yaxis="y2",
        line=dict(color=secondary)
    ))

    fig.update_layout(
        title=f"{cfg['title_prefix']}: Token Adoption",
        xaxis=dict(title="Month"),
        yaxis=dict(title="New Addresses"),
        yaxis2=dict(
            title="Cumulative Addresses",
            overlaying="y",
            side="right"
        ),
        hovermode="x unified"
    )

    save_fig(fig, output_folder / "monthly_adoption.html")


def plot_transfer_size_histogram(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    file = find_file(input_folder, "transfer_size_histogram_all_time")
    if file is None:
        print("[skip] transfer_size_histogram_all_time file not found")
        return

    df = pd.read_csv(file)

    primary = cfg.get("primary_color", "#009393")

    fig = go.Figure()

    fig.add_trace(go.Bar(
        x=df["size_bucket"],
        y=df["transfer_count"],
        name="Transfer Count",
        marker_color=primary
    ))

    fig.update_layout(
        title=f"{cfg['title_prefix']}: Transfer Size Distribution",
        xaxis=dict(title="Transfer Size Bucket"),
        yaxis=dict(title="Transfer Count", type="log"),
        hovermode="x unified"
    )

    save_fig(fig, output_folder / "transfer_size_histogram_all_time.html")


def plot_monthly_transfer_size_buckets_count(
    input_folder: Path,
    output_folder: Path,
    cfg: dict,
) -> None:
    file = find_file(input_folder, "monthly_transfer_size_buckets")
    if file is None:
        print("[skip] monthly_transfer_size_buckets file not found")
        return

    df = pd.read_csv(file)
    df = filter_period(df, cfg.get("start_date"), cfg.get("end_date"))

    fig = px.area(
        df,
        x="month",
        y="transfer_count",
        color="size_bucket",
        title=f"{cfg['title_prefix']}: Monthly Transfer Size Buckets by Count"
    )

    fig.update_layout(
        xaxis=dict(title="Month"),
        yaxis=dict(title="Transfer Count"),
        hovermode="x unified"
    )

    save_fig(fig, output_folder / "monthly_transfer_size_buckets_count_area.html")


def plot_monthly_transfer_size_buckets_volume(
    input_folder: Path,
    output_folder: Path,
    cfg: dict,
) -> None:
    file = find_file(input_folder, "monthly_transfer_size_buckets")
    if file is None:
        print("[skip] monthly_transfer_size_buckets file not found")
        return

    df = pd.read_csv(file)
    df = filter_period(df, cfg.get("start_date"), cfg.get("end_date"))

    fig = px.area(
        df,
        x="month",
        y="bucket_volume",
        color="size_bucket",
        title=f"{cfg['title_prefix']}: Monthly Transfer Size Buckets by Volume"
    )

    fig.update_layout(
        xaxis=dict(title="Month"),
        yaxis=dict(title=f"{cfg['token_symbol']} Volume"),
        hovermode="x unified"
    )

    save_fig(fig, output_folder / "monthly_transfer_size_buckets_volume_area.html")


def plot_top100_users(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    file = find_file(input_folder, "monthly_top100_users")
    if file is None:
        print("[skip] monthly_top100_users file not found")
        return

    df = pd.read_csv(file)
    df = filter_period(df, cfg.get("start_date"), cfg.get("end_date"))

    primary = cfg.get("primary_color", "#009393")

    if df.empty:
        print("[skip] monthly_top100_users empty after date filter")
        return

    top = (
        df.groupby("address", as_index=False)["outgoing_volume"]
        .sum()
        .sort_values("outgoing_volume", ascending=False)
        .head(20)
    )

    fig = go.Figure()

    fig.add_trace(go.Bar(
        x=top["address"],
        y=top["outgoing_volume"],
        name="Outgoing Volume",
        marker_color=primary
    ))

    fig.update_layout(
        title=f"{cfg['title_prefix']}: Top 20 Users by Total Outgoing Volume",
        xaxis=dict(title="Address"),
        yaxis=dict(title=f"{cfg['token_symbol']} Volume"),
        hovermode="x unified"
    )

    save_fig(fig, output_folder / "top20_users_total_volume.html")


def plot_top100_funded_by(input_folder: Path, output_folder: Path, cfg: dict) -> None:
    file = find_file(input_folder, "monthly_top100_funded_by")
    if file is None:
        print("[skip] monthly_top100_funded_by file not found")
        return

    df = pd.read_csv(file)
    df = filter_period(df, cfg.get("start_date"), cfg.get("end_date"))

    primary = cfg.get("primary_color", "#009393")

    if df.empty:
        print("[skip] monthly_top100_funded_by empty after date filter")
        return

    top = (
        df.groupby("funded_by_address", as_index=False)["newly_funded_addresses"]
        .sum()
        .sort_values("newly_funded_addresses", ascending=False)
        .head(20)
    )

    fig = go.Figure()

    fig.add_trace(go.Bar(
        x=top["funded_by_address"],
        y=top["newly_funded_addresses"],
        name="Newly Funded Addresses",
        marker_color=primary
    ))

    fig.update_layout(
        title=f"{cfg['title_prefix']}: Top 20 Funders by Newly Funded Addresses",
        xaxis=dict(title="Funder Address"),
        yaxis=dict(title="Newly Funded Addresses"),
        hovermode="x unified"
    )

    save_fig(fig, output_folder / "top20_funders_new_addresses.html")


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
    plot_monthly_transfer_size_buckets_count(input_folder, output_folder, dataset_cfg)
    plot_monthly_transfer_size_buckets_volume(input_folder, output_folder, dataset_cfg)
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
