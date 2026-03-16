#!/usr/bin/env python3
"""
Polymarket 15-min Arb Bot — Live Dashboard

Запуск:
    LAUNCH.bat              # двойной клик на Windows
    python dashboard.py     # или напрямую

Клавиши:
    s        — настройки
    q/Ctrl+C — выход
"""

import asyncio
import logging
import msvcrt
import signal
import threading
import time
from pathlib import Path

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bot.config import Config
from bot.main import ArbitrageBot
from bot.state import BotState, bot_state

console = Console()

# ─── Утилиты ──────────────────────────────────────────────────────────────────

_TICKER_MAP = {
    "Bitcoin": "BTC",
    "Ethereum": "ETH",
    "Solana": "SOL",
    "XRP": "XRP",
    "Dogecoin": "DOGE",
    "BNB": "BNB",
    "Hyperliquid": "HYPE",
}


def _short_name(question: str, max_len: int) -> str:
    """Сократить название: 'Bitcoin Up or Down - March 17, 11:30AM ET' -> 'BTC March 17, 11:30AM ET'."""
    name = question
    for full, ticker in _TICKER_MAP.items():
        if full in name:
            name = name.replace(f"{full} Up or Down - ", f"{ticker} ")
            name = name.replace(f"{full} Up or Down", ticker)
            break
    if len(name) > max_len:
        name = name[: max_len - 1] + "…"
    return name


# ─── .env файл ────────────────────────────────────────────────────────────────

_ENV_PATH = Path(__file__).parent / ".env"


def _read_env() -> dict[str, str]:
    result: dict[str, str] = {}
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                v = v.split("#")[0].strip()
                result[k.strip()] = v
    return result


def _write_env(data: dict[str, str]) -> None:
    """Обновить .env — только указанные ключи (остальные строки сохраняются)."""
    lines: list[str] = []
    remaining = dict(data)

    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.partition("=")[0].strip()
                if k in remaining:
                    lines.append(f"{k}={remaining.pop(k)}")
                    continue
            lines.append(line)

    for k, v in remaining.items():
        lines.append(f"{k}={v}")

    _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ─── Первый запуск ────────────────────────────────────────────────────────────

def first_run_wizard() -> None:
    """Показать мастер настройки если .env не существует."""
    if _ENV_PATH.exists():
        return

    console.clear()
    console.rule("[bold cyan]Polymarket Arb Bot — Первая настройка[/bold cyan]")
    console.print()
    console.print("Файл настроек не найден. Пройдём быструю настройку.\n")

    data: dict[str, str] = {}

    # ── Режим ────────────────────────────────────────────────────────────────
    console.print("[bold]Режим работы[/bold]")
    mode = console.input("  Dry-run (симуляция без реальных денег)? [Y/n]: ").strip().lower()
    data["DRY_RUN"] = "false" if mode == "n" else "true"

    if data["DRY_RUN"] == "false":
        console.print()
        console.print("[yellow]Для live-торговли нужны API-ключи Polymarket.[/yellow]")
        data["PRIVATE_KEY"]         = console.input("  Приватный ключ (0x...): ").strip()
        data["WALLET_ADDRESS"]      = console.input("  Адрес кошелька (0x...): ").strip()
        data["POLY_API_KEY"]        = console.input("  Poly API Key: ").strip()
        data["POLY_API_SECRET"]     = console.input("  Poly API Secret: ").strip()
        data["POLY_API_PASSPHRASE"] = console.input("  Poly API Passphrase: ").strip()

    # ── Торговые параметры ───────────────────────────────────────────────────
    console.print()
    console.print("[bold]Торговые параметры[/bold] (Enter = значение по умолчанию)")

    v = console.input("  Макс. позиция USD [50]: ").strip()
    data["MAX_POSITION_SIZE"] = v if v else "50.0"

    v = console.input("  Мин. прибыль % [2.0]: ").strip()
    if v:
        pct = float(v)
        data["MIN_PROFIT_PCT"] = str(pct if pct < 1 else pct / 100)
    else:
        data["MIN_PROFIT_PCT"] = "0.02"

    v = console.input("  Макс. дневной убыток USD [20]: ").strip()
    data["MAX_DAILY_LOSS"] = v if v else "20.0"

    # Остальные дефолты
    data.setdefault("MIN_LIQUIDITY_USD", "50.0")
    data.setdefault("MAX_CONCURRENT_POSITIONS", "5")
    data.setdefault("MIN_SECONDS_TO_EXPIRY", "30")
    data.setdefault("MAX_SECONDS_TO_EXPIRY", "1000")
    data.setdefault("MARKET_REFRESH_INTERVAL", "60")
    data.setdefault("LOG_LEVEL", "INFO")

    _write_env(data)

    console.print()
    console.print("[green]Настройки сохранены в .env[/green]")
    console.print()
    console.input("Нажмите Enter чтобы запустить бота...")


