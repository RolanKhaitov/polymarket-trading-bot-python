#!/usr/bin/env python3
"""
Polymarket Arb Bot — Главное меню

Запуск: LAUNCH.bat (двойной клик)
"""

import subprocess
import sys
import time
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()
ENV_PATH = Path(__file__).parent / ".env"


# ─── .env утилиты ─────────────────────────────────────────────────────────────

def read_env() -> dict[str, str]:
    result: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                # Убрать inline-комментарий: KEY=value  # comment
                v = v.split("#")[0].strip()
                result[k.strip()] = v
    return result


def write_env(data: dict[str, str]) -> None:
    lines: list[str] = []
    remaining = dict(data)
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.partition("=")[0].strip()
                if k in remaining:
                    lines.append(f"{k}={remaining.pop(k)}")
                    continue
            lines.append(line)
    for k, v in remaining.items():
        lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_defaults() -> None:
    """Создать .env с дефолтными значениями если не существует."""
    if ENV_PATH.exists():
        return
    defaults = {
        "BOT_MODE": "paper",
        "DATA_SOURCE": "direct",
        "MAX_POSITION_SIZE": "1",
        "MIN_LIQUIDITY_USD": "10",
        "MAX_DAILY_LOSS": "50",
        "MAX_CONCURRENT_POSITIONS": "10",
        "MIN_SECONDS_TO_EXPIRY": "10",
        "MAX_SECONDS_TO_EXPIRY": "120",
        "MARKET_REFRESH_INTERVAL": "60",
        "LOG_LEVEL": "INFO",
    }
    write_env(defaults)


# ─── Статус-панель ────────────────────────────────────────────────────────────

def status_panel() -> Panel:
    env = read_env()

    bot_mode = env.get("BOT_MODE", "").lower().strip()
    if not bot_mode:
        bot_mode = "dry" if env.get("DRY_RUN", "true").lower() != "false" else "live"

    has_wallet = bool(env.get("POLYMARKET_PRIVATE_KEY", "").strip())

    mode_text = {
        "dry":   "[yellow]DRY — симуляция[/]",
        "paper": "[cyan]PAPER — виртуальная торговля[/]",
        "live":  "[red bold]LIVE — реальные деньги[/]",
    }.get(bot_mode, f"[dim]{bot_mode}[/]")
    wallet_text = "[green]configured[/]" if has_wallet else "[yellow]missing[/]"
    env_text    = "[green]найден[/]" if ENV_PATH.exists() else "[red]не найден[/]"

    max_pos  = env.get("MAX_POSITION_SIZE", "1")
    max_loss = env.get("MAX_DAILY_LOSS", "50")

    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", min_width=20)
    t.add_column()

    t.add_row("Файл настроек:", env_text)
    t.add_row("Режим:", mode_text)
    t.add_row("Wallet:", wallet_text)
    t.add_row("Макс. позиция:", f"${max_pos}")
    t.add_row("Макс. убыток/день:", f"${max_loss}")

    return Panel(t, title="[bold]Текущие настройки[/]", box=box.ROUNDED, border_style="cyan")


# ─── Главное меню ─────────────────────────────────────────────────────────────

def show_main_menu() -> str:
    console.clear()

    # Заголовок
    title = Text()
    title.append("  Polymarket 15-min Arb Bot  ", style="bold white on dark_blue")
    console.print(Panel(title, box=box.HORIZONTALS, padding=(0, 1)))
    console.print()

    # Статус
    console.print(status_panel())
    console.print()

    # Меню
    t = Table(show_header=False, box=box.SIMPLE, padding=(0, 3))
    t.add_column(style="bold cyan", min_width=4)
    t.add_column(min_width=30)
    t.add_column(style="dim")

    t.add_row("[1]", "Запустить бота",           "открыть дашборд")
    t.add_row("[2]", "Торговые настройки",        "прибыль, позиция, лимиты")
    t.add_row("[3]", "API ключи и кошелёк",       "для live-торговли")
    t.add_row("[4]", "Посмотреть логи",           "последние события")
    t.add_row("[5]", "Выход",                     "")

    console.print(Panel(t, title="[bold]Меню[/]", box=box.ROUNDED, border_style="blue"))
    console.print()

    return console.input("[cyan]Введите номер и нажмите Enter: [/cyan]").strip()


