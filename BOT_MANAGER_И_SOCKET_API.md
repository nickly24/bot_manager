# Bot Manager и Socket API — полное описание

Документ описывает папку **Bot Manager** (`bot_manager/`), все процессы, HTTP API менеджера и то, как с ним общаются бэкенд, фронтенд и CLI. Нужен для отладки и исправления багов.

---

## 1. Что такое Bot Manager

**Bot Manager** — это отдельный Flask-сервер (порт **6800** по умолчанию), который:

- Управляет жизненным циклом **бот-воркеров** (один процесс на одного пользователя).
- Принимает команды по **HTTP API** (старт, стоп, рестарт, закрыть позиции).
- Запускает воркеры через `subprocess.Popen`, следит за их здоровьем, пишет heartbeat в MySQL.

С ним общаются:

| Кто | Как | Зачем |
|-----|-----|--------|
| **Основной бэкенд** (Flask :8000) | HTTP-запросы на :6800 с заголовком `X-Manager-Key` | Старт/стоп бота, статус, логи по запросу пользователя |
| **CLI** (`python -m bot_manager.cli`) | Те же HTTP-запросы из терминала | Ручное управление и мониторинг |
| **Напрямую** (curl, Postman) | HTTP на :6800 с тем же ключом | Отладка |

Бот Manager **не** общается с фронтендом напрямую. Фронт ходит только в бэкенд (:8000); бэкенд уже решает, когда дергать менеджер.

---

## 2. Структура папки `bot_manager/`

```
bot_manager/
├── app.py              # Точка входа: запуск Flask-сервера
├── server.py           # Flask HTTP API (:6800) — обёртка над BotManager
├── manager.py          # Класс BotManager — ядро (старт/стоп/health-check/recovery)
├── bot_worker.py       # Точка входа одного воркера (запускается менеджером)
├── cli.py              # CLI-утилита (status, workers, start, stop, restart, close, logs, shutdown)
├── config.py           # Конфиг (порт, MySQL, таймауты, OKX demo)
├── models.py           # WorkerInfo (dataclass для процесса воркера)
├── db/
│   ├── connection.py   # Пул подключений к MySQL
│   └── queries.py     # Все SQL-запросы (bot_commands, bot_state, events_log и т.д.)
├── trading/
│   ├── engine.py       # TradingEngine — цикл бота: OKX WebSocket → спред → сделки
│   ├── okx_client.py   # REST + WebSocket к OKX
│   ├── spread.py       # Расчёт спреда по корзинам
│   └── position.py     # Управление позицией (вход/выход, DCA)
└── logs/               # Логи воркеров: worker_<user_id>.log
```

---

## 3. HTTP API Bot Manager (порт 6800)

Все запросы к менеджеру (кроме `GET /` и `GET /api/health`) требуют заголовок:

- **`X-Manager-Key: <MANAGER_SECRET>`** — секрет из `.env` менеджера. Без него — 401.

Формат ответов:

- Успех: `{"ok": true, "data": <...>}`
- Ошибка: `{"ok": false, "error": "текст"}`

### 3.1. Без авторизации

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/` | Информация о сервисе и ссылка на health |
| GET | `/api/health` | Статус менеджера, pid, краткий обзор воркеров (`workers_total`, `workers_alive`) |

### 3.2. С заголовком X-Manager-Key

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/workers` | Список всех воркеров (user_id, pid, alive, uptime, actual_state, spread, pnl, position_open и т.д.) |
| GET | `/api/workers/<user_id>` | Детальный статус одного воркера: `WorkerInfo` + `db_state` из `bot_state` (если worker_pid совпадает с текущим) |
| POST | `/api/workers/<user_id>/start` | Запустить бота для пользователя |
| POST | `/api/workers/<user_id>/stop` | Остановить бота |
| POST | `/api/workers/<user_id>/restart` | Рестарт (stop + пауза 2 сек + start) |
| POST | `/api/workers/<user_id>/close-positions` | Отправить воркеру SIGUSR1 — закрыть все позиции |
| GET | `/api/logs/<user_id>?limit=50` | Последние события из `events_log` для user_id |
| POST | `/api/shutdown` | Мягкое завершение менеджера и всех воркеров |

### 3.3. Важные детали ответов

