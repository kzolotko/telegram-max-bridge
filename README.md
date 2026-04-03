# Telegram ↔ MAX Bridge

Двунаправленный мост между Telegram и [MAX](https://max.ru) (бывший VK Teams / MyTeam). Сообщения автоматически зеркалируются из одного мессенджера в другой и обратно.

## Как это работает

Мост использует **реальные пользовательские аккаунты** (не ботов):
- **Telegram** — MTProto через [Pyrogram](https://docs.pyrogram.org/)
- **MAX** — нативный TCP/SSL бинарный протокол (device_type=DESKTOP)

В конфиге отдельно описываются пользователи (`users`) и мосты (`bridges`). Каждый мост связывает пару чатов и указывает список пользователей, чьи аккаунты выполняют зеркалирование:

```
Telegram-группа                        MAX-чат
      │                                    │
      │  Кто-то написал "Привет"            │
      ▼                                    │
[TG-аккаунт пользователя]                  │
      │  слушает + пересылает              │
      ▼                                    │
  [Bridge]  ──────────────────►  [MAX-аккаунт пользователя]
                                           │  отправляет "[Имя]: Привет"
                                           ▼
                                       MAX-чат

(и в обратную сторону то же самое)
```

### Несколько пользователей на один чат

Когда для одной пары чатов указано несколько пользователей:

- **Первый** пользователь в конфиге (**primary**) слушает чат на обеих сторонах.
- Когда **настроенный** пользователь отправляет сообщение — мост пересылает через **его** аккаунт на другой стороне (**без** `[Имя]:` префикса — авторство сохраняется нативно).
- Когда пишет **ненастроенный** пользователь — сообщение идёт через primary-аккаунт с `[Имя]:` префиксом.
- Каждое сообщение пересылается **ровно один раз** — дубли исключены.

**Поддерживается:** текст, фото, видео, файлы, аудио, голосовые, стикеры, ответы на сообщения (reply), редактирование, удаление.

### Admin-бот (удалённое управление)

Отдельный Telegram-бот для управления бриджем без SSH:

- `/status` — аптайм, кол-во мостов и пользователей, состояние подключений
- `/bridges`, `/users` — просмотр конфигурации
- `/pause` / `/resume` — приостановка пересылки (глобально или по мосту)
- `/addbridge`, `/rmbridge`, `/adduser`, `/rmuser` — управление конфигурацией
- `/authmax`, `/authtg` — авторизация аккаунтов через бота
- `/logs` — последние логи
- `/restart` — перезапуск бриджа
- `/config` — выгрузка текущего config.yaml

Настройка: создать бота через @BotFather, добавить `admin_bot_token` и `admin_ids` в `config/credentials.yaml`.

### MAX→TG бот (DM-бридж + групповая пересылка)

Опциональный Telegram-бот выполняет две функции:

**1. DM-бридж** — пересылает личные сообщения из MAX в Telegram:

```
Кто-то пишет вам в MAX DM
      │
      ▼
[MAX listener обнаруживает DM]
      │
      ▼
TG бот → отправляет вам: "[Имя Фамилия]: текст"
      │
      ▼
Вы reply'ите боту → ответ уходит от вашего MAX-аккаунта
```

**2. Групповая пересылка** — сообщения от **несконфигурированных** пользователей MAX отправляются через бота (вместо primary-аккаунта), чтобы визуально отличать чужие сообщения от своих.

- Один бот обслуживает **всех** пользователей из `bridges`
- DM: текст, фото, файлы, редактирование, удаление, подсказка при отправке не-reply
- Групповая пересылка: автоопределение доступных групп при старте
- Настройка: создать бота через @BotFather, добавить `max2tg_bridge_bot_token` в `config/credentials.yaml`, **добавить бота в нужные TG-группы**

---

## Требования

| Компонент | Версия              |
|-----------|---------------------|
| Python    | 3.12+               |
| Docker    | 20.10+ (опционально)|

---

## Быстрый старт

### 1. Клонировать репозиторий

```bash
git clone git@github.com:kzolotko/telegram-max-bridge.git
cd telegram-max-bridge
pip install -r requirements.txt
```

### 2. Настройка (интерактивный мастер)

```bash
./bridge.sh setup
```

Мастер проведёт по всем шагам:
1. Ввод Telegram API credentials (получите на [my.telegram.org](https://my.telegram.org) → API development tools)
2. Добавление пользователей: аутентификация TG (телефон + код) и MAX (телефон + SMS)
3. Создание мостов: выбор TG-группы и MAX-чата из списка, назначение пользователей
4. Запись `config/credentials.yaml` и `config/config.yaml`

Доступны отдельные режимы:

```bash
./bridge.sh setup credentials   # только API credentials (один раз при первом запуске)
./bridge.sh setup users         # управление пользователями (добавить/удалить/переавторизовать)
./bridge.sh setup bridges       # управление мостами (добавить/удалить, назначить пользователей)
./bridge.sh setup migrate       # конвертация старого формата конфига в новый
```

### 3. Запуск

```bash
./bridge.sh start
```

> После `setup` авторизация уже выполнена — дополнительно запускать `./bridge.sh auth` не нужно.

### 4. MAX→TG бот (опционально)

```bash
# 1. Создать бота через @BotFather в Telegram, скопировать токен
# 2. Каждый пользователь отправляет боту /start (для DM-бриджа)
# 3. Добавить бота в нужные TG-группы (для групповой пересылки)
# 4. Добавить в config/credentials.yaml:
```

```yaml
max2tg_bridge_bot_token: "123456789:ABCdef..."
```

Пользователи берутся автоматически из секции `users` — дополнительная настройка не нужна. Бот при старте проверяет, в каких группах он состоит, и автоматически включает пересылку.

---

## Docker

### Структура файлов

```
telegram-max-bridge/
├── config/
│   ├── credentials.yaml  # API credentials + bot tokens (не в репозитории)
│   └── config.yaml       # конфигурация чатов (не в репозитории)
├── sessions/             # файлы сессий + SQLite БД (создаются при авторизации)
├── docker-compose.yml
└── Dockerfile
```

### Первый запуск

```bash
git clone git@github.com:kzolotko/telegram-max-bridge.git
cd telegram-max-bridge

# Настройка (интерактивно — нужен ввод с клавиатуры)
pip install -r requirements.txt
./bridge.sh setup

# Собрать образ и запустить в фоне
./bridge.sh docker up
```

> **Если Python нет на сервере** — настройте и авторизуйтесь локально, затем скопируйте файлы:
> ```bash
> scp -r config/ user@server:/path/to/telegram-max-bridge/
> scp -r sessions/ user@server:/path/to/telegram-max-bridge/
> ```

### Команды Docker

```bash
./bridge.sh docker up        # собрать образ и запустить в фоне
./bridge.sh docker down      # остановить
./bridge.sh docker restart   # перезапуск (после изменения конфига)
./bridge.sh docker logs      # логи в реальном времени
./bridge.sh docker status    # статус контейнера
./bridge.sh docker build     # только пересобрать образ
```

> **Важно:** `setup` и `auth` всегда запускаются **локально** (`./bridge.sh setup`, `./bridge.sh auth`) — они интерактивны и требуют ввода с клавиатуры. Docker используется только для запуска самого бриджа.

### Production-настройки

Docker Compose уже настроен для production:

| Параметр | Значение | Назначение |
|----------|----------|------------|
| `restart` | `unless-stopped` | Автоперезапуск при падении |
| `healthcheck` | File-based heartbeat (каждые 2 мин) + active ping | Перезапуск при зависании event loop |
| `stop_grace_period` | 15s | Время на graceful shutdown |
| `mem_limit` | 512m | Защита от утечек памяти |
| `logging` | json-file, 10m × 3 | Ротация логов |
| `security_opt` | `no-new-privileges` | Security hardening |
| `TZ` | `Europe/Moscow` | Корректные timestamps |

**Health check**: бридж пишет timestamp в `sessions/.healthcheck` каждые 2 минуты и шлёт реальный PING серверу MAX для проверки. Docker проверяет, что файл не старше 10 минут. Если event loop завис — контейнер перезапускается. Дополнительно, встроенный ping-watchdog принудительно переподключает MAX при 3+ неудачных пингах подряд (обнаружение «полумёртвых» соединений за ~90 сек вместо 15+ мин).

**Dockerfile**: multi-stage build — gcc используется только для компиляции tgcrypto, в финальный образ не попадает.

### Что монтируется в контейнер

| Путь на хосте | Путь в контейнере | Режим      |
|---------------|-------------------|------------|
| `./config/`   | `/app/config/`    | read-write |
| `./sessions/` | `/app/sessions/`  | read-write |

### Бэкап сессий

Директория `sessions/` содержит:
- `*.session` / `*.max_session` — файлы авторизации (важно — при потере нужна повторная авторизация)
- `bridge.db` — SQLite с маппингом сообщений (TTL 24ч — потеря некритична)
- `pymax/<device_id>/session.db` — внутренний кеш pymax (~12 КБ на пользователя, пересоздаётся автоматически)

```bash
# Пример бэкапа (добавить в crontab хоста):
0 3 * * * tar czf /backups/bridge-sessions-$(date +\%Y\%m\%d).tar.gz /path/to/sessions/
```

---

## Все команды

| Команда | Описание |
|---------|----------|
| `./bridge.sh start` | Запустить бридж локально |
| `./bridge.sh setup` | Полный мастер настройки |
| `./bridge.sh setup credentials` | Настроить API credentials |
| `./bridge.sh setup users` | Управление пользователями (добавить/удалить/переавторизовать) |
| `./bridge.sh setup bridges` | Управление мостами (добавить/удалить, назначить пользователей) |
| `./bridge.sh setup migrate` | Конвертация старого формата конфига в новый |
| `./bridge.sh auth` | Повторная авторизация (при истёкшей сессии или ручном изменении конфига) |
| `./bridge.sh docker up` | Собрать образ и запустить в фоне |
| `./bridge.sh docker down` | Остановить Docker |
| `./bridge.sh docker restart` | Перезапустить Docker |
| `./bridge.sh docker logs` | Логи Docker |
| `./bridge.sh docker status` | Статус контейнера |
| `./bridge.sh docker build` | Пересобрать образ |
| `./bridge.sh test-auth` | Авторизовать TG-аккаунт для E2E-тестов (один раз) |
| `./bridge.sh test` | Запустить E2E-тесты (мост должен быть запущен) |
| `./bridge.sh test -k T01` | Запустить конкретный тест-кейс |
| `./bridge.sh test -m media` | Запустить группу тестов по маркеру |
| `./bridge.sh test -m dm` | Запустить тесты DM-бриджа |

---

## Ручная настройка (без мастера)

Если интерактивный мастер не подходит — настройте всё вручную.

### Шаг 1: Telegram API credentials

1. Откройте [my.telegram.org](https://my.telegram.org) и войдите в свой аккаунт.
2. Перейдите в **API development tools**.
3. Создайте приложение (название и описание — произвольные).
4. Скопируйте **App api_id** (число) и **App api_hash** (строка из 32 символов).

Создайте `config/credentials.yaml`:

```bash
cp config/credentials.example.yaml config/credentials.yaml
nano config/credentials.yaml
```

```yaml
api_id: 12345678
api_hash: "0123456789abcdef0123456789abcdef"
```

> `api_id` и `api_hash` — общие для **всех** Telegram-аккаунтов в мосту. Создавать отдельные приложения для каждого пользователя не нужно.

### Шаг 2: Как узнать необходимые ID

#### `telegram_user_id` — ID пользователя в Telegram

Напишите боту [@userinfobot](https://t.me/userinfobot) в Telegram — он ответит вашим числовым ID.

#### `max_user_id` — ID пользователя в MAX

Запустите `./bridge.sh auth` — после авторизации MAX автоматически выводит user ID.

#### `telegram_chat_id` — ID группы в Telegram

Перешлите любое сообщение из нужной группы боту [@userinfobot](https://t.me/userinfobot). Он ответит ID исходного чата.

> Для супергрупп ID начинается с `-100`. Для обычных групп — просто отрицательное число.

#### `max_chat_id` — ID чата в MAX

Откройте нужный чат в [web.max.ru](https://web.max.ru). ID виден в URL:

```
https://web.max.ru/#/chats/@chat/-72099589405396
                                  ↑ это и есть max_chat_id
```

### Шаг 3: Заполнение config/config.yaml

```bash
cp config/config.example.yaml config/config.yaml
nano config/config.yaml
```

Минимальный конфиг (один чат, один пользователь):

```yaml
users:
  - name: "alice"
    telegram_user_id: 111111111
    max_user_id: 205940119

bridges:
  - name: "team-general"
    telegram_chat_id: -1001234567890
    max_chat_id: -72099000000001
    users: ["alice"]
```

> Подробные примеры (несколько чатов, несколько пользователей) — в `config/config.example.yaml`.

### Шаг 4: Запуск

```bash
./bridge.sh start
```

---

## Архитектура

```
src/
├── main.py              # Точка входа, инициализация, health check heartbeat
├── config.py            # Загрузка config/credentials.yaml + config/config.yaml, ConfigLookup
├── types.py             # Датаклассы: AppConfig, BridgeEntry, UserMapping, BridgeEvent, MediaInfo
├── auth.py              # Интерактивная авторизация аккаунтов (по конфигу)
├── setup.py             # Интерактивный мастер настройки (credentials + users + bridges)
├── message_store.py     # SQLite-backed маппинг ID сообщений (TTL 24h, periodic VACUUM)
├── dm_bridge.py         # DM-бридж: MAX DMs ↔ TG бот + групповая пересылка от несконфигурированных пользователей
├── dm_store.py          # Маппинг bot_msg_id → MAX DM контекст (для reply routing)
├── admin_bot.py         # Telegram-бот удалённого управления (status, config, auth, pause)
├── bridge_state.py      # Глобальная/per-bridge пауза пересылки
├── log_buffer.py        # In-memory ring buffer логов (для /logs в admin bot)
├── bridge/
│   ├── bridge.py        # Роутинг событий, sender matching, отправка зеркал
│   ├── mirror_tracker.py# Трекер ID зеркал (защита от эхо-петель)
│   └── formatting.py    # Конвертация форматирования TG ↔ MAX, MIRROR_MARKER
├── telegram/
│   ├── listener.py      # Pyrogram MTProto: слушает TG-группу, album buffering, reactions
│   └── client_pool.py   # Пул Pyrogram-клиентов, по одному на пользователя
└── max/
    ├── native_client.py # Нативный TCP/SSL клиент (авторизация)
    ├── bridge_client.py # Обёртка SocketMaxClient для бриджа (реакции через raw opcodes)
    ├── listener.py      # Queue-based listener: recv → asyncio.Queue → worker task + DM detection
    ├── client_pool.py   # Пул MAX-клиентов, upload с fallback (pymax → HTTP)
    ├── session.py       # Сохранение/загрузка MAX login_token + device_id
    ├── media.py         # Upload/download медиафайлов MAX CDN
    └── _pymax_patch.py  # Runtime monkey-patch для pymax LZ4 buffer bug
```

### Поток данных

1. `TelegramListener` (Pyrogram callback) / `MaxListener` (queue-based worker) получает событие (new / edit / delete / reaction).
2. Listener находит **primary** bridge entry для этого чата через `ConfigLookup`.
3. `Bridge.handle_event` определяет направление и пробует **sender matching** — если отправитель = настроенный пользователь, используется его аккаунт на другой стороне (без префикса).
4. Если sender matching не найден — используется primary аккаунт с `[Имя]:` префиксом.
5. Медиа: скачивается с одной стороны, загружается на другую (pymax FILE_UPLOAD с HTTP fallback для TG→MAX; CDN URL или `get_file_by_id` opcode 88 для MAX→TG).
6. Зеркало отправляется; ID сохраняется в `MessageStore` для reply/edit/delete.
7. ID зеркала регистрируется в `MirrorTracker` — при повторном получении оно будет проигнорировано.

### Защита от дублей и эхо-петель

| Уровень | Механизм | Что защищает |
|---------|----------|-------------|
| Primary listener | Каждый чат слушает только один пользователь | Дубли при нескольких пользователях |
| Pyrogram MTProto | Обработчик не вызывается для собственных сообщений | Эхо на стороне TG |
| MirrorTracker | Трекер ID зеркал — `is_max_mirror` / `is_tg_mirror` | Эхо на стороне MAX (ID глобальны) |
| MIRROR_MARKER | Невидимый `\u200b` в начале TG-сообщений моста | Эхо в обычных TG-группах |

---

## Ограничения

| Функция | Статус |
|---------|--------|
| Текст | ✅ |
| Фото | ✅ |
| Видео / анимации | ✅ (включая TG ANIMATION → MAX FILE) |
| Файлы/документы | ✅ |
| Аудио | ✅ |
| Голосовые сообщения | ✅ (передаются как аудио `.ogg`) |
| Альбомы (несколько медиафайлов) | ✅ |
| Ответы (reply) | ✅ |
| Редактирование | ✅ |
| Форматирование (bold, italic, underline, strikethrough) | ✅ |
| Реакции | ✅ (требуется supergroup для TG) |
| Опросы TG→MAX | ✅ (форматируются как текст `📊 ...`) |
| Опросы MAX→TG | ➖ MAX не поддерживает polls |
| Голосовые MAX→TG | ➖ MAX не поддерживает голосовые сообщения |
| Стикеры | ⚠️ заменяются на `[Sticker]` |
| Удаление MAX→TG | ⚠️ работает для сообщений других пользователей (MAX не уведомляет об удалении собственных) |
| Удаление TG→MAX | ✅ в супергруппах, ⚠️ в обычных группах (Pyrogram не сообщает `chat_id`) |
| Code/pre/text_link форматирование | ⚠️ передаётся как plain text (MAX не поддерживает) |
| Несколько пользователей | ✅ sender routing + primary listener |
| MAX→TG бот (DM + группы) | ✅ DM: MAX DM → TG бот, ответ через reply; Группы: пересылка от несконфигурированных пользователей через бота |
| Admin-бот | ✅ удалённое управление через Telegram (status, config, pause, auth, restart) |
| Reply/edit/delete после перезапуска | ✅ SQLite-backed store (TTL 24ч) |

---

## E2E-тестирование

Полный набор автоматизированных тестов через реальные аккаунты TG и MAX.
Описание всех кейсов и их статусы — в [`tests/e2e/TEST_CASES.md`](tests/e2e/TEST_CASES.md).

### Подготовка (один раз)

```bash
# 1. Тестовые зависимости
pip install -r requirements-test.txt

# 2. Авторизовать отдельную TG-сессию для тестов
./bridge.sh test-auth

# 3. Создать конфиг тестов
cp config/e2e_config.example.yaml config/e2e_config.yaml
nano config/e2e_config.yaml   # заполнить: user_name, tg_chat_id, max_chat_id
```

### Запуск

> **Важно:** бридж должен быть запущен (`./bridge.sh start` или `./bridge.sh docker up`).

```bash
# Все тесты
./bridge.sh test

# Конкретный кейс
./bridge.sh test -k T01
./bridge.sh test -k M13

# Группа тестов по маркеру
./bridge.sh test -m text
./bridge.sh test -m formatting
./bridge.sh test -m media
./bridge.sh test -m reaction
./bridge.sh test -m edge
./bridge.sh test -m dm
```

После прогона `tests/e2e/TEST_CASES.md` автоматически обновляется со статусами и временем последнего запуска.

---

## Повторная авторизация (`./bridge.sh auth`)

После первоначального `./bridge.sh setup` сессии уже созданы. `./bridge.sh auth` нужен только в этих случаях:

**Сессия истекла или была отозвана:**
```bash
rm sessions/max_alice.max_session   # или tg_alice.session
./bridge.sh auth
```

**Перенос на новый сервер** (есть конфиг, но нет сессий):
```bash
scp -r config/ user@server:/path/to/telegram-max-bridge/
ssh user@server
cd /path/to/telegram-max-bridge
pip install -r requirements.txt
./bridge.sh auth       # создаёт сессии по существующему конфигу
./bridge.sh docker up
```

**Ручное добавление пользователя** (минуя wizard):
```bash
nano config/config.yaml   # добавили нового пользователя
./bridge.sh auth       # авторизует только тех, у кого нет сессии
```

---

## Troubleshooting

### MAX-сессия истекла

Токен MAX протухает через несколько недель неактивности:

```bash
rm sessions/max_alice.max_session
./bridge.sh auth
```

### `AuthKeyUnregistered` / Telegram-сессия недействительна

Pyrogram-сессия была завершена другим устройством:

```bash
rm sessions/tg_alice.session
./bridge.sh auth
```

### `error.limit.violate — Попробуйте позже`

MAX ограничивает частоту запросов SMS. Подождите 1–2 часа и повторите.

### Сообщения не пересылаются

- Убедитесь, что аккаунт пользователя добавлен в оба чата (TG и MAX).
- Проверьте логи: `./bridge.sh docker logs` (Docker) или `./bridge.sh start` (локально).
- MAX переподключается автоматически при разрыве — это нормально. Ping-watchdog обнаруживает «полумёртвые» соединения за ~90 сек.
