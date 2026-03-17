#!/usr/bin/env python3
"""
Polymarket Bot — Strategy Performance Analyzer

Reads data/signals.csv, data/orders.csv, data/resolutions.csv
and prints a structured post-analysis report to the terminal.

Usage:
    python analyze_results.py
    python analyze_results.py --mode PAPER
    python analyze_results.py --mode LIVE
"""

import argparse
import sys
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    print("pandas not installed. Run: pip install pandas")
    sys.exit(1)

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

DATA_DIR = Path(__file__).parent / "data"
console = Console()


# ─── Loading ──────────────────────────────────────────────────────────────────

def _load(name: str) -> pd.DataFrame:
    path = DATA_DIR / name
    if not path.exists():
        console.print(f"[yellow]⚠  {name}: file not found[/]")
        return pd.DataFrame()
    df = pd.read_csv(path)
    if len(df) == 0:
        console.print(f"[yellow]⚠  {name}: no rows yet (file exists but empty)[/]")
    return df


# ─── Formatting helpers ───────────────────────────────────────────────────────

def _pct(num: float, denom: float) -> str:
    if not denom:
        return "—"
    return f"{num / denom * 100:.1f}%"


def _money(val: float) -> str:
    color = "green" if val >= 0 else "red"
    sign  = "+" if val >= 0 else ""
    return f"[{color}]{sign}${val:.2f}[/]"


def _int(val) -> str:
    return str(int(val)) if pd.notna(val) else "—"


# ─── Master dataframe ─────────────────────────────────────────────────────────

def build_master(
    sig:  pd.DataFrame,
    ord_: pd.DataFrame,
    res:  pd.DataFrame,
) -> pd.DataFrame:
    """
    Left-join: signals (base) + orders + resolutions, all on signal_id.

    After join:
    - Every signal has its own row.
    - Order columns are NaN for risk_skip signals (no order was submitted).
    - Resolution columns are NaN for pending/unresolved fills.

    This preserves funnel integrity — no rows are lost or duplicated.
    """
    if sig.empty:
        return pd.DataFrame()

    m = sig.copy()

    # Columns to take from orders (avoid colliding with signal columns)
    if not ord_.empty:
        ord_cols = ["signal_id", "order_status", "filled_size", "unfilled_size",
                    "avg_fill_price", "limit_price", "cancel_reason",
                    "placed_at", "timestamp", "executor_type"]
        ord_cols = [c for c in ord_cols if c in ord_.columns]
        m = m.merge(
            ord_[ord_cols].rename(columns={"timestamp": "order_ts"}),
            on="signal_id", how="left",
        )

    # Columns to take from resolutions
    if not res.empty:
        res_cols = ["signal_id", "outcome", "winner_side", "profit_usd", "seconds_held"]
        res_cols = [c for c in res_cols if c in res.columns]
        m = m.merge(res[res_cols], on="signal_id", how="left")

    # ── Derived bucketing columns ──────────────────────────────────────────────
    if "fav_price" in m.columns:
        m["fav_bucket"] = pd.cut(
            pd.to_numeric(m["fav_price"], errors="coerce"),
            bins=[0.84, 0.88, 0.91, 0.94, 1.01],
            labels=["0.85–0.88", "0.88–0.91", "0.91–0.94", "0.94+"],
        )

    if "seconds_left" in m.columns:
        m["sec_bucket"] = pd.cut(
            pd.to_numeric(m["seconds_left"], errors="coerce"),
            bins=[-1, 15, 30, 60, 120, 9_999],
            labels=["0–15s", "15–30s", "30–60s", "60–120s", "120s+"],
        )

    if "placed_at" in m.columns and "order_ts" in m.columns:
        m["fill_latency_s"] = (
            pd.to_datetime(m["order_ts"], errors="coerce", utc=True) -
            pd.to_datetime(m["placed_at"], errors="coerce", utc=True)
        ).dt.total_seconds()

    return m


def _filled(m: pd.DataFrame) -> pd.DataFrame:
    """Rows where the order was actually filled (filled_size > 0)."""
    if "order_status" not in m.columns:
        return m.iloc[0:0]
    return m[m["order_status"].isin(["FILLED", "PARTIAL"])]


def _resolved(m: pd.DataFrame) -> pd.DataFrame:
    """Rows with a confirmed WIN or LOSS outcome."""
    if "outcome" not in m.columns:
        return m.iloc[0:0]
    return m[m["outcome"].isin(["WIN", "LOSS"])]


