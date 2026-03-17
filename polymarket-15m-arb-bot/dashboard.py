#!/usr/bin/env python3
"""
Polymarket 15-min Arb Bot — Live Dashboard

Запуск:
    LAUNCH.bat              # двойной клик на Windows
    python dashboard.py     # или напрямую

Клавиши:
    s        — настройки
    o        — сгенерировать Polymarket API ключи
    q/Ctrl+C — выход
"""

import asyncio
import logging
import msvcrt
import signal
import threading
import time
from datetime import timezone as _tz
from pathlib import Path
from zoneinfo import ZoneInfo

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
    console.print("  1 = dry   — симуляция (нет реальных ордеров, нет денег)")
    console.print("  2 = paper — виртуальные ордера по реальным ценам")
    console.print("  3 = live  — реальная торговля (нужны API-ключи)")
    mode_choice = console.input("  Выберите режим [1/2/3], по умолчанию 1: ").strip()
    bot_mode = {"1": "dry", "2": "paper", "3": "live"}.get(mode_choice, "dry")
    data["BOT_MODE"] = bot_mode

    if bot_mode == "live":
        console.print()
        console.print("[yellow]Для live-торговли нужны API-ключи Polymarket.[/yellow]")
        data["PRIVATE_KEY"]          = console.input("  Приватный ключ (0x...): ").strip()
        data["WALLET_ADDRESS"]       = console.input("  Адрес кошелька (0x...): ").strip()
        data["POLY_API_KEY"]         = console.input("  Poly API Key: ").strip()
        data["POLY_API_SECRET"]      = console.input("  Poly API Secret: ").strip()
        data["POLY_API_PASSPHRASE"]  = console.input("  Poly API Passphrase: ").strip()
        data["LIVE_TRADING_ENABLED"] = "true"

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
    # (env_key,                  label,                        type,     hint)
    ("BOT_MODE",                 "Режим бота",                 "mode",   "dry · paper · live  [Enter = следующий]"),
    ("DATA_SOURCE",              "Источник данных",            "source", "direct · monitoring · auto  [Enter = следующий]"),
    ("MAX_POSITION_SIZE",        "Макс. позиция",              "usd",    "USD на один рынок"),
    ("MIN_FAVORITE_PRICE",       "Мин. цена фаворита",         "pct",  "например 85 = 85¢ (0.85)"),
    ("LIMIT_DISCOUNT",           "Скидка к лимиту (¢)",        "pct",  "например 3 = 3¢ ниже аска"),
    ("MIN_LIQUIDITY_USD",        "Мин. ликвидность",           "usd",  "USD на стороне фаворита"),
    ("MAX_DAILY_LOSS",           "Макс. дневной убыток",       "usd",  "USD — бот остановится"),
    ("MAX_CONCURRENT_POSITIONS", "Макс. позиций сразу",        "int",  "штук"),
    ("MIN_SECONDS_TO_EXPIRY",    "Мин. секунд до закрытия",    "sec",  "не входить если < N сек"),
    ("MAX_SECONDS_TO_EXPIRY",    "Макс. секунд до закрытия",   "sec",  "120 = последние 2 минуты"),
    ("MARKET_REFRESH_INTERVAL",  "Обновление рынков",          "sec",  "каждые N секунд"),
]


_MODE_LABELS = {
    "dry":        "[yellow]DRY — симуляция[/]",
    "paper":      "[cyan]PAPER — виртуальная торговля[/]",
    "live":       "[red bold]LIVE — реальные деньги[/]",
}
_SOURCE_LABELS = {
    "direct":     "[dim]DIRECT[/]",
    "monitoring": "[cyan]MONITORING[/]",
    "auto":       "[yellow]AUTO[/]",
}


def _fmt_value(val: str, typ: str) -> str:
    try:
        if typ == "bool":
            return "[green]ВКЛ (симуляция)[/]" if val.lower() == "true" else "[red bold]ВЫКЛ (live!)[/]"
        if typ == "mode":
            return _MODE_LABELS.get((val or "").lower(), f"[dim]{val or '—'}[/]")
        if typ == "source":
            return _SOURCE_LABELS.get((val or "").lower(), f"[dim]{val or '—'}[/]")
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
        elif typ == "mode":
            _cycle = ["dry", "paper", "live"]
            cur = (current_raw or "dry").lower().strip()
            nxt = _cycle[(_cycle.index(cur) + 1) % len(_cycle)] if cur in _cycle else "dry"
            console.print(f"  {label}: {_fmt_value(current_raw, typ)} → {_fmt_value(nxt, typ)}")
            env[key] = nxt
            _write_env(env)
            console.print("  [green]Сохранено! Перезапустите бота чтобы применить.[/green]")
            time.sleep(1.0)
        elif typ == "source":
            _cycle = ["direct", "monitoring", "auto"]
            cur = (current_raw or "direct").lower().strip()
            nxt = _cycle[(_cycle.index(cur) + 1) % len(_cycle)] if cur in _cycle else "direct"
            console.print(f"  {label}: {_fmt_value(current_raw, typ)} → {_fmt_value(nxt, typ)}")
            env[key] = nxt
            _write_env(env)
            console.print("  [green]Сохранено! Перезапустите бота чтобы применить.[/green]")
            time.sleep(1.0)
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


