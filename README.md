# Telegram ↔ MAX Bridge

Двунаправленный мост между Telegram и [MAX](https://max.ru) (бывший VK Teams / MyTeam). Сообщения автоматически зеркалируются из одного мессенджера в другой и обратно.

## Как это работает

Мост использует **реальные пользовательские аккаунты** (не ботов):
- **Telegram** — MTProto через [Pyrogram](https://docs.pyrogram.org/)
- **MAX** — нативный TCP/SSL бинарный протокол (device_type=DESKTOP)

Каждая запись в конфиге (`bridge`) связывает пару чатов и указывает, чей аккаунт выполняет зеркалирование:

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
2. Аутентификация TG-аккаунта (телефон + код)
3. Аутентификация MAX-аккаунта (телефон + SMS)
4. Выбор TG-группы и MAX-чата из списка чатов пользователя
5. Запись `credentials.yaml` и `config.yaml`

Доступны отдельные режимы:

```bash
./bridge.sh setup credentials   # только API credentials (один раз при первом запуске)
./bridge.sh setup bridges       # добавить/изменить пользователей и чаты
```

### 3. Запуск

```bash
./bridge.sh start
```

> После `setup` авторизация уже выполнена — дополнительно запускать `./bridge.sh auth` не нужно.

---

## Docker

### Структура файлов

```
telegram-max-bridge/
├── credentials.yaml     # API credentials (не в репозитории)
├── config.yaml          # конфигурация чатов (не в репозитории)
├── sessions/            # файлы сессий (создаются при авторизации)
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
> scp credentials.yaml config.yaml user@server:/path/to/telegram-max-bridge/
> scp -r sessions/ user@server:/path/to/telegram-max-bridge/
> ```

### Команды Docker

```bash
./bridge.sh docker up        # собрать образ и запустить в фоне
./bridge.sh docker down      # остановить
./bridge.sh docker restart   # перезапуск (после изменения config.yaml)
./bridge.sh docker logs      # логи в реальном времени
./bridge.sh docker status    # статус контейнера
./bridge.sh docker build     # только пересобрать образ
```

> **Важно:** `setup` и `auth` всегда запускаются **локально** (`./bridge.sh setup`, `./bridge.sh auth`) — они интерактивны и требуют ввода с клавиатуры. Docker используется только для запуска самого бриджа.

### Что монтируется в контейнер

| Путь на хосте         | Путь в контейнере        | Режим      |
|-----------------------|--------------------------|------------|
| `./credentials.yaml`  | `/app/credentials.yaml`  | read-only  |
| `./config.yaml`       | `/app/config.yaml`       | read-only  |
| `./sessions/`         | `/app/sessions/`         | read-write |

---

## Все команды

| Команда | Описание |
|---------|----------|
| `./bridge.sh start` | Запустить бридж локально |
| `./bridge.sh setup` | Полный мастер настройки |
| `./bridge.sh setup credentials` | Настроить API credentials |
| `./bridge.sh setup bridges` | Добавить/изменить пользователей и чаты |
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

---

## Ручная настройка (без мастера)

Если интерактивный мастер не подходит — настройте всё вручную.

### Шаг 1: Telegram API credentials

1. Откройте [my.telegram.org](https://my.telegram.org) и войдите в свой аккаунт.
2. Перейдите в **API development tools**.
3. Создайте приложение (название и описание — произвольные).
4. Скопируйте **App api_id** (число) и **App api_hash** (строка из 32 символов).

Создайте `credentials.yaml`:

```bash
cp credentials.example.yaml credentials.yaml
nano credentials.yaml
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

### Шаг 3: Заполнение config.yaml

```bash
cp config.example.yaml config.yaml
nano config.yaml
```

Минимальный конфиг (один чат, один пользователь):

```yaml
bridges:
  - name: "team-general"
    telegram_chat_id: -1001234567890
    max_chat_id: -72099000000001
    user:
      name: "alice"
      telegram_user_id: 111111111
      max_user_id: 205940119
```

> Подробные примеры (несколько чатов, несколько пользователей) — в `config.example.yaml`.

### Шаг 4: Запуск

```bash
./bridge.sh start
```

---

## Архитектура

```
src/
├── main.py              # Точка входа, инициализация компонентов
├── config.py            # Загрузка credentials.yaml + config.yaml, ConfigLookup
├── types.py             # Датаклассы: AppConfig, BridgeEntry, UserMapping, BridgeEvent, MediaInfo
├── auth.py              # Интерактивная авторизация аккаунтов (по конфигу)
├── setup.py             # Интерактивный мастер настройки (credentials + bridges)
├── message_store.py     # In-memory маппинг ID сообщений (TTL 24h)
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
    ├── listener.py      # Queue-based listener: recv → asyncio.Queue → worker task
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
| Reply/edit/delete после перезапуска | ⚠️ теряются (in-memory store, нет персистентности) |

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
cp tests/e2e/e2e_config.example.yaml tests/e2e/e2e_config.yaml
nano tests/e2e/e2e_config.yaml   # заполнить: user_name, tg_chat_id, max_chat_id
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

**Перенос на новый сервер** (есть config.yaml, но нет сессий):
```bash
scp credentials.yaml config.yaml user@server:/path/to/telegram-max-bridge/
ssh user@server
cd /path/to/telegram-max-bridge
pip install -r requirements.txt
./bridge.sh auth       # создаёт сессии по существующему конфигу
./bridge.sh docker up
```

**Ручное добавление пользователя в config.yaml** (минуя wizard):
```bash
nano config.yaml       # добавили нового пользователя
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
- MAX переподключается автоматически при разрыве — это нормально.