# ─── Экран настроек ───────────────────────────────────────────────────────────

_SETTINGS_FIELDS = [
    # (env_key,                  label,                      type,   hint)
    ("DRY_RUN",                  "Dry Run Mode",             "bool", "true=симуляция / false=live"),
    ("MIN_PROFIT_PCT",           "Мин. прибыль",             "pct",  "например 2.0 = 2%"),
    ("MAX_POSITION_SIZE",        "Макс. позиция",            "usd",  "USD на один рынок"),
    ("MIN_LIQUIDITY_USD",        "Мин. ликвидность",         "usd",  "USD на каждой стороне"),
    ("MAX_DAILY_LOSS",           "Макс. дневной убыток",     "usd",  "USD — бот остановится"),
    ("MAX_CONCURRENT_POSITIONS", "Макс. позиций сразу",      "int",  "штук"),
    ("MIN_SECONDS_TO_EXPIRY",    "Мин. секунд до закрытия",  "sec",  "не входить если < N сек"),
    ("MAX_SECONDS_TO_EXPIRY",    "Макс. секунд до закрытия", "sec",  "1000 = ~16 мин"),
    ("MARKET_REFRESH_INTERVAL",  "Обновление рынков",        "sec",  "каждые N секунд"),
]


def _fmt_value(val: str, typ: str) -> str:
    try:
        if typ == "bool":
            return "[green]ВКЛ (симуляция)[/]" if val.lower() == "true" else "[red bold]ВЫКЛ (live!)[/]"
        if typ == "pct":
            return f"{float(val) * 100:.1f}%"
        if typ == "usd":
            return f"${float(val):.1f}"
        return val
    except Exception:
        return val or "—"