# ─── Генерация API ключей ─────────────────────────────────────────────────────

def api_keys_screen() -> None:
    """Экран генерации Polymarket CLOB API credentials из приватного ключа."""
    import os
    from pathlib import Path

    console.print("\n[bold cyan]═══ Генерация Polymarket API ключей ═══[/bold cyan]\n")

    env_path = Path(__file__).parent / ".env"
    env = _read_env()

    private_key = env.get("POLYMARKET_PRIVATE_KEY", "")
    if not private_key or private_key.startswith("0x...") or private_key == "0x":
        console.print("[red]❌ POLYMARKET_PRIVATE_KEY не задан в .env[/red]")
        console.print("  Добавь свой приватный ключ в .env и попробуй снова.\n")
        time.sleep(2)
        return

    key_preview = private_key[:6] + "..." + private_key[-4:]
    console.print(f"  🔑 Ключ: [yellow]{key_preview}[/yellow]")
    console.print("  ⏳ Запрашиваем credentials у Polymarket CLOB API...\n")

    try:
        import sys, os as _os
        _os.environ.setdefault("POLYMARKET_PRIVATE_KEY", private_key)
        from bot.config import Config
        from bot.wallet import derive_api_credentials, WalletError

        # Build a minimal config for key derivation
        cfg = Config.from_env()
        creds = derive_api_credentials(cfg)
    except Exception as exc:
        console.print(f"[red]❌ Ошибка: {exc}[/red]\n")
        time.sleep(3)
        return

    console.print("[green]✅ Готово! Новые API ключи:[/green]\n")
    console.print(f"  POLY_API_KEY        = [cyan]{creds['api_key']}[/cyan]")
    console.print(f"  POLY_API_SECRET     = [cyan]{creds['api_secret']}[/cyan]")
    console.print(f"  POLY_API_PASSPHRASE = [cyan]{creds['api_passphrase']}[/cyan]")
    console.print()

    # Auto-save to .env
    env["POLY_API_KEY"]        = creds["api_key"]
    env["POLY_API_SECRET"]     = creds["api_secret"]
    env["POLY_API_PASSPHRASE"] = creds["api_passphrase"]
    _write_env(env)
    console.print("[green]  Сохранено в .env автоматически![/green]")
    console.print("[dim]  Нажми любую клавишу чтобы вернуться...[/dim]")
    msvcrt.getch()


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
                    # Специальные клавиши Windows (стрелки, PgUp/PgDn, F-keys)
                    # генерируют 2 байта: \x00 или \xe0, затем код клавиши.
                    # Второй байт PgDn = \x51 = 'Q', Down = \x50 = 'P' —
                    # без этой проверки они случайно триггерят quit/pause!
                    if ch in (b"\x00", b"\xe0"):
                        # Ждём второй байт (он может ещё не поступить в буфер)
                        time.sleep(0.01)
                        if msvcrt.kbhit():
                            msvcrt.getch()  # поглощаем второй байт и игнорируем
                        continue
                    try:
                        decoded = ch.decode("utf-8", errors="ignore").lower()
                    except Exception:
                        decoded = ""
                    if decoded in ("q", "s", "p", "o"):
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


# ─── Event log handler ────────────────────────────────────────────────────────

class _StateEventHandler(logging.Handler):
    """
    Forwarding-хэндлер: пишет важные INFO/WARN/ERROR события бота
    в state.recent_events (ring buffer) для отображения в дашборде.

    Правила фильтрации:
    - Только логгеры 'bot.*' (не сторонние библиотеки)
    - Только INFO и выше
    - Многострочные сообщения — только первая строка (остальное в bot.log)
    """

    def __init__(self, state: BotState) -> None:
        super().__init__(level=logging.INFO)
        self._state = state

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith("bot."):
            return
        # Берём только первую строку (DryRunExecutor логирует многострочно)
        first_line = record.getMessage().partition("\n")[0].strip()
        if not first_line:
            return
        icon = "⚠ " if record.levelno >= logging.WARNING else "· "
        mod  = record.name.split(".")[-1]          # e.g. "main", "executor"
        self._state.add_event(f"{icon}{mod}: {first_line[:68]}")


# ─── Рендер блоков ────────────────────────────────────────────────────────────

_ET = ZoneInfo("America/New_York")
_TRADING_START = (11, 20)   # 11:20 AM ET
_TRADING_END   = (15, 50)   # 3:50 PM ET


def _et_now_str() -> str:
    """Текущее время ET в формате HH:MM:SS."""
    from datetime import datetime as _dt
    return _dt.now(_ET).strftime("%H:%M:%S ET")


def _trading_status() -> str:
    """Статус торгового часа ET."""
    from datetime import datetime as _dt
    now = _dt.now(_ET)
    h, m = now.hour, now.minute
    sh, sm = _TRADING_START
    eh, em = _TRADING_END
    in_window = (h * 60 + m) >= (sh * 60 + sm) and (h * 60 + m) < (eh * 60 + em)
    # Weekday 0=Mon..4=Fri
    if now.weekday() >= 5:
        return "[dim]выходной[/]"
    return "[green]OPEN[/]" if in_window else "[dim]CLOSED (11:20-15:50 ET)[/]"