- **GET /api/workers/<user_id>** возвращает объект с полями:
  - из **WorkerInfo**: `user_id`, `pid`, `alive`, `started_at`, `restart_count`, `uptime_seconds`
  - **db_state** — только если в БД `worker_pid` совпадает с текущим pid воркера (иначе `db_state: null`, чтобы не отдавать устаревшее состояние после рестарта).
- **db_state** — это строка из таблицы `bot_state`: спред, PnL, баланс, позиция (open/closed), корзины, котировки и т.д. Её пишет сам воркер раз в несколько секунд.

---

## 4. Как основной бэкенд (Flask :8000) общается с Bot Manager

Бэкенд хранит в конфиге:

- `MANAGER_URL` — например `http://127.0.0.1:6800`
- `MANAGER_API_KEY` — тот же секрет, что и `MANAGER_SECRET` у менеджера

При каждом запросе к менеджеру бэкенд подставляет заголовок **`X-Manager-Key: <MANAGER_API_KEY>`**.

### 4.1. Прокси-маршруты бэкенда (то, что вызывает фронт и приложение)

| Метод бэкенда | Что делает | Вызов к менеджеру |
|---------------|------------|-------------------|
| GET `/api/bot/status` | Вернуть статус бота текущего пользователя | GET `{MANAGER_URL}/api/workers/{user_id}`; ответ передаётся как есть (с Cache-Control: no-store) |
| POST `/api/bot/start` | Запустить бота | POST `.../api/workers/{user_id}/start`, затем polling по БД до `actual_state == 'running'` (до 15 с) |
| POST `/api/bot/stop` | Остановить бота | POST `.../api/workers/{user_id}/stop`, затем опрос GET `.../api/workers/{user_id}` до `alive === false` (до 5 с) |
| POST `/api/bot/close-position` | Закрыть позиции | POST `.../api/workers/{user_id}/close-positions`, при открытой позиции — polling по `position_open == 0` (до 25 с) |
| GET `/api/bot/logs?limit=50` | Логи бота | GET `.../api/logs/{user_id}?limit=...` |

То есть **типы запросов к менеджеру** со стороны бэкенда — это только те, что перечислены в разделе 3: GET workers (список/один), POST start/stop/restart/close-positions, GET logs.

---

## 5. Socket API (фронтенд ↔ бэкенд)

Фронтенд ожидает **Socket.IO** для живого обновления статуса бота.

- Подключение: к **тому же хосту, что и REST API** — `NEXT_PUBLIC_API_URL` (например `http://localhost:8000`).
- После `connect` клиент отправляет событие **`auth`** с payload `{ token: "<JWT access_token>" }`.
- Сервер должен (по задумке фронта):
  - Проверить токен, привязать сокет к user_id.
  - Отправить клиенту событие **`authenticated`**.
  - Периодически или по запросу отправлять событие **`status`** с объектом статуса бота (тот же формат, что и у GET `/api/bot/status`: `alive`, `db_state`, `pid`, `uptime_seconds`, `user_id` и т.д.).

В коде бэкенда (Flask :8000) **нет** Flask-SocketIO или другого Socket.IO сервера: в `back/` только REST. Поэтому сейчас реальное получение статуса на фронте идёт через **REST**: вызов `GET /api/bot/status` (например, при `refresh()` или при загрузке дашборда). Если позже добавите Socket.IO на бэкенд, он может запрашивать статус у Bot Manager (GET `/api/workers/<user_id>`) и слать его в сокет событием `status` — тогда фронт будет получать те же данные в реальном времени.

Кратко:

- **Типы запросов по Socket** со стороны фронта: один — **`auth`** (с токеном).
- **События от сервера**, которые слушает фронт: **`status`** (данные статуса бота), **`authenticated`**, **`error`**.

---

## 6. Процессы внутри Bot Manager

### 6.1. Процесс менеджера (server.py + manager.py)

- При старте создаётся один экземпляр **BotManager**, вызывается **recover()** (запуск воркеров для всех `desired_state='running'`), стартует фоновый поток **health-check + heartbeat**.
- В цикле раз в `HEALTH_CHECK_INTERVAL` сек:
  - **Health-check**: для каждого воркера проверяется `alive`; если процесс умер — лог, событие в `events_log`, попытка рестарта с учётом лимита перезапусков; если воркер жив, но в БД давно не обновлялся (`updated_at` старше `WORKER_HANG_TIMEOUT`) — воркер убивается и перезапускается.
  - **Heartbeat**: запись в `manager_heartbeat` (pid менеджера, количество живых воркеров).

