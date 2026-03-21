# TASKS — Telegram ↔ MAX Bridge

Трекер задач и тест-план.

---

## Тест-план

Тест-кейсы и их статусы ведутся в [`tests/e2e/TEST_CASES.md`](tests/e2e/TEST_CASES.md).

---

## Backlog

| Приоритет | Фича | Описание |
|-----------|------|----------|
| 🔴 High | Telegram-бот управления бриджем | Бот для управления через Telegram-чат. **Конфигурирование:** добавление/удаление пользователей и мостов, авторизация MAX-аккаунтов, просмотр/редактирование config.yaml. **Управление состоянием:** статус бриджа (подключения TG/MAX, аптайм), перезапуск отдельных мостов, пауза/возобновление пересылки, просмотр логов в реальном времени. Позволяет управлять бриджем на удалённом сервере без SSH |
| 🔴 High | Персистентный message store | In-memory store теряется при перезапуске — reply/edit/delete перестают работать. SQLite с TTL |
| 🔴 High | Асимметричные пользователи | Опциональность `telegram_user_id` / `max_user_id` — пользователь только в одном мессенджере |
| 🟢 Low | Healthcheck endpoint | HTTP `/health` для Docker healthcheck |
| 🟢 Low | Метрики | Счётчики переданных сообщений |

---

## Выполнено

| Фича | Описание |
|------|----------|
| ✅ Нативный TCP/SSL протокол MAX | Замена WebSocket/JS скриптов на бинарный протокол через `api.oneme.ru:443` |
| ✅ Разделение конфигов | `credentials.yaml` (API credentials, один раз) + `config.yaml` (мосты) |
| ✅ Setup wizard с инкрементальным режимом | Добавление мостов/пользователей к существующему конфигу без перезаписи |
| ✅ Автоматический список чатов TG и MAX | Выбор чатов из списка при настройке (загружаются чаты выбранного пользователя) |
| ✅ Проверка членства в чатах | Верификация при добавлении пользователя к мосту и при ручном вводе ID |
| ✅ `bridge.sh` launcher | Единый скрипт для всех операций (start, setup, auth, docker) |
| ✅ Быстрая доставка MAX→TG | Устранена 3-секундная задержка (pre-populated name cache вместо блокирующего `get_users()`) |
| ✅ NOTIF_MSG_DELETE (opcode 142) | Обработка уведомлений об удалении сообщений в MAX |
| ✅ Async SSL (NativeMaxAuth) | Переход с `run_in_executor` + blocking sockets на `asyncio.open_connection` |
| ✅ Исправить delete в обе стороны | TG→MAX: кэш `msg_id→chat_id` + unfiltered `DeletedMessagesHandler`. MAX→TG: opcode 128 (status=REMOVED) + opcode 142 |
| ✅ Delete/Edit MAX→TG (редактирование) | Opcode 128 (status=EDITED) — редактирование чужих сообщений в MAX корректно отражается в TG |
| ✅ Имена отправителей MAX | Предзагрузка участников чатов при старте (`load_members`); имя = `first_name + last_name` из профиля MAX |
| ✅ `max_user_id` в setup wizard | Надёжное получение ID через `BridgeMaxClient.connect_and_login()` → `inner.me.id` после авторизации |
| ✅ Тёплый кэш Pyrogram peer | `get_dialogs()` перед `get_chat()` устраняет ошибку «Peer id invalid» для обычных TG-групп |
| ✅ Стабилизация повторного подключения MAX в setup | Пауза 2 с между MAX-сессиями предотвращает ошибки «Connection lost / Failed to unpack packet» |
| ✅ Форматирование текста (bold/italic/code) | Конвертация TG entities ↔ MAX elements. Bold, italic, underline, strikethrough — в обе стороны. Code/pre/text_link — TG→MAX без форматирования (только текст) |
| ✅ Реакции | TG→MAX: `RawUpdateHandler` + `UpdateMessageReactions.chosen_order` → `add/remove_reaction`. MAX→TG: opcode 155 `yourReaction` → `send_reaction`. Echo-защита через `MirrorTracker` |
| ✅ Опросы (polls) | TG→MAX: `message.poll` форматируется как `📊 Вопрос\n  A) ...` и отправляется как текст. MAX→TG: не требуется (MAX не поддерживает polls) |
| ✅ Несколько медиафайлов в сообщении | TG→MAX: буферизация альбомов по `media_group_id` (0.8 с задержка); MAX→TG: все аттачи скачиваются и пересылаются как TG album через `send_media_group` |
| ✅ Graceful reconnect | Pool-клиенты MAX автоматически переподключаются при обрыве; retry отправки после reconnect; MirrorTracker с LRU-eviction (10k); health-check каждые 5 мин |
| ✅ E2E-автотесты | 58 тестов через реальные аккаунты TG и MAX: текст, медиа (фото/видео/альбомы), форматирование, reply, edit, delete, реакции, краевые сценарии. Статусы автообновляются в `TEST_CASES.md`. Запуск: `./bridge.sh test` |

---

## Известные ограничения

| Ограничение | Причина |
|-------------|---------|
| Delete MAX→TG не работает для собственных сообщений бриджа | MAX не присылает уведомления об удалении сообщений, отправленных от имени того же аккаунта |
| Reply/edit/delete теряются после перезапуска | In-memory store — нет персистентности |
| Code/pre/text_link форматирование теряется при TG→MAX | MAX поддерживает только bold/italic/underline/strikethrough; code-блоки и ссылки передаются как plain text |
| Кэш `msg_id→chat_id` не переживает перезапуск | TG→MAX delete не сработает для сообщений, отправленных до перезапуска бриджа (в памяти) |
