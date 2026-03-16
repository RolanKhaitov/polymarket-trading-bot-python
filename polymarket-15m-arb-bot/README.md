# Polymarket 15-min Arb Bot

Арбитражный бот для рынков **"Up or Down"** на Polymarket.

## Стратегия

Polymarket создаёт рынки типа:
> "Bitcoin Up or Down — March 17, 11:30AM–11:45AM ET"

В каждом рынке два токена: **YES** (BTC вырастет) и **NO** (BTC упадёт).

Если `YES_ask + NO_ask < $1.00` — покупаем оба. При закрытии рынка один из токенов выплатит $1.00, второй $0.00. Итого $1.00 за $0.98 потраченных = **гарантированная прибыль**.

Торговые окна: **11:20AM – 3:50PM ET** (Нью-Йорк), по будням.

---

## Быстрый старт

### 1. Установи Python
Скачай с [python.org](https://python.org) → при установке отметь **"Add to PATH"**

### 2. Клонируй репозиторий
```
git clone https://github.com/KhaitovR/polymarket-brothers.git
cd polymarket-brothers/polymarket-15m-arb-bot
```

### 3. Запусти
Двойной клик на **`LAUNCH.bat`**

При первом запуске автоматически:
- Установятся все зависимости
- Откроется мастер настройки

---

## Интерфейс

```
LAUNCH.bat → Главное меню

[1] Запустить бота       ← основной дашборд
[2] Торговые настройки   ← прибыль, позиция, лимиты
[3] API ключи            ← для live-торговли
[4] Посмотреть логи
[5] Выход
```

**В дашборде:**
- `s` — настройки
- `q` — выход

---

## Настройки

| Параметр | По умолчанию | Описание |
|---|---|---|
| `DRY_RUN` | `true` | Симуляция без реальных денег |
| `MIN_PROFIT_PCT` | `0.005` | Мин. прибыль 0.5% |
| `MAX_POSITION_SIZE` | `50.0` | USD на один рынок |
| `MIN_LIQUIDITY_USD` | `20.0` | Мин. ликвидность |
| `MAX_DAILY_LOSS` | `20.0` | Дневной лимит убытка |
| `MIN_SECONDS_TO_EXPIRY` | `30` | Не входить за < 30 сек до закрытия |
| `MAX_SECONDS_TO_EXPIRY` | `420` | Входить только в последние 7 минут |

---

## Что нужно для live-торговли

1. Аккаунт на [polymarket.com](https://polymarket.com)
2. Кошелёк с USDC на Polygon
3. API ключи: polymarket.com → Account → API Keys
4. В меню `[3] API ключи` — вставить все данные
5. В настройках `DRY_RUN = false`

> ⚠️ **Live-торговля пока в разработке.** Сначала протестируй в dry-run режиме.

---

## Дашборд

```
┌─ Polymarket 15-min Arb Bot ─── DRY RUN ─── Uptime: 00:05:12 ───────────────┐
│                                                                              │
│  Statistics           │  Active Markets (in window)                         │
│  Price updates: 2.1M  │  Market             YES    NO    Comb  Profit  Left │
│  Opportunities: 3     │  BTC Mar17 11:30   0.487  0.498  0.985 +1.52%  312s│
│  Best Comb: 0.9851    │  ETH Mar17 11:30   0.501  0.502  1.003 -0.30%  312s│
│  Near misses: 12      │                                                      │
│  Daily PnL: +$0.12    │  Recent Trades                                      │
│                       │  11:32:05  BTC Mar17 11:30  0.9851  +$0.02  (sim)  │
└──────────────────────────────────────────────────────────────────────────────┘
  q = выход  |  s = настройки  |  Торговые часы: 11:20AM – 3:50PM ET
```

**Best Comb today** — минимальный `YES+NO` за сессию. Если < 1.0 — была сделка.
**Near misses** — сколько раз Comb был < 1.005 (почти арбитраж).

---

## Файлы

```
polymarket-15m-arb-bot/
├── LAUNCH.bat      ← двойной клик для запуска
├── launcher.py     ← главное меню
├── dashboard.py    ← live дашборд
├── .env            ← твои настройки (не коммитится в git!)
├── .env.example    ← шаблон настроек
├── bot.log         ← лог работы бота
└── bot/
    ├── config.py   ← конфигурация
    ├── gamma.py    ← загрузка рынков (Gamma API)
    ├── ws_scanner.py ← цены в реальном времени (WebSocket)
    ├── analyzer.py ← поиск арбитража
    ├── executor.py ← исполнение сделок
    ├── risk.py     ← риск-менеджмент
    └── state.py    ← общее состояние
```