# ─── Пункты меню ──────────────────────────────────────────────────────────────

def menu_launch_bot() -> None:
    console.clear()
    console.print("[bold green]Запуск бота...[/bold green]")
    console.print("[dim]Нажмите q в дашборде чтобы вернуться в меню[/dim]")
    console.print("[dim]Нажмите s в дашборде для быстрых настроек[/dim]")
    console.print()
    time.sleep(1)
    subprocess.run([sys.executable, "dashboard.py"])


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


def _fmt_field(raw: str, typ: str) -> str:
    try:
        if typ == "mode":
            return _MODE_LABELS.get((raw or "").lower(), f"[dim]{raw or '—'}[/]")
        if typ == "source":
            return _SOURCE_LABELS.get((raw or "").lower(), f"[dim]{raw or '—'}[/]")
        if typ == "pct":
            return f"{float(raw) * 100:.1f}%"
        if typ == "usd":
            return f"${float(raw):.1f}"
        if typ == "url":
            return f"[cyan]{raw}[/]" if raw else "[red]не задан[/]"
        return raw or "—"
    except Exception:
        return raw or "—"


def menu_trading_settings() -> None:
    FIELDS = [
        ("BOT_MODE",                 "Режим бота",                "mode",   "dry · paper · live  [Enter = следующий]"),
        ("DATA_SOURCE",              "Источник данных",           "source", "direct · monitoring · auto  [Enter = следующий]"),
        ("MONITORING_API_URL",        "Monitoring API URL",        "url",    "http://host:port  (только для DATA_SOURCE=monitoring/auto)"),
        ("MONITORING_WS_URL",        "Monitoring WebSocket URL",  "url",    "ws://host:port/ws  (только для DATA_SOURCE=monitoring/auto)"),
        ("MAX_POSITION_SIZE",        "Макс. позиция",             "usd",    "USD на один рынок"),
        ("MIN_FAVORITE_PRICE",      "Мин. цена фаворита",        "pct",    "0.75 = 75¢ — в последние 2 мин должно быть ≥75¢"),
        ("LIMIT_DISCOUNT",          "Скидка лимита (¢)",         "pct",    "0.03 = 3¢ ниже ask — наша лимитная цена"),
        ("MIN_LIQUIDITY_USD",       "Мин. ликвидность",          "usd",    "USD на стороне фаворита (5-10 для 5-мин рынков)"),
        ("CANDIDATE_NEAR_DELTA",    "Зона кандидатов",           "pct",    "0.05 = показывать рынки в диапазоне (fav-5¢, fav)"),
        ("MAX_DAILY_LOSS",           "Макс. дневной убыток",      "usd",    "бот остановится при достижении"),
        ("MAX_CONCURRENT_POSITIONS", "Макс. позиций одновременно","int",    "рекомендуется 3-10"),
        ("MIN_SECONDS_TO_EXPIRY",    "Мин. секунд до закрытия",   "sec",    "не входить если < N сек"),
        ("MAX_SECONDS_TO_EXPIRY",    "Макс. секунд до закрытия",  "sec",    "120 = последние 2 минуты"),
        ("MARKET_REFRESH_INTERVAL",  "Обновление списка рынков",  "sec",    "каждые N секунд"),
    ]

    while True:
        env = read_env()
        console.clear()
        console.rule("[bold cyan]Торговые настройки[/bold cyan]")
        console.print()

        t = Table(show_header=True, header_style="bold cyan", box=box.ROUNDED, padding=(0, 2))
        t.add_column("#",          min_width=3,  justify="right", style="dim")
        t.add_column("Параметр",   min_width=30)
        t.add_column("Значение",   min_width=22, justify="right")
        t.add_column("Пояснение",  style="dim")

        for i, (key, label, typ, hint) in enumerate(FIELDS, 1):
            t.add_row(str(i), label, _fmt_field(env.get(key, ""), typ), hint)

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
            if not (0 <= idx < len(FIELDS)):
                raise ValueError
        except ValueError:
            console.print("[red]Неверный номер[/red]")
            time.sleep(0.8)
            continue

        key, label, typ, hint = FIELDS[idx]
        current = env.get(key, "")

        if typ == "mode":
            cycle = ["dry", "paper", "live"]
            cur = (current or "paper").lower().strip()
            nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else "paper"
            console.print(f"  {label}: {_fmt_field(current, typ)} → {_fmt_field(nxt, typ)}")

            if nxt == "live":
                # ── Safety confirmation before enabling LIVE mode ─────────────
                console.print()
                console.print("[red bold]WARNING: LIVE trading enabled[/red bold]")
                console.print("[red]Real funds will be used.[/red]")
                has_wallet = bool(env.get("POLYMARKET_PRIVATE_KEY", "").strip())
                if not has_wallet:
                    console.print(
                        "[yellow]  Wallet not configured — set POLYMARKET_PRIVATE_KEY "
                        "in option [3] before starting.[/yellow]"
                    )
                console.print()
                confirm = console.input("  Type [bold]YES[/bold] to continue: ").strip()
                if confirm != "YES":
                    console.print("  [dim]Cancelled — mode not changed.[/dim]")
                    time.sleep(1.0)
                    continue

            env[key] = nxt
            write_env(env)
            console.print("  [green]Сохранено! Перезапустите бота чтобы применить.[/green]")
            time.sleep(1.0)
        elif typ == "source":
            cycle = ["direct", "monitoring", "auto"]
            cur = (current or "direct").lower().strip()
            nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else "direct"
            console.print(f"  {label}: {_fmt_field(current, typ)} → {_fmt_field(nxt, typ)}")
            env[key] = nxt
            write_env(env)
            console.print("  [green]Сохранено! Перезапустите бота чтобы применить.[/green]")

            # When switching to monitoring/auto, prompt for missing URLs
            if nxt in ("monitoring", "auto"):
                g_url = (env.get("MONITORING_API_URL") or env.get("MONITORING_GAMMA_URL", "")).strip()
                w_url = env.get("MONITORING_WS_URL", "").strip()
                if not g_url or not w_url:
                    console.print()
                    console.print(
                        f"  [yellow]DATA_SOURCE={nxt} requires monitoring URLs.[/yellow]"
                    )
                    if not g_url:
                        v = console.input(
                            "  MONITORING_API_URL [dim](e.g. http://localhost:8000)[/dim]: "
                        ).strip()
                        if v:
                            env["MONITORING_API_URL"] = v
                            write_env(env)
                    if not w_url:
                        v = console.input(
                            "  MONITORING_WS_URL [dim](e.g. ws://localhost:8000/ws)[/dim]: "
                        ).strip()
                        if v:
                            env["MONITORING_WS_URL"] = v
                            write_env(env)
            time.sleep(1.0)
        else:
            new_str = console.input(f"  {label}  ({hint})\n  Новое значение: ").strip()
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
                elif typ == "url":
                    env[key] = new_str
                write_env(env)
                console.print("  [green]Сохранено![/green]")
                time.sleep(0.8)
            except ValueError:
                console.print("  [red]Неверное значение[/red]")
                time.sleep(0.8)