def settings_screen() -> None:
    """Интерактивный экран настроек (запускается вместо Live)."""
    while True:
        env = _read_env()
        console.clear()
        console.rule("[bold cyan]Настройки[/bold cyan]")
        console.print()

        t = Table(show_header=True, header_style="bold cyan", box=box.ROUNDED, padding=(0, 2))
        t.add_column("#", min_width=3, justify="right", style="dim")
        t.add_column("Параметр", min_width=28)
        t.add_column("Значение", min_width=18, justify="right")
        t.add_column("Пояснение", style="dim")

        for i, (key, label, typ, hint) in enumerate(_SETTINGS_FIELDS, 1):
            raw = env.get(key, "")
            t.add_row(str(i), label, _fmt_value(raw, typ), hint)

        console.print(t)
        console.print()
        console.print("[dim]Введите номер для изменения, или Enter чтобы вернуться[/dim]")
        console.print("[dim]Изменения вступят в силу после перезапуска бота[/dim]")
        console.print()

        choice = console.input("[cyan]> [/cyan]").strip()
        if not choice:
            break

        try:
            idx = int(choice) - 1
            if not (0 <= idx < len(_SETTINGS_FIELDS)):
                raise ValueError("out of range")
        except ValueError:
            console.print("[red]Неверный номер[/red]")
            time.sleep(0.8)
            continue

        key, label, typ, hint = _SETTINGS_FIELDS[idx]
        current_raw = env.get(key, "")

        if typ == "bool":
            current_is_true = current_raw.lower() == "true"
            new_val = "false" if current_is_true else "true"
            status = "[green]ВКЛ (симуляция)[/]" if new_val == "true" else "[red bold]ВЫКЛ (live!)[/]"
            console.print(f"  {label}: → {status}")
            env[key] = new_val
            _write_env(env)
            console.print("  [green]Сохранено![/green]")
            time.sleep(0.8)
        else:
            new_str = console.input(
                f"  {label} (сейчас: {_fmt_value(current_raw, typ)},  {hint}): "
            ).strip()
            if not new_str:
                continue
            try:
                if typ == "pct":
                    v = float(new_str)
                    env[key] = str(v if v < 1 else v / 100)
                elif typ == "usd":
                    env[key] = str(float(new_str))
                elif typ in ("int", "sec"):
                    env[key] = str(int(float(new_str)))
                else:
                    env[key] = new_str
                _write_env(env)
                console.print("  [green]Сохранено! Перезапустите бота чтобы применить.[/green]")
                time.sleep(1.0)
            except ValueError:
                console.print("  [red]Неверное значение[/red]")
                time.sleep(0.8)


# ─── Клавиатура (Windows msvcrt) ─────────────────────────────────────────────

class _KeyboardHandler:
    """Фоновый поток читает нажатия клавиш (только Windows)."""

    def __init__(self) -> None:
        self._key: str | None = None
        self._lock = threading.Lock()
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self) -> None:
        try:
            while True:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    try:
                        decoded = ch.decode("utf-8", errors="ignore").lower()
                    except Exception:
                        decoded = ""
                    if decoded:
                        with self._lock:
                            self._key = decoded
                time.sleep(0.05)
        except Exception:
            pass

    def consume(self) -> str | None:
        with self._lock:
            k = self._key
            self._key = None
            return k


# ─── Рендер блоков ────────────────────────────────────────────────────────────

def render_header(state: BotState) -> Panel:
    mode_color = "yellow" if state.mode == "DRY RUN" else "red"
    ws_status = "[green]CONNECTED[/]" if state.ws_connected else "[red]RECONNECTING[/]"

    title = Text()
    title.append("  Polymarket 15-min Arb Bot  ", style="bold white on dark_blue")
    title.append(f"  {state.mode}  ", style=f"bold white on {mode_color}")
    title.append(f"  Uptime: {state.uptime_str}  ", style="dim")
    title.append(f"  WS: {ws_status}  ")
    title.append(f"  Markets: {state.markets_loaded:,}  ", style="cyan")
    title.append(f"  Tokens: {state.tokens_subscribed:,}  ", style="cyan dim")

    status = "[green bold]RUNNING[/]" if state.running else "[red bold]STOPPED[/]"
    title.append(f"  {status}  ")

    return Panel(title, box=box.HORIZONTALS, padding=(0, 1))