def render_header(state: BotState) -> Panel:
    mode_color = {"DRY RUN": "yellow", "PAPER": "cyan", "LIVE": "red"}.get(state.mode, "dim")
    ws_status = "[green]ПОДКЛЮЧЁН[/]" if state.ws_connected else "[red]ПЕРЕПОДКЛЮЧЕНИЕ[/]"

    title = Text()
    title.append("  Polymarket Favourite-Leg Bot  ", style="bold white on dark_blue")
    title.append(f"  {state.mode}  ", style=f"bold white on {mode_color}")
    title.append(f"  Аптайм: {state.uptime_str}  ", style="dim")
    title.append(f"  {_et_now_str()}  ", style="cyan")
    title.append(f"  WS: {ws_status}  ")
    if getattr(state, "data_source", "DIRECT") == "MONITORING":
        title.append("  MON  ", style="bold cyan")
    title.append(f"  Рынков: {state.markets_loaded:,}  ", style="cyan")
    title.append(f"  Токенов: {state.tokens_subscribed:,}  ", style="cyan dim")
    title.append(f"  Заторг.: {getattr(state, 'traded_this_epoch', 0)}  ", style="dim")

    # Live safety indicators — показываем перед статусом паузы
    if state.kill_switch_active:
        title.append("  KILL SWITCH  ", style="bold white on red")
    elif state.live_halted:
        title.append("  LIVE HALTED  ", style="bold white on dark_red")

    if state.bot_paused:
        title.append("  ПАУЗА [p=продолжить]  ", style="bold white on dark_orange3")
    elif state.risk_paused:
        title.append("  РИСК-ПАУЗА  ", style="bold white on red")
    else:
        status = "[green bold]РАБОТАЕТ[/]" if state.running else "[red bold]ОСТАНОВЛЕН[/]"
        title.append(f"  {status} [dim][p=пауза][/]  ")

    return Panel(title, box=box.HORIZONTALS, padding=(0, 1))