def _derive_api_credentials_menu(env: dict[str, str]) -> None:
    """Derive POLY_API_KEY/SECRET/PASSPHRASE from private key and save to .env."""
    console.print()
    console.rule("[bold yellow]Получение API-ключей из приватного ключа[/bold yellow]")
    console.print()
    console.print(
        "  Вызывает [bold]createOrDeriveApiKey()[/bold] на Polymarket CLOB API.\n"
        "  Один HTTP-запрос — [green]никаких ордеров не создаётся[/green].\n"
        "  Если ключи уже существуют — возвращает те же значения (idempotent).\n"
    )

    priv_key = env.get("POLYMARKET_PRIVATE_KEY", "").strip()
    if not priv_key:
        console.print(
            "[red]POLYMARKET_PRIVATE_KEY не задан.[/red]\n"
            "  Сначала введи приватный ключ кошелька (пункт 1)."
        )
        console.print()
        console.input("Нажмите Enter чтобы вернуться...")
        return

    # Показать что будет заменено
    old_key        = env.get("POLY_API_KEY", "").strip()
    old_secret     = env.get("POLY_API_SECRET", "").strip()
    old_passphrase = env.get("POLY_API_PASSPHRASE", "").strip()
    has_old = old_key or old_secret or old_passphrase

    def _mask(v: str) -> str:
        if not v:
            return "[dim]не задан[/dim]"
        return f"[yellow]{v[:6]}...{v[-4:]}[/yellow]"

    if has_old:
        console.print("  [yellow]Текущие ключи (будут заменены):[/yellow]")
        console.print(f"    POLY_API_KEY:        {_mask(old_key)}")
        console.print(f"    POLY_API_SECRET:     {_mask(old_secret)}")
        console.print(f"    POLY_API_PASSPHRASE: {_mask(old_passphrase)}")
        console.print()

    confirm = console.input("  Получить и заменить ключи? ([bold]y[/bold]/n): ").strip().lower()
    if confirm not in ("y", "yes", ""):
        console.print("  [dim]Отменено.[/dim]")
        time.sleep(0.8)
        return

    console.print()
    console.print("  [dim]Обращаемся к Polymarket CLOB API...[/dim]")

    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from bot.config import Config
        from bot.wallet import WalletError, derive_api_credentials

        cfg = Config.from_env()
        result = derive_api_credentials(cfg)

    except Exception as exc:
        console.print(f"\n  [red]Ошибка: {exc}[/red]")
        console.print("  [dim]Проверь приватный ключ и доступность интернета.[/dim]")
        console.print()
        console.input("Нажмите Enter чтобы вернуться...")
        return

    api_key        = result["api_key"]
    api_secret     = result["api_secret"]
    api_passphrase = result["api_passphrase"]

    console.print()
    console.print("  [green bold]Успешно! Новые ключи:[/green bold]")
    console.print(f"  POLY_API_KEY:        {api_key[:8]}...{api_key[-4:]}")
    console.print(f"  POLY_API_SECRET:     {api_secret[:6]}...{api_secret[-4:]}")
    console.print(f"  POLY_API_PASSPHRASE: {api_passphrase[:6]}...{api_passphrase[-4:]}")
    console.print()

    save = console.input("  Записать в .env (заменить старые)? ([bold]y[/bold]/n): ").strip().lower()
    if save in ("y", "yes", ""):
        env["POLY_API_KEY"]        = api_key
        env["POLY_API_SECRET"]     = api_secret
        env["POLY_API_PASSPHRASE"] = api_passphrase
        write_env(env)
        console.print("  [green]Ключи заменены и сохранены в .env![/green]")
    else:
        console.print("  [dim]Не сохранено. Скопируй значения вручную.[/dim]")

    console.print()
    console.input("Нажмите Enter чтобы вернуться...")


