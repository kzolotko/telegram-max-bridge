# E2E Test Cases — Telegram ↔ MAX Bridge

## Prerequisites

- Bridge is running (`./bridge.sh start` or equivalent)
- E2E TG session is authenticated (`python -m tests.e2e.auth_e2e`)
- `tests/e2e/e2e_config.yaml` exists with valid credentials

## How to run

```bash
# All E2E tests
pytest tests/e2e/ -v

# Single test case
pytest tests/e2e/ -k T01
pytest tests/e2e/ -k M13

# By direction
pytest tests/e2e/ -k "tg_to_max"
pytest tests/e2e/ -k "max_to_tg"

# By marker
pytest tests/e2e/ -m text
pytest tests/e2e/ -m reply
pytest tests/e2e/ -m edit
pytest tests/e2e/ -m delete
pytest tests/e2e/ -m echo
pytest tests/e2e/ -m formatting
```

## Status legend

- Авто: `🤖` = has automated test, `👤` = manual only
- Статус: `🔲` not tested, `✅` passed, `❌` failed, `⚠️` skipped
- Последний прогон: `—` initially, updated automatically by conftest after each pytest run

---

### TG → MAX

| ID | Сценарий | Авто | Статус | Последний прогон |
|----|----------|:----:|:------:|-----------------|
| T01 | Текст от зарегистрированного пользователя | 🤖 | 🔲 | — |
| T02 | Текст от незарегистрированного пользователя (префикс `[Имя]:`) | 👤 | 🔲 | — |
| T03 | Фото без подписи | 👤 | 🔲 | — |
| T04 | Фото с подписью | 👤 | 🔲 | — |
| T05 | Видео | 👤 | 🔲 | — |
| T06 | Файл/документ | 👤 | 🔲 | — |
| T07 | Аудио | 👤 | 🔲 | — |
| T08 | Голосовое сообщение | 👤 | 🔲 | — |
| T09 | Стикер (передаётся как текст) | 👤 | 🔲 | — |
| T10 | Опрос (передаётся как текст) | 👤 | 🔲 | — |
| T11 | Reply на сообщение, прошедшее через бридж | 🤖 | 🔲 | — |
| T12 | Reply на сообщение, не проходившее через бридж | 👤 | 🔲 | — |
| T13 | Редактирование текста | 🤖 | 🔲 | — |
| T14 | Удаление сообщения | 🤖 | 🔲 | — |
| T15 | Эхо-петля: сообщение бриджа не возвращается обратно | 🤖 | 🔲 | — |

### MAX → TG

| ID | Сценарий | Авто | Статус | Последний прогон |
|----|----------|:----:|:------:|-----------------|
| M01 | Текст от зарегистрированного пользователя | 🤖 | 🔲 | — |
| M02 | Текст от незарегистрированного пользователя (префикс `[Имя]:`) | 👤 | 🔲 | — |
| M03 | Фото без подписи | 👤 | 🔲 | — |
| M04 | Фото с подписью | 👤 | 🔲 | — |
| M05 | Видео | 👤 | 🔲 | — |
| M06 | Файл/документ | 👤 | 🔲 | — |
| M07 | Аудио | 👤 | 🔲 | — |
| M08 | Голосовое сообщение | 👤 | 🔲 | — |
| M09 | Стикер (передаётся как текст) | 👤 | 🔲 | — |
| M10 | Reply на TG-origin сообщение | 🤖 | 🔲 | — |
| M11 | Reply на MAX-origin сообщение | 🤖 | 🔲 | — |
| M12 | Reply на сообщение, не проходившее через бридж | 👤 | 🔲 | — |
| M13 | Редактирование текста | 🤖 | 🔲 | — |
| M14 | Удаление сообщения | 🤖 | 🔲 | — |
| M15 | Эхо-петля: сообщение бриджа не возвращается обратно | 🤖 | 🔲 | — |

### Форматирование

| ID | Сценарий | Авто | Статус | Последний прогон |
|----|----------|:----:|:------:|-----------------|
| F01 | Bold TG → MAX | 🤖 | 🔲 | — |
| F02 | Italic TG → MAX | 🤖 | 🔲 | — |
| F03 | Bold MAX → TG | 🤖 | 🔲 | — |
| F04 | Italic MAX → TG | 🤖 | 🔲 | — |