### 6.2. Процесс воркера (bot_worker.py)

- Запуск: `python bot_worker.py --user-id <user_id>` (из менеджера через `Popen`).
- Загружает конфиг и ключи из БД, поднимает **TradingEngine**, вешает обработчики сигналов:
  - **SIGTERM** — сохранить состояние, выход.
  - **SIGUSR1** — закрыть все позиции (флаг в движке).
- В движке: подключение к **OKX WebSocket** (тикеры), расчёт спреда, решение о входе/выходе, периодическая запись в **bot_state** и логи (spread_log, chart_spread_points, events_log и т.д.).

То есть **Socket API** в смысле «сокеты» здесь два разных контура:

1. **Фронт ↔ бэкенд**: Socket.IO (порт 8000) — в коде не реализован, фронт использует REST.
2. **Воркер ↔ OKX**: WebSocket к бирже — реализован в `trading/okx_client.py` и используется в `trading/engine.py`.

---

## 7. Цепочка данных статуса (для отладки багов)

1. **Воркер** раз в ~2–3 сек пишет в MySQL таблицу **bot_state** (спред, PnL, позиция, корзины, pid и т.д.).
2. **Менеджер** при GET `/api/workers/<user_id>` собирает ответ: свои данные по процессу (WorkerInfo) + одна строка из **bot_state** для этого user_id, причём **db_state** подставляется только если `bot_state.worker_pid == текущий pid воркера** (чтобы не отдавать старый state после рестарта).
3. **Бэкенд** при GET `/api/bot/status` просто вызывает GET менеджера и возвращает ответ клиенту.
4. **Фронт** получает статус либо по REST (GET `/api/bot/status`), либо (когда будет реализован) по Socket.IO событию `status`.

Если баг связан с «не тем» или устаревшим статусом — смотреть по цепочке: запись в `bot_state` воркером → логика `get_worker_status` в менеджере (pid) → кэши/заголовки на бэкенде → как часто фронт дергает status/refresh.

---

## 8. Конфигурация (кратко)

- **bot_manager**: `.env` в папке `bot_manager/` — `MANAGER_HOST`, `MANAGER_PORT` (6800), `MANAGER_SECRET`, MySQL, `HEALTH_CHECK_INTERVAL`, `WORKER_HANG_TIMEOUT`, `WORKER_STOP_TIMEOUT`, `MAX_RESTARTS_PER_WINDOW`, `RESTART_WINDOW_SECONDS`, `OKX_DEMO` и т.д.
- **back**: `.env` в папке `back/` — `MANAGER_URL`, `MANAGER_API_KEY` (должны соответствовать менеджеру), порт 8000, БД, JWT и т.д.

Если менеджер не поднимается или бэкенд получает 401/503 — проверить совпадение `MANAGER_API_KEY` (back) и `MANAGER_SECRET` (bot_manager), а также что менеджер слушает на том же хосте/порту, что в `MANAGER_URL`.

---

## 9. Итог: типы запросов и кто с кем говорит

| Откуда | Куда | Тип | Запросы / события |
|--------|-----|-----|-------------------|
| Бэкенд (:8000) | Bot Manager (:6800) | HTTP | GET /api/workers, GET /api/workers/<id>, POST start/stop/restart/close-positions, GET /api/logs/<id> |
| CLI | Bot Manager (:6800) | HTTP | Те же пути, те же заголовки X-Manager-Key |
| Фронт | Бэкенд (:8000) | REST | GET /api/bot/status, POST /api/bot/start, POST /api/bot/stop, POST /api/bot/close-position, GET /api/bot/logs |
| Фронт | Бэкенд (:8000) | Socket.IO (ожидаемый) | emit `auth` с token; слушает `status`, `authenticated`, `error` |
| Воркер | OKX | WebSocket | Подписка на тикеры, обновление цен и расчёт спреда |

Этого достаточно, чтобы уверенно искать баг: понимать, какой запрос куда идёт, кто подставляет `db_state`, и где может теряться или устаревать информация о статусе бота.