def menu_api_keys() -> None:
    console.clear()
    console.rule("[bold cyan]API ключи и кошелёк[/bold cyan]")
    console.print()
    console.print("[dim]Нужны только для LIVE торговли. Для paper/dry можно пропустить.[/dim]")
    console.print()

    env = read_env()

    def mask(val: str) -> str:
        if not val:
            return "[red]не задан[/]"
        if len(val) <= 8:
            return "[green]задан[/]"
        return f"[green]{val[:6]}...{val[-4:]}[/]"

    # Показать текущее состояние
    t = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
    t.add_column(style="dim", min_width=30)
    t.add_column()

    t.add_row("POLYMARKET_PRIVATE_KEY",   mask(env.get("POLYMARKET_PRIVATE_KEY", "")))
    t.add_row("POLYMARKET_WALLET_ADDRESS",mask(env.get("POLYMARKET_WALLET_ADDRESS", "")))
    t.add_row("POLYMARKET_PROXY_ADDRESS", mask(env.get("POLYMARKET_PROXY_ADDRESS", "")))
    t.add_row("POLY_API_KEY (legacy)",    mask(env.get("POLY_API_KEY", "")))
    t.add_row("POLY_API_SECRET (legacy)", mask(env.get("POLY_API_SECRET", "")))

    console.print(Panel(t, title="[bold]Текущие ключи[/]", box=box.ROUNDED, border_style="yellow"))
    console.print()

    KEYS = [
        ("POLYMARKET_PRIVATE_KEY",    "Приватный ключ кошелька (0x...)"),
        ("POLYMARKET_WALLET_ADDRESS", "Адрес кошелька (0x...)"),
        ("POLYMARKET_PROXY_ADDRESS",  "Proxy/Safe адрес (0x..., если используется)"),
        ("POLY_API_KEY",              "Polymarket API Key (legacy)"),
        ("POLY_API_SECRET",           "Polymarket API Secret (legacy)"),
        ("POLY_API_PASSPHRASE",       "Polymarket API Passphrase (legacy)"),
    ]

    for i, (key, label) in enumerate(KEYS, 1):
        console.print(f"  [cyan][{i}][/cyan] {label}")
    console.print(f"  [cyan][{len(KEYS)+1}][/cyan] Ввести wallet-ключи заново (1-3)")
    console.print(f"  [cyan][D][/cyan] [bold]Получить API-ключи автоматически[/bold] (из приватного ключа)")
    console.print(f"  [cyan][0][/cyan] Назад")
    console.print()

    choice = console.input("[cyan]> [/cyan]").strip()

    if choice == "0" or not choice:
        return
    elif choice.upper() == "D":
        _derive_api_credentials_menu(env)
    elif choice == str(len(KEYS) + 1):
        # Re-enter wallet keys (first 3)
        console.print()
        console.print("[yellow]Введи wallet ключи (Enter = оставить без изменений):[/yellow]")
        console.print()
        for key, label in KEYS[:3]:
            new_val = console.input(f"  {label}: ").strip()
            if new_val:
                env[key] = new_val
        write_env(env)
        console.print()
        console.print("[green]Ключи сохранены![/green]")
        time.sleep(1.5)
    else:
        try:
            idx = int(choice) - 1
            if not (0 <= idx < len(KEYS)):
                raise ValueError
            key, label = KEYS[idx]
            new_val = console.input(f"  {label}: ").strip()
            if new_val:
                env[key] = new_val
                write_env(env)
                console.print("  [green]Сохранено![/green]")
                time.sleep(0.8)
        except ValueError:
            console.print("[red]Неверный выбор[/red]")
            time.sleep(0.8)