def render_stats(state: BotState) -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", min_width=20)
    t.add_column(style="bold")

    sim_tag = " [dim](сим)[/]" if state.mode == "DRY RUN" else ""

    t.add_row("Обновлений цен:", f"[cyan]{state.price_updates:,}[/]")
    t.add_row("Проанализировано:", f"{state.analyzed:,}")

    # Pipeline funnel
    in_win = getattr(state, "markets_in_window", 0)
    with_fav = getattr(state, "markets_with_favorite", 0)
    rej_fav = getattr(state, "rejected_no_favorite", 0)
    rej_win = getattr(state, "rejected_out_of_window", 0)
    rej_liq = getattr(state, "rejected_low_liquidity", 0)
    fav_color = "green" if with_fav > 0 else "dim"
    t.add_row("  В окне:", f"[cyan]{in_win}[/] рынков")
    t.add_row("  С фаворитом:", f"[{fav_color}]{with_fav}[/]")
    if rej_win > 0 or rej_fav > 0 or rej_liq > 0:
        t.add_row("  Отсев вне окна:", f"[dim]{rej_win:,}[/]")
        t.add_row("  Отсев нет фав:", f"[dim]{rej_fav:,}[/]")
        t.add_row("  Отсев ликвид.:", f"[dim]{rej_liq:,}[/]")

    # Favourite price distribution
    d65  = getattr(state, "fav_dist_65_70",  0)
    d70  = getattr(state, "fav_dist_70_75",  0)
    d75  = getattr(state, "fav_dist_75_80",  0)
    d80  = getattr(state, "fav_dist_80plus", 0)
    thresh = getattr(state, "min_favorite_price", 0.75)
    thresh_pct = int(thresh * 100)
    if d65 + d70 + d75 + d80 > 0:
        t.add_row("  Fav 65–70%:", f"[dim]{d65}[/]")
        t.add_row("  Fav 70–75%:", f"[dim]{d70}[/]")
        # Highlight the bucket that crosses the active threshold
        t.add_row(
            f"  Fav 75–80% [dim](≥{thresh_pct}%→ сигнал)[/]:",
            f"[yellow bold]{d75}[/]" if d75 else "[dim]0[/]",
        )
        t.add_row("  Fav ≥80%:", f"[green bold]{d80}[/]" if d80 else "[dim]0[/]")

    t.add_row("Сигналов найдено:", f"[yellow]{state.opportunities_found:,}[/]")
    t.add_row("Ордеров выставлено:", f"[bold]{state.trades_executed}[/]")
    t.add_row("Заторгованных:", f"[dim]{getattr(state, 'traded_this_epoch', 0)} рынков[/]")

    # Trade lifecycle
    active_pos = getattr(state, "active_positions_count", 0)
    closed_pos = getattr(state, "closed_positions_count", 0)
    pos_color = "cyan" if active_pos > 0 else "dim"
    t.add_row("  Активных поз.:", f"[{pos_color}]{active_pos}[/]")
    t.add_row("  Закрыто поз.:", f"[dim]{closed_pos}[/]")

    # Last signal and last reject for diagnostics
    last_sig = getattr(state, "last_signal_info", "")
    last_rej = getattr(state, "last_window_reject", "")
    if last_sig:
        t.add_row("  Посл. сигнал:", f"[green]{last_sig[:38]}[/]")
    if last_rej:
        t.add_row("  Посл. reject:", f"[dim]{last_rej[:38]}[/]")

    t.add_row("")

    # PnL
    daily_color = "green" if state.daily_pnl >= 0 else "red"
    daily_sign = "+" if state.daily_pnl >= 0 else "-"
    t.add_row(
        "PnL сегодня:",
        f"[{daily_color}]{daily_sign}${abs(state.daily_pnl):.2f}[/]{sim_tag}",
    )
    total_color = "green" if state.total_pnl >= 0 else "red"
    total_sign = "+" if state.total_pnl >= 0 else "-"
    t.add_row(
        "PnL итого:",
        f"[{total_color}]{total_sign}${abs(state.total_pnl):.2f}[/]{sim_tag}",
    )
    t.add_row("")

    # Favourite tracker (Gabagool)
    best_fav = state.best_comb_today          # best favourite price seen in entry window
    threshold = getattr(state, "min_favorite_price", 0.75)
    if best_fav > 0:
        if best_fav >= threshold:
            best_color = "green bold"
        elif best_fav >= threshold - 0.05:
            best_color = "yellow"
        else:
            best_color = "dim"
        best_str = f"[{best_color}]{best_fav:.3f}[/]"
        if state.best_comb_market:
            short = _short_name(state.best_comb_market, 14)
            best_str += f" [dim]{short}[/]"
    else:
        best_str = "[dim]—[/]"
    t.add_row("Лучший фаворит:", best_str)
    nm_delta = getattr(state, "candidate_near_delta", 0.05)
    nm_color = "yellow" if state.near_misses > 0 else "dim"
    nm_floor = threshold - nm_delta
    t.add_row(
        "Near-miss:",
        f"[{nm_color}]{state.near_misses:,}[/] "
        f"[dim](фав {nm_floor:.0%}–{threshold:.0%} in окне)[/]",
    )
    t.add_row("")

    # Time-to-expiry breakdown
    tgt5 = getattr(state, "time_gt_5m",  0)
    t25  = getattr(state, "time_2_5m",   0)
    t12  = getattr(state, "time_1_2m",   0)
    tlt1 = getattr(state, "time_lt_1m",  0)
    entry_sec = getattr(state, "max_seconds_to_expiry", 120)
    if tgt5 + t25 + t12 + tlt1 > 0:
        t.add_row(f"  >5мин (>300с):", f"[dim]{tgt5}[/]")
        t.add_row(f"  2–5мин (>{entry_sec}с):", f"[dim]{t25}[/]")
        t.add_row(f"  1–2мин (60–{entry_sec}с):", f"[cyan]{t12}[/]" if t12 else "[dim]0[/]")
        t.add_row(f"  <1мин (<60с):", f"[yellow]{tlt1}[/]" if tlt1 else "[dim]0[/]")
    t.add_row("")

    # Конфиг
    t.add_row("Стратегия:", "[bold cyan]Favourite-Leg[/]")
    t.add_row("Окно входа:", f"последние {state.max_seconds_to_expiry}с")
    t.add_row("Макс. позиция:", f"${state.max_position_size:.0f}")
    t.add_row("WS последний:", f"{state.ws_last_msg_sec:.0f}с назад")
    ws_total = getattr(state, "ws_messages_total", 0)
    ws_unk   = getattr(state, "ws_unknown_tokens", 0)
    ws_msg_str = f"[cyan]{ws_total:,}[/]"
    if ws_unk > 0:
        ws_msg_str += f" [red]({ws_unk} unknown token)[/]"
    t.add_row("WS сообщений:", ws_msg_str)

    # Latency metrics
    tick_ms = getattr(state, "tick_latency_ms", 0.0)
    s2o_ms  = getattr(state, "signal_to_order_ms", 0.0)
    tick_color = "green" if tick_ms < 5 else ("yellow" if tick_ms < 20 else "red")
    s2o_color  = "green" if s2o_ms < 100 else ("yellow" if s2o_ms < 500 else "red")
    t.add_row("Tick latency:", f"[{tick_color}]{tick_ms:.1f}мс[/]")
    if s2o_ms > 0:
        t.add_row("Signal→Order:", f"[{s2o_color}]{s2o_ms:.0f}мс[/]")

    # Live safety — показываем только в LIVE режиме
    if state.mode == "LIVE":
        t.add_row("")
        ks_str = "[red bold]АКТИВЕН[/]" if state.kill_switch_active else "[green]выкл[/]"
        t.add_row("Kill switch:", ks_str)
        halted_str = "[red bold]ДА[/]" if state.live_halted else "[green]нет[/]"
        t.add_row("Live заблок.:", halted_str)
        t.add_row("Live ордеров:", f"{state.live_orders_session}")

    # ── Wallet block ──────────────────────────────────────────────────────────
    t.add_row("")
    addr = getattr(state, "wallet_address_short", "")
    if not addr:
        t.add_row("[dim]Кошелёк:[/]", "[dim]не настроен[/]")
    else:
        t.add_row("[dim]Кошелёк:[/]", f"[dim]{addr}[/]")

        usdc = getattr(state, "wallet_usdc", -1.0)
        pol  = getattr(state, "wallet_pol",  -1.0)

        if usdc < 0:
            usdc_str = "[dim]…[/]"
        elif usdc < state.max_position_size:
            usdc_str = f"[red bold]${usdc:.2f} ⚠[/]"
        else:
            usdc_str = f"[green]${usdc:.2f}[/]"
        t.add_row("  USDC:", usdc_str)

        if pol < 0:
            pol_str = "[dim]…[/]"
        elif pol < 1.0:
            pol_str = f"[red bold]{pol:.3f} ⚠[/]"
        else:
            pol_str = f"[dim]{pol:.3f}[/]"
        t.add_row("  POL (gas):", pol_str)

        proxy = getattr(state, "wallet_proxy_short", "")
        t.add_row("  Proxy:", f"[dim]{proxy}[/]" if proxy else "[dim]нет[/]")

    api_ok = getattr(state, "wallet_api_configured", False)
    api_str = "[green]ОК[/]" if api_ok else "[yellow]missing[/]"
    t.add_row("  API creds:", api_str)

    return Panel(t, title="[bold]Статистика[/]", box=box.ROUNDED, border_style="blue")