# ─── Standard group aggregation ───────────────────────────────────────────────

def _agg(grp: pd.DataFrame) -> dict:
    sigs   = int((grp["status"] == "submitted").sum()) if "status" in grp.columns else len(grp)
    fills  = int(grp["order_status"].isin(["FILLED", "PARTIAL"]).sum()) if "order_status" in grp.columns else 0
    wins   = int((grp["outcome"] == "WIN").sum())   if "outcome" in grp.columns else 0
    losses = int((grp["outcome"] == "LOSS").sum())  if "outcome" in grp.columns else 0
    pnl    = float(grp["profit_usd"].fillna(0).sum()) if "profit_usd" in grp.columns else 0.0
    return {"sigs": sigs, "fills": fills, "wins": wins, "losses": losses, "pnl": pnl}


# ─── Section printers ─────────────────────────────────────────────────────────

def print_funnel(sig: pd.DataFrame, ord_: pd.DataFrame) -> None:
    total     = len(sig)
    risk_skip = int((sig["status"] == "risk_skip").sum()) if "status" in sig.columns else 0
    submitted = int((sig["status"] == "submitted").sum())  if "status" in sig.columns else 0

    filled = partial = cancelled = failed = 0
    if not ord_.empty and "order_status" in ord_.columns:
        filled    = int((ord_["order_status"] == "FILLED").sum())
        partial   = int((ord_["order_status"] == "PARTIAL").sum())
        cancelled = int((ord_["order_status"] == "CANCELLED").sum())
        failed    = int((ord_["order_status"] == "FAILED").sum())

    t = Table("Step", "Count", box=box.SIMPLE, padding=(0, 2),
              header_style="bold cyan", show_header=True)
    t.add_row("Signals detected",   str(total))
    t.add_row("  ↳ risk_skip",      f"[dim]{risk_skip}[/]")
    t.add_row("  ↳ submitted",      str(submitted))
    t.add_row("    ↳ FILLED",       f"[green]{filled}[/]")
    t.add_row("    ↳ PARTIAL",      f"[yellow]{partial}[/]")
    t.add_row("    ↳ CANCELLED",    f"[dim]{cancelled}[/]")
    t.add_row("    ↳ FAILED",       f"[red]{failed}[/]")
    t.add_row("", "")
    t.add_row(
        "Fill rate  (FILLED+PARTIAL / submitted)",
        _pct(filled + partial, submitted),
    )
    t.add_row(
        "Skip rate  (risk_skip / detected)",
        _pct(risk_skip, total),
    )

    console.print(Panel(t, title="[bold]1. Signal Funnel[/]", box=box.ROUNDED, border_style="blue"))


def print_outcomes(m: pd.DataFrame) -> None:
    fil  = _filled(m)
    res  = _resolved(m)

    wins   = int((res["outcome"] == "WIN").sum())   if not res.empty else 0
    losses = int((res["outcome"] == "LOSS").sum())  if not res.empty else 0
    pnl    = float(res["profit_usd"].sum())          if not res.empty else 0.0
    avg_p  = float(res["profit_usd"].mean())         if not res.empty else 0.0
    avg_e  = float(fil["avg_fill_price"].mean())     if not fil.empty else 0.0
    pending = len(fil) - (wins + losses)

    lat_str = "—"
    if not fil.empty and "fill_latency_s" in fil.columns:
        lat = fil["fill_latency_s"].dropna()
        if len(lat):
            lat_str = f"{lat.mean():.1f}s  (median {lat.median():.1f}s)"

    t = Table("Metric", "Value", box=box.SIMPLE, padding=(0, 2),
              header_style="bold cyan", show_header=True)
    t.add_row("Resolved trades",    str(wins + losses))
    t.add_row("  Wins",             f"[green]{wins}[/]")
    t.add_row("  Losses",           f"[red]{losses}[/]")
    t.add_row("Win rate",           _pct(wins, wins + losses))
    t.add_row("Total PnL",          _money(pnl)   if (wins + losses) else "—")
    t.add_row("Avg profit / trade", _money(avg_p) if (wins + losses) else "—")
    t.add_row("Avg entry price",    f"${avg_e:.4f}" if avg_e else "—")
    t.add_row("Avg fill latency",   lat_str)
    if pending > 0:
        t.add_row("Pending (no resolution yet)", f"[yellow]{pending}[/]")

    console.print(Panel(t, title="[bold]2. Trade Outcomes[/]", box=box.ROUNDED, border_style="green"))


