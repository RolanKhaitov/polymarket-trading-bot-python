#!/usr/bin/env python3
"""
Polymarket Monitoring Service — entrypoint.

Запуск (в отдельном терминале, ПЕРЕД ботом):
    python run_monitoring.py
    python run_monitoring.py --port 8001

Сервис поднимает локальный HTTP+WebSocket прокси между Polymarket и ботом:
    - GET  http://localhost:8000/health   → статус сервиса
    - GET  http://localhost:8000/markets  → список рынков (Gamma API формат)
    - GET  http://localhost:8000/stats    → детальные метрики
    - WS   ws://localhost:8000/ws         → relay price updates → бот

После запуска сервиса — запускай бот с DATA_SOURCE=monitoring.

Переменные окружения (опционально):
    POLYMARKET_GAMMA_URL  (default: https://gamma-api.polymarket.com)
    POLYMARKET_WS_URL     (default: wss://ws-subscriptions-clob.polymarket.com/ws/market)
    MARKET_REFRESH_INTERVAL  (default: 60 seconds)
"""

import sys
import os

# ── Boot print BEFORE any imports that could fail ──────────────────────────
print("[BOOT] run_monitoring.py started", flush=True)
print(f"[BOOT] Python {sys.version}", flush=True)
print(f"[BOOT] Working dir: {os.getcwd()}", flush=True)

# Ensure project root is on Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load .env from the project directory
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(_env_path):
        load_dotenv(_env_path)
        print(f"[BOOT] Loaded .env from {_env_path}", flush=True)
    else:
        print(f"[BOOT] No .env found at {_env_path} (using system env)", flush=True)
except ImportError:
    print("[BOOT] python-dotenv not installed — using system env", flush=True)

print("[BOOT] Importing monitoring service...", flush=True)
try:
    import argparse
    from monitoring.service import run
    print("[BOOT] Import OK", flush=True)
except Exception as _import_err:
    import traceback
    print(f"[BOOT] IMPORT FAILED: {_import_err}", flush=True)
    traceback.print_exc()
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Polymarket Monitoring Service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_monitoring.py
  python run_monitoring.py --port 8001
  python run_monitoring.py --host 127.0.0.1 --port 8000
        """,
    )
    parser.add_argument(
        "--host",
        default=os.getenv("MONITORING_HOST", "0.0.0.0"),
        help="Bind host (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MONITORING_PORT", "8000")),
        help="Bind port (default: 8000)",
    )
    args = parser.parse_args()

    print(f"[BOOT] Starting HTTP server on {args.host}:{args.port}", flush=True)
    try:
        run(host=args.host, port=args.port)
    except KeyboardInterrupt:
        print("\n[BOOT] Monitoring service stopped by user.", flush=True)
    except OSError as e:
        import traceback
        print(f"\n[BOOT] FAILED TO BIND {args.host}:{args.port} — {e}", flush=True)
        if "10048" in str(e) or "Address already in use" in str(e):
            print(f"[BOOT] Port {args.port} is already in use. Kill the process using it or choose another port:", flush=True)
            print(f"[BOOT]   python run_monitoring.py --port 8001", flush=True)
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f"\n[BOOT] UNEXPECTED ERROR: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