def render_events(state: BotState) -> Panel:
    """Последние N событий бота (newest first)."""
    text = Text(overflow="ellipsis")
    events = list(state.recent_events)[:14]

    if not events:
        text.append("Ожидаем события...", style="dim")
    else:
        for i, line in enumerate(events):
            if i > 0:
                text.append("\n")
            # Цветовая маркировка по ключевым словам
            if "⚠" in line:
                style = "yellow"
            elif any(w in line for w in ("FILLED", "WIN", "connected", "refreshed")):
                style = "green"
            elif any(w in line for w in ("LOSS", "CANCELLED", "error", "failed", "pausing")):
                style = "red"
            else:
                style = "dim"
            text.append(line, style=style)

    return Panel(
        text,
        title="[bold]Последние события[/]",
        box=box.ROUNDED,
        border_style="cyan",
    )


def render_candidates(state: BotState) -> Panel:
    """Рынки близкие к порогу фаворита — ещё не прошли фильтр."""
    threshold = state.min_favorite_price
    near_delta = getattr(state, "candidate_near_delta", 0.05)

    candidates = [
        m for m in state.active_market_prices.values()
        if m.seconds_left is not None
        and state.min_seconds_to_expiry < m.seconds_left < state.max_seconds_to_expiry
        and m.favorite_price is not None
        and (threshold - near_delta) <= m.favorite_price < threshold
    ]
    # Ближайшие к порогу — первыми
    candidates.sort(key=lambda m: -(m.favorite_price or 0.0))
    candidates = candidates[:6]

    t = Table(
        show_header=True,
        header_style="bold dim",
        box=box.SIMPLE,
        padding=(0, 1),
        expand=True,
    )
    t.add_column("Market", ratio=4)
    t.add_column("Fav", justify="center", min_width=4)
    t.add_column("Fav$", justify="right", min_width=6)
    t.add_column("Gap↑", justify="right", min_width=6)   # нужен рост на Gap↑ для сигнала
    t.add_column("Left", justify="right", min_width=5)

    if not candidates:
        t.add_row(
            f"[dim]Нет кандидатов (ищем фаворита ≥ {threshold:.0%})...[/]",
            "", "", "", "",
        )
    else:
        for m in candidates:
            fp  = m.favorite_price or 0.0
            gap = threshold - fp   # сколько нужно ещё вырасти до порога
            sec = m.seconds_left or 0

            if gap <= near_delta * 0.4:
                style = "yellow"
            elif gap <= near_delta * 0.8:
                style = "orange3"
            else:
                style = "dim"

            q = _short_name(m.question, 32)
            t.add_row(
                f"[{style}]{q}[/]",
                f"[{style}]{m.favorite_side or '—'}[/]",
                f"[{style}]{fp:.3f}[/]",
                f"[{style}]+{gap:.3f}[/]",
                f"[dim]{sec:.0f}s[/]",
            )

    subtitle = f"[dim]фаворит {threshold - near_delta:.0%}–{threshold:.0%}, ниже порога[/dim]"
    return Panel(
        t,
        title="[bold]Кандидаты (почти-сигналы)[/]",
        subtitle=subtitle,
        box=box.ROUNDED,
        border_style="yellow",
    )


