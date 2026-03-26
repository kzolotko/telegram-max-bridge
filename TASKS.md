# TASKS — Telegram ↔ MAX Bridge

Трекер задач и тест-план.

---

## Тест-план

Тест-кейсы и их статусы ведутся в [`tests/e2e/TEST_CASES.md`](tests/e2e/TEST_CASES.md).

---

## Backlog

| Приоритет | Фича | Описание |
|-----------|------|----------|
| 🔴 High | Персистентный message store | In-memory store теряется при перезапуске — reply/edit/delete перестают работать. SQLite с TTL |
| 🔴 High | Telegram-бот управления бриджем | Бот для управления через Telegram-чат. **Конфигурирование:** добавление/удаление пользователей и мостов, авторизация MAX-аккаунтов, просмотр/редактирование config.yaml. **Управление состоянием:** статус бриджа (подключения TG/MAX, аптайм), перезапуск отдельных мостов, пауза/возобновление пересылки, просмотр логов в реальном времени. Позволяет управлять бриджем на удалённом сервере без SSH |
| 🔴 High | Бридж директ-сообщений | Опция пересылки личных (direct) сообщений между TG и MAX, а не только групповых чатов |
| 🟡 Medium | Асимметричные пользователи | Опциональность `telegram_user_id` / `max_user_id` — пользователь только в одном мессенджере |
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
| ✅ `bridge.sh` launcher | Единый скрипт для всех операций (start, setup, auth, docker, test) |
| ✅ Быстрая доставка MAX→TG | Предзагрузка имён участников при старте; async name resolution через queue-based worker |
| ✅ NOTIF_MSG_DELETE (opcode 142) | Обработка уведомлений об удалении сообщений в MAX |
| ✅ Async SSL (NativeMaxAuth) | `asyncio.open_connection` вместо `run_in_executor` + blocking sockets |
| ✅ Delete в обе стороны | TG→MAX: кэш `msg_id→chat_id` + `DeletedMessagesHandler`. MAX→TG: opcode 128 (status=REMOVED) + opcode 142 |
| ✅ Edit MAX→TG | Opcode 128 (status=EDITED) — редактирование чужих сообщений в MAX корректно отражается в TG |
| ✅ Имена отправителей MAX | Предзагрузка участников чатов при старте (`load_members`); async fetch через queue-based worker на cache miss |
| ✅ `max_user_id` в setup wizard | Получение ID через `BridgeMaxClient.connect_and_login()` → `inner.me.id` |
| ✅ Тёплый кэш Pyrogram peer | Точечный `get_chat()` только для чатов из конфига (вместо полного `get_dialogs()` ~20с). Fallback на `get_dialogs()` при cache miss |
| ✅ Стабилизация повторного подключения MAX | Пауза 2 с между MAX-сессиями предотвращает ошибки «Connection lost» |
| ✅ Форматирование текста | TG entities ↔ MAX elements. Bold, italic, underline, strikethrough — в обе стороны. Code/pre/text_link — TG→MAX как plain text |
| ✅ Реакции | TG→MAX: `UpdateMessageReactions` → opcode 178 (MSG_REACTION). MAX→TG: opcode 155 → `send_reaction`. Echo-защита через `MirrorTracker` |
| ✅ Опросы (polls) | TG→MAX: `message.poll` → форматированный текст `📊 ...`. MAX→TG: не требуется (MAX не поддерживает polls) |
| ✅ Медиа (фото/видео/файлы/аудио) | TG→MAX: скачивание через Pyrogram + upload через pymax (opcode 87) с HTTP fallback (opcode 80). MAX→TG: download через CDN URL или get_file_by_id (opcode 88) + send через Pyrogram. Поддержка `message.animation` (маленькие видео) |
| ✅ Альбомы (media groups) | TG→MAX: буферизация по `media_group_id` (0.8 с) → единое сообщение с несколькими attaches. MAX→TG: все аттачи → TG `send_media_group` |
| ✅ Graceful reconnect | Pool-клиенты MAX автоматически переподключаются; retry отправки; MirrorTracker с LRU-eviction (10k); health-check каждые 5 мин |
| ✅ Queue-based MAX listener | Recv callback → asyncio.Queue → worker task. Устраняет deadlock при `_send_and_wait` (get_file_by_id, get_users) из обработчиков пакетов |
| ✅ E2E-автотесты | 58 тестов через реальные аккаунты TG и MAX. 49-53 pass, 4 skip (реакции — ограничение single-user). Статусы автообновляются в `TEST_CASES.md`. Запуск: `./bridge.sh test` |

---

## Известные ограничения

| Ограничение | Причина |
|-------------|---------|
| Delete MAX→TG не работает для собственных сообщений бриджа | MAX не присылает уведомления об удалении сообщений, отправленных от имени того же аккаунта |
| Reply/edit/delete теряются после перезапуска | In-memory store — нет персистентности (см. бэклог: SQLite) |
| Code/pre/text_link форматирование теряется при TG→MAX | MAX поддерживает только bold/italic/underline/strikethrough |
| Кэш `msg_id→chat_id` не переживает перезапуск | TG→MAX delete не сработает для сообщений до перезапуска |
| MAX rate limit на edits | `errors.edit-message.send-too-many-edit` при частых редактированиях; cooldown ~5 мин |
