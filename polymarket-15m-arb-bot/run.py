#!/usr/bin/env python3
"""
Точка входа в Polymarket 15-min Arbitrage Bot.

Запуск:
    python run.py

Или с явным указанием режима:
    DRY_RUN=true python run.py
    DRY_RUN=false python run.py  # live торговля (требует credentials)
"""

import asyncio
import logging
import signal
import sys

from bot.config import Config
from bot.main import ArbitrageBot


def setup_logging(level: str) -> None:
    import io
    # Используем UTF-8 для Windows терминала
    stream = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(stream)],
    )
    # Заглушить лишние логи от aiohttp
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def main() -> None:
    config = Config.from_env()
    setup_logging(config.log_level)

    bot = ArbitrageBot(config)

    # Graceful shutdown по Ctrl+C
    loop = asyncio.get_running_loop()

    def handle_shutdown(sig):
        print(f"\nReceived {sig.name}, shutting down...")
        asyncio.create_task(bot.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_shutdown, sig)
        except NotImplementedError:
            # Windows не поддерживает add_signal_handler
            pass

    try:
        await bot.start()
    except KeyboardInterrupt:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
