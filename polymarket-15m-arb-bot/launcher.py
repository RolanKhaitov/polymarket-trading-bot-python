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
        "DRY_RUN": "true",
        "MAX_POSITION_SIZE": "50.0",
        "MIN_PROFIT_PCT": "0.005",
        "MIN_LIQUIDITY_USD": "20.0",
        "MAX_DAILY_LOSS": "20.0",
        "MAX_CONCURRENT_POSITIONS": "5",
        "MIN_SECONDS_TO_EXPIRY": "30",
        "MAX_SECONDS_TO_EXPIRY": "1000",
        "MARKET_REFRESH_INTERVAL": "60",
        "LOG_LEVEL": "INFO",
    }
    write_env(defaults)


# ─── Статус-панель ────────────────────────────────────────────────────────────

def status_panel() -> Panel:
    env = read_env()

    dry_run = env.get("DRY_RUN", "true").lower() == "true"
    has_key = bool(env.get("PRIVATE_KEY", "").strip())
    has_api = bool(env.get("POLY_API_KEY", "").strip())

    mode_text = "[green]DRY RUN (симуляция)[/]" if dry_run else "[red bold]LIVE (реальные деньги)[/]"
    api_text  = "[green]настроены[/]" if (has_key and has_api) else "[yellow]не настроены[/]"
    env_text  = "[green]найден[/]" if ENV_PATH.exists() else "[red]не найден[/]"

    min_profit = float(env.get("MIN_PROFIT_PCT", "0.005")) * 100
    max_pos    = env.get("MAX_POSITION_SIZE", "50")
    max_loss   = env.get("MAX_DAILY_LOSS", "20")

    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", min_width=20)
    t.add_column()

    t.add_row("Файл настроек:", env_text)
    t.add_row("Режим:", mode_text)
    t.add_row("API ключи:", api_text)
    t.add_row("Мин. прибыль:", f"{min_profit:.1f}%")
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


def menu_trading_settings() -> None:
    FIELDS = [
        ("MIN_PROFIT_PCT",           "Мин. прибыль %",            "pct",  "2.0 = 2% (рекомендуется 1-3%)"),
        ("MAX_POSITION_SIZE",        "Макс. позиция",             "usd",  "USD на один рынок"),
        ("MIN_LIQUIDITY_USD",        "Мин. ликвидность",          "usd",  "USD на каждой стороне рынка"),
        ("MAX_DAILY_LOSS",           "Макс. дневной убыток",      "usd",  "бот остановится при достижении"),
        ("MAX_CONCURRENT_POSITIONS", "Макс. позиций одновременно","int",  "рекомендуется 3-10"),
        ("MIN_SECONDS_TO_EXPIRY",    "Мин. секунд до закрытия",   "sec",  "не входить если < N сек (30)"),
        ("MAX_SECONDS_TO_EXPIRY",    "Макс. секунд до закрытия",  "sec",  "не входить если > N сек (1000 = ~16 мин)"),
        ("MARKET_REFRESH_INTERVAL",  "Обновление списка рынков",  "sec",  "каждые N секунд (60)"),
        ("DRY_RUN",                  "Режим (DRY RUN / LIVE)",    "bool", "true = симуляция, false = реальная торговля"),
    ]

    while True:
        env = read_env()
        console.clear()
        console.rule("[bold cyan]Торговые настройки[/bold cyan]")
        console.print()

        t = Table(show_header=True, header_style="bold cyan", box=box.ROUNDED, padding=(0, 2))
        t.add_column("#",          min_width=3,  justify="right", style="dim")
        t.add_column("Параметр",   min_width=30)
        t.add_column("Значение",   min_width=14, justify="right")
        t.add_column("Пояснение",  style="dim")

        for i, (key, label, typ, hint) in enumerate(FIELDS, 1):
            raw = env.get(key, "")
            try:
                if typ == "bool":
                    val_str = "[green]DRY RUN[/]" if raw.lower() == "true" else "[red bold]LIVE[/]"
                elif typ == "pct":
                    val_str = f"{float(raw) * 100:.1f}%"
                elif typ == "usd":
                    val_str = f"${float(raw):.1f}"
                else:
                    val_str = raw or "—"
            except Exception:
                val_str = raw or "—"
            t.add_row(str(i), label, val_str, hint)

        console.print(t)
        console.print()
        console.print("[dim]Введите номер для изменения, или Enter чтобы вернуться[/dim]")
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

        if typ == "bool":
            is_true = current.lower() == "true"
            new_val = "false" if is_true else "true"
            status = "[green]DRY RUN (симуляция)[/]" if new_val == "true" else "[red bold]LIVE (реальные деньги!)[/]"
            console.print(f"  Режим изменён на: {status}")
            env[key] = new_val
            write_env(env)
            console.print("  [green]Сохранено![/green]")
            time.sleep(1)
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
                write_env(env)
                console.print("  [green]Сохранено![/green]")
                time.sleep(0.8)
            except ValueError:
                console.print("  [red]Неверное значение[/red]")
                time.sleep(0.8)


def menu_api_keys() -> None:
    console.clear()
    console.rule("[bold cyan]API ключи и кошелёк[/bold cyan]")
    console.print()
    console.print("[dim]Нужны только для LIVE торговли. Для dry-run можно пропустить.[/dim]")
    console.print("[dim]Ключи получить: polymarket.com → Account → API Keys[/dim]")
    console.print()

    env = read_env()

    # Показать текущее состояние
    t = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
    t.add_column(style="dim", min_width=24)
    t.add_column()

    def mask(val: str) -> str:
        if not val:
            return "[red]не задан[/]"
        if len(val) <= 8:
            return "[green]задан[/]"
        return f"[green]{val[:6]}...{val[-4:]}[/]"

    t.add_row("PRIVATE_KEY",         mask(env.get("PRIVATE_KEY", "")))
    t.add_row("WALLET_ADDRESS",      mask(env.get("WALLET_ADDRESS", "")))
    t.add_row("POLY_API_KEY",        mask(env.get("POLY_API_KEY", "")))
    t.add_row("POLY_API_SECRET",     mask(env.get("POLY_API_SECRET", "")))
    t.add_row("POLY_API_PASSPHRASE", mask(env.get("POLY_API_PASSPHRASE", "")))

    console.print(Panel(t, title="[bold]Текущие ключи[/]", box=box.ROUNDED, border_style="yellow"))
    console.print()

    KEYS = [
        ("PRIVATE_KEY",         "Приватный ключ кошелька (0x...)"),
        ("WALLET_ADDRESS",      "Адрес кошелька (0x...)"),
        ("POLY_API_KEY",        "Polymarket API Key"),
        ("POLY_API_SECRET",     "Polymarket API Secret"),
        ("POLY_API_PASSPHRASE", "Polymarket API Passphrase"),
    ]

    for i, (key, label) in enumerate(KEYS, 1):
        console.print(f"  [cyan][{i}][/cyan] {label}")
    console.print(f"  [cyan][6][/cyan] Ввести все ключи заново")
    console.print(f"  [cyan][0][/cyan] Назад")
    console.print()

    choice = console.input("[cyan]> [/cyan]").strip()

    if choice == "0" or not choice:
        return
    elif choice == "6":
        console.print()
        console.print("[yellow]Введи ключи (Enter = оставить без изменений):[/yellow]")
        console.print()
        for key, label in KEYS:
            current = env.get(key, "")
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