def render_stats(state: BotState) -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", min_width=18)
    t.add_column(style="bold")

    t.add_row("Price updates:", f"[cyan]{state.price_updates:,}[/]")
    t.add_row("Analyzed:", f"{state.analyzed:,}")
    t.add_row("Opportunities:", f"[yellow]{state.opportunities_found:,}[/]")
    t.add_row("Trades executed:", f"[bold]{state.trades_executed}[/]")
    t.add_row("Win rate:", f"[green]{state.win_rate:.1f}%[/]" if state.win_rate > 0 else "—")
    t.add_row("")
    t.add_row(
        "Daily PnL:",
        (f"[green]+${state.daily_pnl:.2f}[/]" if state.daily_pnl >= 0
         else f"[red]-${abs(state.daily_pnl):.2f}[/]")
        + (" [dim](sim)[/]" if state.mode == "DRY RUN" else ""),
    )
    t.add_row(
        "Total PnL:",
        (f"[green]+${state.total_pnl:.2f}[/]" if state.total_pnl >= 0
         else f"[red]-${abs(state.total_pnl):.2f}[/]")
        + (" [dim](sim)[/]" if state.mode == "DRY RUN" else ""),
    )
    t.add_row("")
    # Near-miss трекер
    best = state.best_comb_today
    if best < 9.0:
        best_color = "green bold" if best < 1.0 else ("yellow" if best < 1.005 else "dim")
        best_str = f"[{best_color}]{best:.4f}[/]"
        if state.best_comb_market:
            short = _short_name(state.best_comb_market, 16)
            best_str += f" [dim]{short}[/]"
    else:
        best_str = "[dim]—[/]"
    t.add_row("Best Comb today:", best_str)
    nm_color = "yellow" if state.near_misses > 0 else "dim"
    t.add_row("Near misses:", f"[{nm_color}]{state.near_misses:,}[/] [dim](<1.005)[/]")
    t.add_row("")
    t.add_row("Min profit:", f"{state.min_profit_pct * 100:.1f}%")
    t.add_row("Max position:", f"${state.max_position_size:.0f}")
    t.add_row("WS last msg:", f"{state.ws_last_msg_sec:.0f}s ago")

    return Panel(t, title="[bold]Statistics[/]", box=box.ROUNDED, border_style="blue")


def render_markets(state: BotState) -> Panel:
    t = Table(
        show_header=True,
        header_style="bold cyan",
        box=box.SIMPLE,
        padding=(0, 1),
        expand=True,
    )
    t.add_column("Market", ratio=4)
    t.add_column("YES", justify="right", min_width=6)
    t.add_column("NO", justify="right", min_width=6)
    t.add_column("Comb", justify="right", min_width=6)
    t.add_column("Profit", justify="right", min_width=7)
    t.add_column("Left", justify="right", min_width=5)

    markets = state.get_top_opportunities(n=12)

    if not markets:
        t.add_row("[dim]Нет активных рынков в окне...[/]", "", "", "", "", "")
        subtitle = "[dim]Торговые часы: 11:20AM – 3:50PM ET[/]"
    else:
        subtitle = f"[dim]Топ {len(markets)} рынков по спреду[/]"

    for m in markets:
        combined = m.combined or 0
        profit_pct = m.profit_pct or 0
        seconds_left = m.seconds_left or 0

        if profit_pct > state.min_profit_pct:
            comb_style = "bold green"
            profit_style = "bold green"
        elif combined < 1.0:
            comb_style = "yellow"
            profit_style = "yellow"
        else:
            comb_style = "dim red"
            profit_style = "dim"

        q = _short_name(m.question, 38)

        t.add_row(
            q,
            f"{m.yes_ask:.3f}" if m.yes_ask else "—",
            f"{m.no_ask:.3f}" if m.no_ask else "—",
            f"[{comb_style}]{combined:.4f}[/]",
            f"[{profit_style}]{profit_pct * 100:+.2f}%[/]" if profit_pct else "—",
            f"{seconds_left:.0f}s" if seconds_left else "—",
        )

    return Panel(
        t,
        title="[bold]Active Markets (in window)[/]",
        subtitle=subtitle,
        box=box.ROUNDED,
        border_style="green",
    )


