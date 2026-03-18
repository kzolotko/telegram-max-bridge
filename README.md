# Telegram ↔ MAX Bridge

Двунаправленный мост между Telegram и [MAX](https://max.ru) (бывший VK Teams / MyTeam). Сообщения автоматически зеркалируются из одного мессенджера в другой и обратно.

## Как это работает

Мост использует **реальные пользовательские аккаунты** (не ботов) через MTProto (Pyrogram) для Telegram и WebSocket API (vkmax) для MAX.

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

Аккаунт пользователя одновременно **слушает** входящие сообщения и **отправляет** зеркала — это исключает эхо-петли на уровне протокола (Pyrogram не вызывает обработчик для собственных отправленных сообщений).

Имя отправителя оригинального сообщения всегда добавляется в начало текста зеркала.

**Поддерживается:** текст, фото, видео, файлы, аудио, голосовые, ответы на сообщения (reply), редактирование, удаление.

---

## Требования

| Компонент | Версия       |
|-----------|--------------|
| Python    | 3.12+        |
| Docker    | 20.10+ (опционально) |

```bash
pip install -r requirements.txt
```

---

## Шаг 1: Получение Telegram API credentials

1. Откройте [my.telegram.org](https://my.telegram.org) и войдите в свой аккаунт.
2. Перейдите в **API development tools**.
3. Создайте приложение (название и описание — произвольные).
4. Скопируйте **App api_id** (число) и **App api_hash** (строка из 32 символов).

> `api_id` и `api_hash` — общие для **всех** Telegram-аккаунтов в мосту. Создавать отдельные приложения для каждого пользователя не нужно.

---

## Шаг 2: Как узнать необходимые ID

### `telegram_user_id` — ID пользователя в Telegram

Напишите боту [@userinfobot](https://t.me/userinfobot) в Telegram — он ответит вашим числовым ID:

```
Your user ID is: 209388640
```

### `max_user_id` — ID пользователя в MAX

Откройте [web.max.ru](https://web.max.ru), войдите в аккаунт, откройте профиль и найдите `viewerId` в URL или в исходном коде страницы.

Самый простой способ: запустить `python -m src.auth` (шаг 3) — после авторизации MAX выводит user ID в логах:

```
bridge.max.pool INFO: Started client for alice (MAX ID: 205940119)
```

### `telegram_chat_id` — ID группы в Telegram

**Способ 1 (рекомендуется):** Перешлите любое сообщение из нужной группы боту [@userinfobot](https://t.me/userinfobot). Он ответит ID исходного чата:

```
Forwarded from: Chat ID: -639177777
```

**Способ 2:** В Telegram Desktop: `Settings → Advanced → Experimental features → Show Peer IDs`. ID отображается прямо в заголовке чата.

> Для супергрупп ID начинается с `-100`. Для обычных групп — просто отрицательное число (например `-639177777`).

### `max_chat_id` — ID чата в MAX

Откройте нужный чат в [web.max.ru](https://web.max.ru). ID виден в URL:

```
https://web.max.ru/#/chats/@chat/-72099589405396
                                  ↑ это и есть max_chat_id
```

---

## Шаг 3: Заполнение config.yaml

Скопируйте пример:

```bash
cp config.example.yaml config.yaml
```

Структура конфига:

```yaml
api_id: 12345678
api_hash: "0123456789abcdef0123456789abcdef"

sessions_dir: "sessions"   # необязательно, по умолчанию "sessions"

bridges:
  - name: "team-general"           # произвольное название (используется в логах)
    telegram_chat_id: -1001234567  # ID группы в Telegram (отрицательное число)
    max_chat_id: -72099000000001   # ID чата в MAX (отрицательное число)
    user:
      name: "alice"                # короткое имя (латиница/цифры), задаёт имена файлов сессий
      telegram_user_id: 111111111  # числовой ID пользователя в Telegram
      max_user_id: 205940119       # числовой ID пользователя в MAX
```

> Имена файлов сессий формируются автоматически:
> - `sessions/tg_{name}.session` (Telegram)
> - `sessions/max_{name}.max_session` (MAX)

### Несколько чатов для одного пользователя

Дублируйте блок `bridge` с теми же данными пользователя:

```yaml
bridges:
  - name: "team-general"
    telegram_chat_id: -1001111111111
    max_chat_id: -72099000000001
    user:
      name: "alice"
      telegram_user_id: 111111111
      max_user_id: 205940119

  - name: "team-dev"
    telegram_chat_id: -1002222222222
    max_chat_id: -72099000000002
    user:
      name: "alice"
      telegram_user_id: 111111111
      max_user_id: 205940119
```

### Несколько пользователей

Добавьте отдельный блок для каждого пользователя:

```yaml
bridges:
  - name: "team-general"
    telegram_chat_id: -1001111111111
    max_chat_id: -72099000000001
    user:
      name: "alice"
      telegram_user_id: 111111111
      max_user_id: 205111111

  - name: "team-general"
    telegram_chat_id: -1001111111111
    max_chat_id: -72099000000001
    user:
      name: "bob"
      telegram_user_id: 333333333
      max_user_id: 205333333
```

---

## Шаг 4: Авторизация аккаунтов

```bash
python -m src.auth
```

Скрипт последовательно авторизует каждого уникального пользователя из конфига:

- **Telegram**: запросит номер телефона и код из SMS/приложения Telegram.
- **MAX**: запросит номер телефона и код из SMS.

После авторизации в `sessions/` появятся файлы:

```
sessions/
├── tg_alice.session        # Pyrogram-сессия (бинарный формат + SQLite)
└── max_alice.max_session   # MAX-сессия (JSON с login_token)
```

> ⚠️ Файлы сессий содержат токены доступа к аккаунтам — не публикуйте их и не передавайте третьим лицам.

---

## Шаг 5: Запуск

### Вариант A — напрямую (Python)

```bash
python -m src
```

### Вариант B — Docker

#### Структура файлов

```
telegram-max-bridge/
├── config.yaml          # ← создать вручную (не в репозитории)
├── sessions/            # ← создать вручную, заполнится при авторизации
│   ├── tg_alice.session
│   └── max_alice.max_session
├── docker-compose.yml
└── Dockerfile
```

#### Первый запуск

```bash
# Клонировать репозиторий
git clone git@github.com:kzolotko/telegram-max-bridge.git
cd telegram-max-bridge

mkdir -p sessions
cp config.example.yaml config.yaml
nano config.yaml

# Авторизоваться интерактивно (нельзя сделать в фоновом контейнере — нужен ввод кода)
pip install -r requirements.txt
python -m src.auth

# Собрать образ и запустить в фоне
docker compose up -d --build
```

> Если Python нет на сервере — авторизуйтесь локально, затем скопируйте сессии:
> ```bash
> scp -r sessions/ user@server:/path/to/telegram-max-bridge/
> ```

#### Управление

```bash
docker compose logs -f              # логи в реальном времени
docker compose restart              # перезапуск (после изменения config.yaml)
docker compose up -d --build        # пересборка после обновления кода
docker compose down                 # остановка
docker compose exec bridge bash     # войти в контейнер
```

#### Что монтируется в контейнер

| Путь на хосте  | Путь в контейнере  | Режим      |
|----------------|--------------------|------------|
| `./config.yaml` | `/app/config.yaml` | read-only  |
| `./sessions/`  | `/app/sessions/`   | read-write |

---

## Архитектура

```
src/
├── main.py              # Точка входа, инициализация компонентов
├── config.py            # Загрузка и валидация config.yaml, ConfigLookup
├── types.py             # Датаклассы: AppConfig, BridgeEntry, UserMapping, BridgeEvent
├── auth.py              # Интерактивная авторизация аккаунтов
├── message_store.py     # In-memory маппинг ID сообщений (TTL 24h, нужен для reply/edit/delete)
├── bridge/
│   ├── bridge.py        # Роутинг событий, отправка зеркал
│   ├── mirror_tracker.py# Трекер ID отправленных зеркал (защита от эхо-петель)
│   └── formatting.py    # Форматирование текста (prepend_sender_name)
├── telegram/
│   ├── listener.py      # Pyrogram-клиент: слушает TG-группу от имени пользователя
│   └── client_pool.py   # Пул Pyrogram-клиентов, по одному на пользователя
└── max/
    ├── listener.py      # WebSocket-клиент: слушает MAX-чат, авто-переподключение
    ├── client_pool.py   # Пул MAX-клиентов для отправки
    ├── session.py       # Сохранение/загрузка MAX login_token
    ├── media.py         # Скачивание медиафайлов из MAX
    └── patched_client.py# Патч vkmax для корректной работы с MAX API
```

### Поток данных

1. `TelegramListener` / `MaxListener` получает событие (new / edit / delete).
2. Listener создаёт `BridgeEvent` с `bridge_entry` (какие чаты, какой пользователь).
3. `Bridge.handle_event` определяет направление и вызывает `_tg_to_max` или `_max_to_tg`.
4. Нужный клиент (TG или MAX) берётся из пула по `user_id` из `bridge_entry`.
5. Зеркало отправляется; ID нового сообщения сохраняется в `MessageStore` для последующих edit/delete/reply.
6. ID зеркала регистрируется в `MirrorTracker` — если MAX вернёт его обратно через WebSocket, слушатель его проигнорирует.

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
| Удаление MAX→TG | ✅ |
| Удаление TG→MAX | ⚠️ только для каналов (Bot API не сообщает `chat_id` при удалении в группах) |
| Стикеры | ⚠️ заменяются на `[Sticker: 🎉]` |
| Собственные сообщения пользователя в TG | ⚠️ не зеркалируются в MAX (Pyrogram не вызывает обработчик для своих отправленных сообщений) |
| Форматирование (bold, italic и т.д.) | ❌ не сохраняется |
| Реакции | ❌ |
| Опросы | ❌ |

---

## Troubleshooting

### `MAX session not found (...). Run 'python -m src.auth' first.`

Сессия не создана. Запустите авторизацию:

```bash
python -m src.auth
```

### MAX-сессия истекла

Токен MAX протухает через несколько недель неактивности. Удалите файл и повторите:

```bash
rm sessions/max_alice.max_session
python -m src.auth
```

### `AuthKeyUnregistered` / Telegram-сессия недействительна

Pyrogram-сессия была завершена другим устройством (например, через «Завершить все сессии» в настройках Telegram):

```bash
rm sessions/tg_alice.session
python -m src.auth
```

### `bridges[0].telegram_chat_id is required`

Заполните все обязательные поля в `config.yaml`. Убедитесь, что аккаунт пользователя является **участником** указанного чата.

### Сообщения не пересылаются

- Убедитесь, что аккаунт пользователя добавлен в оба чата (TG и MAX).
- Проверьте логи на ошибки подключения: `python -m src` или `docker compose logs -f`.
- Для MAX: WebSocket переподключается автоматически при разрыве — это нормально.
