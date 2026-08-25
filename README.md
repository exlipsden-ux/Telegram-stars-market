# almaz-bot

Реферальный Telegram-бот на aiogram 3: задания, спонсоры, промокоды,
VIP-уровни, рассылки, отзывы и админ-панель прямо в чате.

## Запуск

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Настройки читаются из переменных окружения — список и описание в
[`.env.example`](.env.example). Секретов в коде нет: без `ALMAZ_BOT_TOKEN`
и `ROOT_OWNER_IDS` бот не стартует.

```bash
export ALMAZ_BOT_TOKEN=...
export ROOT_OWNER_IDS=...
.venv/bin/python main.py
```

## Что внутри

- `main.py` — весь бот: хендлеры, FSM, админ-панель, SQLite-слой, HTTP-эндпоинты
  для мини-аппа отзывов.
- `webapp/index.html` — мини-апп с опубликованными отзывами.

Админ-панель открывается командой `/admin` у владельца из `ROOT_OWNER_IDS`.
Со-владельцам и саб-админам доступ выдаётся оттуда же, вход для них можно
закрыть паролем (`/setpass`).
