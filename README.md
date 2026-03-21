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

| Компонент | Версия       |
|-----------|--------------|
| Python    | 3.12+        |
| Docker    | 20.10+ (опционально) |

---

## Быстрый старт

### 1. Установка зависимостей

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
4. Выбор TG-группы из списка ваших чатов
5. Ввод MAX chat ID (из URL `web.max.ru`)
6. Запись `credentials.yaml` и `config.yaml`

Доступны отдельные режимы:

```bash
./bridge.sh setup credentials   # только API credentials (один раз)
./bridge.sh setup bridges       # только пользователи + чаты
```

### 3. Запуск

```bash
./bridge.sh start
```

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

# Собрать образ и запустить
docker compose up -d --build
```

> **Если Python нет на сервере** — настройте и авторизуйтесь локально, затем скопируйте файлы:
> ```bash
> scp credentials.yaml config.yaml user@server:/path/to/telegram-max-bridge/
> scp -r sessions/ user@server:/path/to/telegram-max-bridge/
> ```

### Docker-команды через скрипт

```bash
./bridge.sh docker build     # собрать образ
./bridge.sh docker up        # запустить в фоне
./bridge.sh docker down      # остановить
./bridge.sh docker logs      # логи в реальном времени
./bridge.sh docker restart   # перезапуск
```

Или напрямую через docker compose:

```bash
docker compose up -d --build   # собрать и запустить
docker compose logs -f         # логи
docker compose restart         # перезапуск
docker compose down            # остановить
```

### Что монтируется в контейнер

| Путь на хосте         | Путь в контейнере         | Режим      |
|-----------------------|--------------------------|------------|
| `./credentials.yaml`  | `/app/credentials.yaml`  | read-only  |
| `./config.yaml`       | `/app/config.yaml`       | read-only  |
| `./sessions/`         | `/app/sessions/`         | read-write |

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

### Шаг 4: Авторизация аккаунтов

```bash
./bridge.sh auth
```

Скрипт последовательно авторизует каждого пользователя из конфига:

- **Telegram**: телефон + код из SMS/приложения
- **MAX**: телефон + SMS код (через нативный TCP/SSL протокол)

После авторизации в `sessions/` появятся файлы:

```
sessions/
├── tg_alice.session        # Pyrogram-сессия
└── max_alice.max_session   # MAX-сессия (login_token + device_id)
```

> Файлы сессий содержат токены доступа к аккаунтам — **не публикуйте их**.

### Шаг 5: Запуск

```bash
./bridge.sh start
```

---

## Команды

| Команда | Описание |
|---------|----------|
| `./bridge.sh start` | Запустить бридж |
| `./bridge.sh setup` | Полный мастер настройки |
| `./bridge.sh setup credentials` | Настроить API credentials |
| `./bridge.sh setup bridges` | Настроить пользователей и чаты |
| `./bridge.sh auth` | Авторизация аккаунтов (по конфигу) |
| `./bridge.sh docker build` | Собрать Docker-образ |
| `./bridge.sh docker up` | Запустить в Docker |
| `./bridge.sh docker down` | Остановить Docker |
| `./bridge.sh docker logs` | Логи Docker |
| `./bridge.sh docker restart` | Перезапуск Docker |

---

## Архитектура

```
src/
├── main.py              # Точка входа, инициализация компонентов
├── config.py            # Загрузка credentials.yaml + config.yaml, ConfigLookup
├── types.py             # Датаклассы: AppConfig, BridgeEntry, UserMapping, BridgeEvent
├── auth.py              # Интерактивная авторизация аккаунтов (по конфигу)
├── setup.py             # Интерактивный мастер настройки (credentials + bridges)
├── message_store.py     # In-memory маппинг ID сообщений (TTL 24h)
├── bridge/
│   ├── bridge.py        # Роутинг событий, sender matching, отправка зеркал
│   ├── mirror_tracker.py# Трекер ID зеркал (защита от эхо-петель)
│   └── formatting.py    # MIRROR_MARKER, prepend_sender_name
├── telegram/
│   ├── listener.py      # Pyrogram MTProto: слушает TG-группу
│   └── client_pool.py   # Пул Pyrogram-клиентов, по одному на пользователя
└── max/
    ├── native_client.py # Нативный TCP/SSL клиент (авторизация + listener)
    ├── bridge_client.py # Обёртка SocketMaxClient для бриджа
    ├── listener.py      # Слушает MAX-чат через нативный протокол
    ├── client_pool.py   # Пул MAX-клиентов для отправки
    ├── session.py       # Сохранение/загрузка MAX login_token + device_id
    └── media.py         # Скачивание/загрузка медиафайлов MAX CDN
```

### Поток данных

1. `TelegramListener` / `MaxListener` получает событие (new / edit / delete).
2. Listener находит **primary** bridge entry для этого чата через `ConfigLookup`.
3. `Bridge.handle_event` определяет направление и пробует **sender matching** — если отправитель = настроенный пользователь, используется его аккаунт на другой стороне (без префикса).
4. Если sender matching не найден — используется primary аккаунт с `[Имя]:` префиксом.
5. Зеркало отправляется; ID нового сообщения сохраняется в `MessageStore` для последующих edit/delete/reply.
6. ID зеркала регистрируется в `MirrorTracker` — при повторном получении оно будет проигнорировано.

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
| Видео | ✅ |
| Файлы/документы | ✅ |
| Аудио | ✅ |
| Голосовые сообщения | ✅ (передаются как аудио `.ogg`) |
| Ответы (reply) | ✅ |
| Редактирование | ✅ |
| Удаление MAX→TG | ⚠️ работает для сообщений других пользователей (MAX не уведомляет об удалении собственных) |
| Удаление TG→MAX | ⚠️ работает в супергруппах (Pyrogram не сообщает `chat_id` в обычных группах) |
| Стикеры | ⚠️ заменяются на `[Sticker]` |
| Несколько пользователей | ✅ sender routing + primary listener |
| Форматирование (bold, italic) | ❌ не сохраняется |
| Реакции | ❌ |
| Опросы | ❌ |

---

## Troubleshooting

### `MAX session not found (...). Run './bridge.sh auth' first.`

Сессия не создана. Запустите авторизацию:

```bash
./bridge.sh auth
```

### MAX-сессия истекла

Токен MAX протухает через несколько недель неактивности. Удалите файл и повторите:

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

MAX ограничивает частоту запросов SMS. Подождите 5–15 минут и повторите.

### Сообщения не пересылаются

- Убедитесь, что аккаунт пользователя добавлен в оба чата (TG и MAX).
- Проверьте логи: `./bridge.sh start` или `./bridge.sh docker logs`.
- MAX переподключается автоматически при разрыве — это нормально.