def render_trades(state: BotState) -> Panel:
    t = Table(
        show_header=True,
        header_style="bold magenta",
        box=box.SIMPLE,
        padding=(0, 1),
        expand=True,
    )
    t.add_column("Time", min_width=8)
    t.add_column("Market", ratio=3)
    t.add_column("Comb", justify="right", min_width=6)
    t.add_column("Profit", justify="right", min_width=8)
    t.add_column("", min_width=4)

    if not state.recent_trades:
        t.add_row("[dim]Сделок пока нет...[/]", "", "", "", "")
    else:
        for trade in state.recent_trades[:10]:
            ts = trade.timestamp.strftime("%H:%M:%S")
            sign = "[green]+[/]" if trade.profit_usd >= 0 else "[red]-[/]"
            pnl = f"{sign}${abs(trade.profit_usd):.2f}"
            sim = "[dim](sim)[/]" if trade.dry_run else "[yellow](live)[/]"
            q = _short_name(trade.market, 30)
            t.add_row(f"[dim]{ts}[/]", q, f"{trade.combined:.4f}", pnl, sim)

    return Panel(t, title="[bold]Recent Trades[/]", box=box.ROUNDED, border_style="magenta")


def render_help() -> Panel:
    return Panel(
        "[dim]  q / Ctrl+C = выход  |  s = настройки  |  "
        "Обновление: 1 сек  |  "
        "Торговые часы: 11:20AM – 3:50PM ET (New York)[/dim]",
        box=box.HORIZONTALS,
        padding=(0, 1),
    )


def build_layout(state: BotState) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(render_header(state), name="header", size=3),
        Layout(name="body"),
        Layout(render_help(), name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="left", ratio=1),
        Layout(name="right", ratio=2),
    )
    layout["left"].update(render_stats(state))
    layout["right"].split_column(
        Layout(render_markets(state), name="markets", ratio=3),
        Layout(render_trades(state), name="trades", ratio=2),
    )
    return layout


# ─── Основной цикл ────────────────────────────────────────────────────────────

async def run_dashboard(config: Config, state: BotState) -> None:
    bot = ArbitrageBot(config, state)

    # Логи → файл, не в терминал (чтобы не мешать Rich)
    log_file = open("bot.log", "a", encoding="utf-8")
    file_handler = logging.StreamHandler(log_file)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    ))
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.setLevel(logging.INFO)

    bot_task = asyncio.create_task(bot.start())
    keyboard = _KeyboardHandler()
    stop_requested = False

    with Live(
        build_layout(state),
        console=console,
        refresh_per_second=1,
        screen=True,
    ) as live:
        try:
            while not bot_task.done() and not stop_requested:
                key = keyboard.consume()

                if key in ("q", "\x03"):   # q или Ctrl+C
                    stop_requested = True
                    break
                elif key == "s":
                    live.stop()
                    settings_screen()
                    live.start()

                live.update(build_layout(state))
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            console.print("\n[yellow]Stopping bot...[/]")
            await bot.stop()
            if not bot_task.done():
                bot_task.cancel()
                try:
                    await bot_task
                except asyncio.CancelledError:
                    pass
            log_file.close()

    # Финальный отчёт
    console.print()
    console.rule("[bold]Final Report[/]")
    console.print(f"  Mode:    [{'yellow' if state.mode == 'DRY RUN' else 'red'}]{state.mode}[/]")
    console.print(f"  Uptime:  {state.uptime_str}")
    console.print(f"  Trades:  {state.trades_executed}  (win rate: {state.win_rate:.1f}%)")
    pnl_color = "green" if state.total_pnl >= 0 else "red"
    sim_note = " (simulated)" if state.mode == "DRY RUN" else ""
    console.print(f"  PnL:     [{pnl_color}]${state.total_pnl:+.2f}[/]{sim_note}")
    console.print(f"  Log:     bot.log")
    console.rule()


def main() -> None:
    first_run_wizard()         # мастер настройки если нет .env
    config = Config.from_env()
    state = bot_state

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown(sig):
        console.print(f"\n[yellow]Received {sig.name}, shutting down...[/]")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown, sig)
        except NotImplementedError:
            pass  # Windows

    try:
        loop.run_until_complete(run_dashboard(config, state))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