def render_markets(state: BotState) -> Panel:
    # ── Активные рынки в окне (последние 2 минуты) ───────────────────────────
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
    t.add_column("Fav", justify="center", min_width=5)
    t.add_column("Fav$", justify="right", min_width=6)
    t.add_column("Bid$", justify="right", min_width=6)
    t.add_column("Left", justify="right", min_width=5)

    markets = state.get_top_opportunities(n=10)
    entry_sec = getattr(state, "max_seconds_to_expiry", 120)
    fav_thresh = getattr(state, "min_favorite_price", 0.75)

    if not markets:
        t.add_row(
            f"[dim]Ожидаем рынки в окне последних {entry_sec}с...[/]",
            "", "", "", "", "", "",
        )
        subtitle = f"[dim]Окно: последние {entry_sec}с перед закрытием[/dim]"
    else:
        subtitle = f"[dim]Топ {len(markets)} рынков в окне (фавориты первыми)[/dim]"

    for m in markets:
        seconds_left = m.seconds_left or 0
        fav_side = m.favorite_side or "—"
        fav_price = m.favorite_price or 0.0

        bid_price = round(fav_price - state.limit_discount, 3) if fav_price > 0 else 0.0

        # Цвет зависит от силы фаворита относительно порога
        if fav_price >= fav_thresh + 0.10:
            fav_style = "bold green"
            bid_style = "bold green"
        elif fav_price >= fav_thresh:
            fav_style = "yellow"
            bid_style = "yellow"
        else:
            fav_style = "dim"
            bid_style = "dim"

        q = _short_name(m.question, 35)

        t.add_row(
            q,
            f"{m.yes_ask:.3f}" if m.yes_ask else "—",
            f"{m.no_ask:.3f}" if m.no_ask else "—",
            f"[{fav_style}]{fav_side}[/]" if fav_side != "—" else "[dim]—[/]",
            f"[{fav_style}]{fav_price:.3f}[/]" if fav_price else "—",
            f"[{bid_style}]{bid_price:.3f}[/]" if bid_price else "—",
            f"{seconds_left:.0f}s" if seconds_left else "—",
        )

    # ── Upcoming (входят в окно скоро) ───────────────────────────────────────
    upcoming = state.get_upcoming_markets(n=8)
    if upcoming:
        t.add_row("", "", "", "", "", "", "")
        t.add_row("[dim bold]↓ Скоро войдут в окно[/]", "", "", "", "", "", "")
        for m in upcoming:
            sec = m.seconds_left or 0
            mins_left = int(sec // 60)
            secs_left = int(sec % 60)
            q = _short_name(m.question, 35)
            fav_price = m.favorite_price or 0.0
            fav_side = m.favorite_side or "—"
            t.add_row(
                f"[dim]{q}[/]",
                f"[dim]{m.yes_ask:.3f}[/]" if m.yes_ask else "—",
                f"[dim]{m.no_ask:.3f}[/]" if m.no_ask else "—",
                f"[dim]{fav_side}[/]",
                f"[dim]{fav_price:.3f}[/]" if fav_price else "—",
                "",
                f"[dim]{mins_left}m{secs_left:02d}s[/]",
            )

    # ── Window market samples — debug rows when markets_in_window > 0 ─────────
    # Shows up to 3 in-window markets with their actual yes/no prices, favourite,
    # and why they are (or aren't) triggering a signal.  Only shown when there are
    # markets in the entry window so the debug section doesn't clutter idle view.
    samples = getattr(state, "window_market_samples", [])
    if samples:
        t.add_row("", "", "", "", "", "", "")
        t.add_row(
            f"[bold dim]↓ В окне — диагностика ({len(samples)})[/]",
            "", "", "", "", "", "",
        )
        thresh = getattr(state, "min_favorite_price", 0.75)
        for s in samples:
            fp = s.get("fav_p", 0.0)
            traded = s.get("traded", False)
            reject = s.get("reject", "")
            # Colour: green=signal_ready, yellow=close, dim=far, orange=traded
            if traded:
                row_style = "orange3"
                reject_str = "[orange3]traded[/]"
            elif fp >= thresh:
                row_style = "green"
                reject_str = f"[green]≥{thresh:.2f} → signal[/]"
            elif fp >= thresh - 0.05:
                row_style = "yellow"
                reject_str = f"[yellow]{reject}[/]"
            else:
                row_style = "dim"
                reject_str = f"[dim]{reject}[/]"

            q = _short_name(s.get("q", ""), 28)
            yes_v = s.get("yes", 0.0) or 0.0
            no_v  = s.get("no",  0.0) or 0.0
            t.add_row(
                f"[{row_style}]{q}[/]",
                f"[{row_style}]{yes_v:.3f}[/]",
                f"[{row_style}]{no_v:.3f}[/]",
                f"[{row_style}]{s.get('fav', '—')}[/]",
                f"[{row_style}]{fp:.3f}[/]" if fp else "—",
                reject_str,
                f"[dim]{s.get('left', 0):.0f}s[/]",
            )

    entry_min = entry_sec // 60
    entry_label = f"{entry_min}мин" if entry_sec % 60 == 0 else f"{entry_sec}с"
    return Panel(
        t,
        title=f"[bold]Активные рынки — окно входа (последние {entry_label})[/]",
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
    t.add_column("Время", min_width=8)
    t.add_column("Рынок", ratio=3)
    t.add_column("Сторона", justify="center", min_width=7)
    t.add_column("Цена/шт", justify="right", min_width=7)   # bid price per share
    t.add_column("Акций", justify="right", min_width=6)     # number of shares
    t.add_column("Ставка", justify="right", min_width=7)    # total stake = shares × price
    t.add_column("Осталось", justify="right", min_width=7)
    t.add_column("Профит", justify="right", min_width=10)
    t.add_column("", min_width=4)

    if not state.recent_trades:
        t.add_row("[dim]Сделок пока нет...[/]", "", "", "", "", "", "", "", "")
    else:
        for trade in state.recent_trades[:10]:
            ts = trade.timestamp.strftime("%H:%M:%S")

            # Цена доли (bid price per share) = combined field (repurposed)
            bid_price = trade.combined
            bid_str = f"${bid_price:.3f}"

            # Количество акций
            shares = int(trade.trade_size)
            shares_str = f"{shares}"

            # Ставка = акции × цена (сколько реально потратили)
            stake = shares * bid_price
            stake_str = f"${stake:.2f}"

            # Сторона
            if trade.side == "YES":
                side_str = "[green]YES[/]"
            elif trade.side == "NO":
                side_str = "[cyan]NO[/]"
            else:
                side_str = "[dim]±[/]"

            # Профит + статус
            if trade.outcome == "PENDING":
                stake = int(trade.trade_size) * trade.combined
                pnl = f"[dim]-${stake:.2f}[/]"   # потрачено, ждём исхода
                outcome_str = "[yellow]⏳[/]"
            elif trade.outcome == "WIN":
                pnl = f"[green]+${trade.profit_usd:.3f}[/]"
                outcome_str = "[green bold]WIN[/]"
            elif trade.outcome == "LOSS":
                pnl = f"[red]-${abs(trade.profit_usd):.3f}[/]"
                outcome_str = "[red bold]LOSS[/]"
            else:
                sign = "[green]+[/]" if trade.profit_usd >= 0 else "[red]-[/]"
                pnl = f"{sign}${abs(trade.profit_usd):.3f}"
                outcome_str = "[dim]…[/]"

            sim = "[dim](sim)[/]" if trade.dry_run else "[yellow](live)[/]"
            q = _short_name(trade.market, 25)

            sec = trade.seconds_left
            if sec >= 60:
                left_str = f"[dim]{int(sec // 60)}м{int(sec % 60):02d}с[/]"
            else:
                left_str = f"[yellow]{int(sec)}с[/]"

            t.add_row(
                f"[dim]{ts}[/]", q, side_str,
                bid_str, shares_str, stake_str,
                left_str, f"{pnl} {outcome_str}", sim,
            )

    return Panel(t, title="[bold]Последние сделки[/]", box=box.ROUNDED, border_style="magenta")


def render_help(state: BotState) -> Panel:
    pause_label = "[yellow]p = ПРОДОЛЖИТЬ[/]" if state.bot_paused else "[dim]p = пауза[/]"
    keys = f"  [dim]q / Ctrl+C = выход  │  s = настройки  │  o = API ключи  │  {pause_label}  │  Favourite-Leg: фаворит в последние 2 мин[/dim]"
    last = state.last_event
    last_line = f"\n  [dim]▶ {last[:100]}[/dim]" if last else ""
    return Panel(
        keys + last_line,
        box=box.HORIZONTALS,
        padding=(0, 1),
    )


def build_layout(state: BotState) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(render_header(state), name="header", size=3),
        Layout(name="body"),
        Layout(render_help(state), name="footer", size=4),  # +1 для строки последнего события
    )
    layout["body"].split_row(
        Layout(name="left", ratio=1),
        Layout(name="right", ratio=2),
    )
    layout["left"].split_column(
        Layout(render_stats(state), name="stats", ratio=2),
        Layout(render_events(state), name="events", ratio=1),
    )
    layout["right"].split_column(
        Layout(render_markets(state), name="markets", ratio=3),
        Layout(render_candidates(state), name="candidates", ratio=1),
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
    # Дублируем важные события в state.recent_events для отображения в дашборде
    root_logger.addHandler(_StateEventHandler(state))
    root_logger.setLevel(logging.INFO)

    bot_task = asyncio.create_task(bot.start())
    keyboard = _KeyboardHandler()
    stop_requested = False

    with Live(
        build_layout(state),
        console=console,
        refresh_per_second=4,
        screen=True,
    ) as live:
        try:
            _last_render = 0.0
            while not bot_task.done() and not stop_requested:
                key = keyboard.consume()

                if key in ("q", "\x03"):   # q или Ctrl+C
                    stop_requested = True
                    break
                elif key == "p":           # пауза / продолжить
                    state.bot_paused = not state.bot_paused
                elif key == "s":
                    live.stop()
                    settings_screen()
                    live.start()
                elif key == "o":
                    live.stop()
                    api_keys_screen()
                    live.start()

                # Рендер не чаще 2 раз в секунду — плавный таймер без перегрузки
                now = asyncio.get_event_loop().time()
                if now - _last_render >= 0.5:
                    live.update(build_layout(state))
                    _last_render = now

                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        finally:
            console.print("\n[yellow]Останавливаем бота...[/]")
            await bot.stop()
            _bot_exc = None
            if not bot_task.done():
                bot_task.cancel()
                try:
                    await bot_task
                except asyncio.CancelledError:
                    pass
            else:
                # Task завершилась сама (не по cancel) — забираем исключение
                if not bot_task.cancelled():
                    _bot_exc = bot_task.exception()
            log_file.close()
            if _bot_exc is not None:
                raise _bot_exc

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


def _print_startup_diag(config: Config) -> None:
    """Вывести конфигурацию запуска перед стартом бота."""
    ds = config.data_source.lower()
    ds_color = {"direct": "dim", "monitoring": "cyan", "auto": "yellow"}.get(ds, "dim")
    mode_color = {"dry": "yellow", "paper": "cyan", "live": "red bold"}.get(config.bot_mode, "dim")

    console.print()
    console.rule("[dim]Запуск бота[/dim]")
    console.print(f"  BOT_MODE:             [{mode_color}]{config.bot_mode.upper()}[/]")
    console.print(f"  DATA_SOURCE:          [{ds_color}]{ds.upper()}[/]")
    if ds in ("monitoring", "auto"):
        g = config.monitoring_gamma_url or ""
        w = config.monitoring_ws_url or ""
        g_str = f"[cyan]{g}[/]" if g else "[red]не задан[/]"
        w_str = f"[cyan]{w}[/]" if w else "[red]не задан[/]"
        console.print(f"  MONITORING_API_URL:   {g_str}")
        console.print(f"  MONITORING_WS_URL:    {w_str}")
    console.rule()
    console.print()


def _show_startup_error(config: Config, exc: Exception) -> None:
    """Показать диагностику ошибки запуска и дождаться Enter."""
    exc_type = type(exc).__name__
    exc_msg = str(exc)

    # Определяем фазу отказа по содержимому сообщения
    msg_lower = exc_msg.lower()
    if "monitoring_api_url" in exc_msg.lower() or "monitoring_ws_url" in exc_msg.lower():
        phase = "инициализация monitoring-компонентов"
        hint = (
            "Задайте MONITORING_API_URL и MONITORING_WS_URL в .env.\n"
            "  Меню → Торговые настройки → Источник данных."
        )
    elif "bot_mode" in msg_lower or "data_source" in msg_lower:
        phase = "валидация конфига"
        hint = "Исправьте значения в .env или через меню настроек."
    elif "probe" in msg_lower or "connect" in msg_lower or "refused" in msg_lower:
        phase = "проба monitoring-сервера"
        hint = "Проверьте, что MONITORING_API_URL доступен с этой машины."
    elif "gamma" in msg_lower or "fetch" in msg_lower:
        phase = "Gamma API / discovery"
        hint = "Проверьте интернет-соединение и доступность gamma-сервера."
    elif "websocket" in msg_lower or "ws://" in msg_lower or "wss://" in msg_lower:
        phase = "WebSocket scanner"
        hint = "Проверьте MONITORING_WS_URL."
    else:
        phase = "bot.start()"
        hint = "Подробности в bot.log."

    console.print()
    console.rule("[bold red]ОШИБКА ЗАПУСКА БОТА[/bold red]")
    console.print()

    # Конфиг запуска
    _print_startup_diag(config)

    console.print(f"  [red bold]Тип ошибки:[/red bold] {exc_type}")
    console.print(f"  [red bold]Фаза:[/red bold]       {phase}")
    console.print()
    console.print("  [red bold]Сообщение:[/red bold]")
    for line in exc_msg.splitlines():
        console.print(f"    {line}")
    console.print()
    console.print(f"  [yellow]Подсказка:[/yellow] {hint}")
    console.print()
    console.input("[dim]Нажмите Enter чтобы вернуться в меню...[/dim]")


def main() -> None:
    first_run_wizard()         # мастер настройки если нет .env
    config = Config.from_env()

    try:
        config.validate()
    except ValueError as e:
        console.print()
        console.print("[red bold]ОШИБКА КОНФИГУРАЦИИ[/]")
        console.print()
        for line in str(e).splitlines():
            console.print(f"  {line}")
        console.print()
        console.print("[dim]Исправьте .env и перезапустите бота.[/dim]")
        console.print()
        # ← ВАЖНО: ждём Enter, иначе launcher.show_main_menu() сотрёт ошибку
        console.input("[dim]Нажмите Enter чтобы вернуться в меню...[/dim]")
        return

    # Дополнительная проверка для monitoring-режима (URLs уже проверены в validate,
    # но показываем явное сообщение ДО старта чтобы помочь с диагностикой)
    if config.data_source.lower() == "monitoring":
        missing_urls = []
        if not (config.monitoring_gamma_url or "").strip():
            missing_urls.append("MONITORING_API_URL")
        if not (config.monitoring_ws_url or "").strip():
            missing_urls.append("MONITORING_WS_URL")
        if missing_urls:
            console.print()
            console.print("[red bold]Monitoring mode requires:[/red bold]", ", ".join(missing_urls))
            console.print("[dim]Задайте URL-ы в .env через меню → Торговые настройки.[/dim]")
            console.print()
            console.input("[dim]Нажмите Enter чтобы вернуться в меню...[/dim]")
            return

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
    except Exception as exc:
        # Любое необработанное исключение из bot.start() или инициализации
        _show_startup_error(config, exc)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