def menu_view_logs() -> None:
    console.clear()
    console.rule("[bold cyan]Логи бота[/bold cyan]")
    console.print()

    log_path = Path(__file__).parent / "bot.log"
    if not log_path.exists():
        console.print("[dim]Файл bot.log не найден — бот ещё не запускался[/dim]")
        console.print()
        console.input("Нажмите Enter чтобы вернуться...")
        return

    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    last_lines = lines[-40:]  # последние 40 строк

    for line in last_lines:
        if "ERROR" in line:
            console.print(f"[red]{line}[/]")
        elif "WARNING" in line:
            console.print(f"[yellow]{line}[/]")
        elif "INFO" in line:
            console.print(f"[dim]{line}[/]")
        else:
            console.print(line)

    console.print()
    console.print(f"[dim]Показаны последние {len(last_lines)} строк из {len(lines)} всего[/dim]")
    console.print(f"[dim]Полный файл: {log_path}[/dim]")
    console.print()
    console.input("Нажмите Enter чтобы вернуться...")


# ─── Точка входа ──────────────────────────────────────────────────────────────

def main() -> None:
    ensure_defaults()

    while True:
        choice = show_main_menu()

        if choice == "1":
            menu_launch_bot()
        elif choice == "2":
            menu_trading_settings()
        elif choice == "3":
            menu_api_keys()
        elif choice == "4":
            menu_view_logs()
        elif choice in ("5", "q", ""):
            console.clear()
            console.print("[dim]До свидания![/dim]")
            break
        else:
            console.print("[red]Неверный выбор, попробуй ещё раз[/red]")
            time.sleep(0.5)


if __name__ == "__main__":
    main()
