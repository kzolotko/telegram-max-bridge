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
pytest tests/e2e/ -k E05

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
pytest tests/e2e/ -m edge
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

### Краевые сценарии

| ID | Сценарий | Авто | Статус | Последний прогон |
|----|----------|:----:|:------:|-----------------|
| E01 | Emoji (astral plane) TG → MAX | 🤖 | 🔲 | — |
| E02 | Emoji (astral plane) MAX → TG | 🤖 | 🔲 | — |
| E03 | Многострочный текст TG → MAX | 🤖 | 🔲 | — |
| E04 | Многострочный текст MAX → TG | 🤖 | 🔲 | — |
| E05 | Спецсимволы `< > & "` TG → MAX | 🤖 | 🔲 | — |
| E06 | Спецсимволы `< > & "` MAX → TG | 🤖 | 🔲 | — |
| E07 | Длинный текст (1000+ символов) TG → MAX | 🤖 | 🔲 | — |
| E08 | Длинный текст (1000+ символов) MAX → TG | 🤖 | 🔲 | — |
| E09 | Порядок 3 быстрых сообщений TG → MAX | 🤖 | 🔲 | — |
| E10 | Порядок 3 быстрых сообщений MAX → TG | 🤖 | 🔲 | — |
| E11 | Двойное редактирование TG → MAX | 🤖 | 🔲 | — |
| E12 | Двойное редактирование MAX → TG | 🤖 | 🔲 | — |

### Форматирование

| ID | Сценарий | Авто | Статус | Последний прогон |
|----|----------|:----:|:------:|-----------------|
| F01 | Bold TG → MAX (проверка STRONG элемента) | 🤖 | 🔲 | — |
| F02 | Italic TG → MAX (проверка EMPHASIZED элемента) | 🤖 | 🔲 | — |
| F03 | Bold MAX → TG (проверка BOLD entity) | 🤖 | 🔲 | — |
| F04 | Italic MAX → TG (проверка ITALIC entity) | 🤖 | 🔲 | — |
| F05 | Underline TG → MAX (проверка UNDERLINE элемента) | 🤖 | 🔲 | — |
| F06 | Strikethrough TG → MAX (проверка STRIKETHROUGH элемента) | 🤖 | 🔲 | — |
| F07 | Underline MAX → TG (проверка UNDERLINE entity) | 🤖 | 🔲 | — |
| F08 | Strikethrough MAX → TG (проверка STRIKETHROUGH entity) | 🤖 | 🔲 | — |
| F09 | Смешанное (bold + italic) TG → MAX | 🤖 | 🔲 | — |
| F10 | Code block TG → MAX (передаётся как plain text) | 🤖 | 🔲 | — |

### Реакции

| ID | Сценарий | Авто | Статус | Последний прогон |
|----|----------|:----:|:------:|-----------------|
| R01 | Реакция TG → MAX (добавление 👍) | 👤 | 🔲 | — |
| R02 | Реакция MAX → TG (добавление 👍) | 👤 | 🔲 | — |
| R03 | Снятие реакции TG → MAX | 👤 | 🔲 | — |
| R04 | Снятие реакции MAX → TG | 👤 | 🔲 | — |
