# TASKS — Telegram ↔ MAX Bridge

Трекер задач и тест-план.

---

## Тест-план

Тест-кейсы и их статусы ведутся в [`tests/e2e/TEST_CASES.md`](tests/e2e/TEST_CASES.md).

---

## Backlog

| Приоритет | Фича | Описание |
|-----------|------|----------|
| 🟡 Medium | Асимметричные пользователи | Опциональность `telegram_user_id` / `max_user_id` — пользователь только в одном мессенджере |
| 🟢 Low | Метрики | Счётчики переданных сообщений (доступны через admin bot) |

---

## Выполнено

| Фича | Описание |
|------|----------|
| ✅ Production Docker | Multi-stage Dockerfile (gcc только в builder), file-based healthcheck (heartbeat каждые 5 мин → Docker перезапускает при зависании), graceful shutdown (stop_grace_period 15s), mem_limit 512m, log rotation (json-file 10m×3), no-new-privileges, TZ |
| ✅ SQLite VACUUM | Автоматический VACUUM раз в 24 часа в cleanup loop — рекламация дискового пространства |
| ✅ Admin-бот | Telegram-бот для удалённого управления: /status, /bridges, /users, /logs, /pause, /resume, /addbridge, /rmbridge, /adduser, /rmuser, /authmax, /authtg, /config, /restart. Уведомления админам при старте/перезапуске |
| ✅ Персистентный message store | SQLite с WAL mode. TTL 24ч, cleanup каждые 10 мин. Reply/edit/delete работают после перезапуска |
| ✅ DM-бридж | MAX DMs → TG бот с reply-routing. Один бот на всех пользователей из bridges. Текст, фото, файлы, edit, delete. Настройка: `dm_bridge.bot_token` в config.yaml |
| ✅ Реструктуризация конфига | Отдельная секция `users:` (реестр пользователей) + `bridges:` со ссылками на пользователей по имени. Изолированные сценарии: управление пользователями (`setup users`) отдельно от управления мостами (`setup bridges`). Обратная совместимость со старым форматом + миграция (`setup migrate`) |
| ✅ E2E-автотесты | 86 тестов через реальные аккаунты TG и MAX. 71 pass, 6 skip (ограничение протокола MAX), 3 manual only. Включая DM-бридж тесты. Статусы автообновляются в `TEST_CASES.md`. Запуск: `./bridge.sh test` |
| ✅ Queue-based MAX listener | Recv callback → asyncio.Queue → worker task. Устраняет deadlock при `_send_and_wait` (get_file_by_id, get_users) из обработчиков пакетов |
| ✅ Graceful reconnect | Pool-клиенты MAX автоматически переподключаются; retry отправки; MirrorTracker с LRU-eviction (10k); health-check каждые 5 мин |
| ✅ Альбомы (media groups) | TG→MAX: буферизация по `media_group_id` (0.8 с) → единое сообщение с несколькими attaches. MAX→TG: все аттачи → TG `send_media_group` |
| ✅ Медиа (фото/видео/файлы/аудио) | TG→MAX: скачивание через Pyrogram + upload через pymax (opcode 87) с HTTP fallback (opcode 80). MAX→TG: download через CDN URL или get_file_by_id (opcode 88) + send через Pyrogram. Поддержка `message.animation` (маленькие видео) |
| ✅ Опросы (polls) | TG→MAX: `message.poll` → форматированный текст `📊 ...`. MAX→TG: не требуется (MAX не поддерживает polls) |
| ✅ Реакции | TG→MAX: `UpdateMessageReactions` → opcode 178 (MSG_REACTION). MAX→TG: opcode 155 → `send_reaction`. Echo-защита через `MirrorTracker` |
| ✅ Форматирование текста | TG entities ↔ MAX elements. Bold, italic, underline, strikethrough — в обе стороны. Code/pre/text_link — TG→MAX как plain text |
| ✅ Delete в обе стороны | TG→MAX: `DeletedMessagesHandler`. MAX→TG: opcode 128 (status=REMOVED) + opcode 142 |
| ✅ Edit MAX→TG | Opcode 128 (status=EDITED) — редактирование чужих сообщений в MAX корректно отражается в TG |
| ✅ Нативный TCP/SSL протокол MAX | Замена WebSocket/JS скриптов на бинарный протокол через `api.oneme.ru:443` |
| ✅ Setup wizard с инкрементальным режимом | Добавление мостов/пользователей к существующему конфигу без перезаписи |
| ✅ Автоматический список чатов TG и MAX | Выбор чатов из списка при настройке (загружаются чаты выбранного пользователя) |
| ✅ Проверка членства в чатах | Верификация при добавлении пользователя к мосту и при ручном вводе ID |
| ✅ `bridge.sh` launcher | Единый скрипт для всех операций (start, setup, auth, docker, test) |
| ✅ Быстрая доставка MAX→TG | Предзагрузка имён участников при старте; async name resolution через queue-based worker |
| ✅ Async SSL (NativeMaxAuth) | `asyncio.open_connection` вместо `run_in_executor` + blocking sockets |
| ✅ Имена отправителей MAX | Предзагрузка участников чатов при старте (`load_members`); async fetch через queue-based worker на cache miss |
| ✅ `max_user_id` в setup wizard | Получение ID через `BridgeMaxClient.connect_and_login()` → `inner.me.id` |
| ✅ Тёплый кэш Pyrogram peer | Точечный `get_chat()` только для чатов из конфига (вместо полного `get_dialogs()` ~20с). Fallback на `get_dialogs()` при cache miss |
| ✅ Стабилизация повторного подключения MAX | Пауза 2 с между MAX-сессиями предотвращает ошибки «Connection lost» |
| ✅ Разделение конфигов | `credentials.yaml` (API credentials, один раз) + `config.yaml` (мосты) |

---

## Известные ограничения

| Ограничение | Причина |
|-------------|---------|
| Delete MAX→TG не работает для собственных сообщений бриджа | MAX не присылает уведомления об удалении сообщений, отправленных от имени того же аккаунта |
| Code/pre/text_link форматирование теряется при TG→MAX | MAX поддерживает только bold/italic/underline/strikethrough |
| MAX rate limit на edits | `errors.edit-message.send-too-many-edit` при частых редактированиях; cooldown ~5 мин |
| DM-уведомления MAX | MAX доставляет DM-уведомления (opcode 128) только на соединение отправителя, не получателя. Echo prevention через MirrorTracker |