def print_breakdown(
    m:         pd.DataFrame,
    group_col: str,
    title:     str,
    border:    str = "cyan",
) -> None:
    t = Table(box=box.SIMPLE, show_header=True, header_style="bold dim", padding=(0, 1))
    label = group_col.replace("_bucket", "").replace("_", " ").title()
    t.add_column(label,    min_width=14)
    t.add_column("Sigs",   justify="right", min_width=5)
    t.add_column("Fills",  justify="right", min_width=5)
    t.add_column("Fill%",  justify="right", min_width=7)
    t.add_column("Wins",   justify="right", min_width=5)
    t.add_column("Loss",   justify="right", min_width=5)
    t.add_column("Win%",   justify="right", min_width=7)
    t.add_column("PnL",    justify="right", min_width=10)

    if m.empty or group_col not in m.columns:
        t.add_row("[dim]no data[/]", *["—"] * 7)
        console.print(Panel(t, title=f"[bold]{title}[/]", box=box.ROUNDED, border_style=border))
        return

    any_data = False
    for name, grp in m.groupby(group_col, observed=True):
        a = _agg(grp)
        if a["sigs"] == 0:
            continue
        any_data = True
        pnl_str = _money(a["pnl"]) if (a["wins"] + a["losses"]) > 0 else "[dim]—[/]"
        t.add_row(
            str(name),
            str(a["sigs"]),
            str(a["fills"]),
            _pct(a["fills"], a["sigs"]),
            f"[green]{a['wins']}[/]"  if a["wins"]   else "[dim]0[/]",
            f"[red]{a['losses']}[/]"  if a["losses"] else "[dim]0[/]",
            _pct(a["wins"], a["wins"] + a["losses"]),
            pnl_str,
        )

    if not any_data:
        t.add_row("[dim]no submitted signals[/]", *["—"] * 7)

    console.print(Panel(t, title=f"[bold]{title}[/]", box=box.ROUNDED, border_style=border))


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze bot strategy results")
    parser.add_argument(
        "--mode",
        help="Filter by mode: PAPER, LIVE, DRY_RUN  (default: all)",
        default=None,
    )
    args = parser.parse_args()

    console.print()
    console.print(Rule("[bold cyan]Polymarket Bot — Strategy Analysis[/]"))
    console.print()

    sig  = _load("signals.csv")
    ord_ = _load("orders.csv")
    res  = _load("resolutions.csv")
    console.print()

    if sig.empty:
        console.print("[red]No signal data found. Run the bot first.[/]")
        return

    # ── Optional mode filter ──────────────────────────────────────────────────
    if args.mode:
        norm = args.mode.upper().replace("-", "_")
        before = len(sig)
        sig  = sig[sig["mode"].str.upper().str.replace(" ", "_") == norm]  if "mode" in sig.columns  else sig
        ord_ = ord_[ord_["mode"].str.upper().str.replace(" ", "_") == norm] if not ord_.empty and "mode" in ord_.columns else ord_
        res  = res[res["mode"].str.upper().str.replace(" ", "_") == norm]  if not res.empty  and "mode" in res.columns  else res
        console.print(f"  [dim]Mode filter:[/] [cyan]{args.mode}[/]  ({len(sig)} of {before} signals)")
        if sig.empty:
            console.print(f"[red]No signals found for mode={args.mode}[/]")
            return
        console.print()

    # ── Data range info ───────────────────────────────────────────────────────
    if "timestamp" in sig.columns:
        ts = pd.to_datetime(sig["timestamp"], errors="coerce", utc=True)
        modes_str = ", ".join(sig["mode"].unique()) if "mode" in sig.columns else "—"
        console.print(f"  Range:  [cyan]{str(ts.min())[:19]}[/] → [cyan]{str(ts.max())[:19]}[/] UTC")
        console.print(f"  Modes:  [cyan]{modes_str}[/]")
        console.print()

    m = build_master(sig, ord_, res)

    print_funnel(sig, ord_)
    print_outcomes(m)
    print_breakdown(m, "asset",      "3. By Asset",              border="cyan")
    print_breakdown(m, "fav_bucket", "4. By Fav Price Bucket",   border="yellow")
    print_breakdown(m, "sec_bucket", "5. By Seconds Left",       border="magenta")
    print_breakdown(m, "mode",       "6. By Mode",               border="blue")

    console.print(Rule("[dim]End of report[/]"))
    console.print()


if __name__ == "__main__":
    main()
