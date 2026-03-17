#!/usr/bin/env python3
"""
Генерация Polymarket CLOB API ключей из приватного ключа кошелька.

Запуск:
    python get_api_keys.py

Скопируй результат в .env:
    POLY_API_KEY=...
    POLY_API_SECRET=...
    POLY_API_PASSPHRASE=...
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from bot.config import Config
from bot.wallet import derive_api_credentials, WalletError

def main():
    config = Config.from_env()

    if not config.polymarket_private_key:
        print("❌ POLYMARKET_PRIVATE_KEY не задан в .env")
        sys.exit(1)

    key_preview = config.polymarket_private_key[:6] + "..." + config.polymarket_private_key[-4:]
    print(f"🔑 Используем ключ: {key_preview}")
    print(f"📡 CLOB endpoint:   {config.clob_url}")
    print("⏳ Запрашиваем API credentials у Polymarket...\n")

    try:
        creds = derive_api_credentials(config)
    except WalletError as e:
        print(f"❌ Ошибка: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Неожиданная ошибка: {e}")
        sys.exit(1)

    print("✅ Готово! Добавь эти строки в .env:\n")
    print(f"POLY_API_KEY={creds['api_key']}")
    print(f"POLY_API_SECRET={creds['api_secret']}")
    print(f"POLY_API_PASSPHRASE={creds['api_passphrase']}")
    print("\n⚠️  Не передавай эти ключи никому.")

if __name__ == "__main__":
    main()
