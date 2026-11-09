# Agent Board

Показывает, кто из агентов работает, кто ждёт ответа и что нужно переслать. Стиль Windows 98, работает на ПК (окно + уведомления Windows) и на телефоне по Wi-Fi.

Агенты не подключаются к приложению напрямую: они кладут маленький JSON-файл (маркер) в папку, Agent Board его забирает. Новый агент появляется в списке сам после первого маркера.

## Установка (Windows)

Нужен Python 3.10+ в PATH.

```powershell
cd agent-board
powershell -ExecutionPolicy Bypass -File install.ps1
```

Установщик скачает 98.css и иконки Windows 98 (в репозитории их нет), поставит pywebview, создаст ярлык «Agent Board» на рабочем столе и в автозагрузке (сервер стартует в фоне при входе в Windows). Окно — WebView2 без рамки Windows: единственный заголовок — в стиле Windows 98 (перетаскивание, двойной клик — развернуть, уголок справа внизу — размер). Без pywebview окно откроется в Chromium-браузере (Edge, Chrome, Thorium…) или в браузере по умолчанию. При первом запуске Windows спросит про брандмауэр — разрешите Python для **частной** сети, иначе телефон не подключится.

Данные хранятся в `%USERPROFILE%\AgentBoard`: `config.json`, `state.json`, `server.log`.

## Папки и маркеры

У каждого проекта своя папка-ящик (настраивается в «Файл → Папки с маркерами»). Проект определяется папкой, а не полем в маркере:

| Проект | Папка | С ноутбука |
|---|---|---|
| AZS | `C:\Users\User\Documents\AZS-Shared\monitor\inbox` | `\\DESKTOP-BQ3HL4Q\Users\User\Documents\AZS-Shared\monitor\inbox` |
| SSJC | `C:\Users\User\Documents\SSJ Camera-Shared\monitor\inbox` | `\\DESKTOP-BQ3HL4Q\Users\User\Documents\SSJ Camera-Shared\monitor\inbox` |

Агент пишет новый файл на каждое событие (сначала `.tmp`, затем переименование в `.json`). Agent Board забирает его и переносит в `processed\<дата>\`; нечитаемые через 10 секунд уходят в `bad\<дата>\` вместе с файлом ошибки.

```json
{"agent": "Hoplite", "session_id": "hop-m41-a", "seq": 4, "event_id": "hop-m41-a-4",
 "state": "done", "phase": "ready_for_review", "task": "M41: офлайн-сверка O6", "to": "Codex",
 "message": "Hoplite: M41 готова, отчёт from-helper\\hoplite\\M41-O6-phone-analysis\\REPORT-M41.md, SHA …",
 "result": "from-helper\\hoplite\\M41-O6-phone-analysis\\REPORT-M41.md"}
```

| Поле | Значение |
|---|---|
| `agent` | постоянное имя: Codex, Hoplite, Opus (1), Opus (2), Astra |
| `state` | `working` · `waiting` · `done` · `blocked` · `idle` |
| `waiting_for` | при `waiting`: `owner_input` · `owner_forward` · `reviewer` · `coordinator_review` · `resource` · `model_limit` |
| `to`, `message` | кому владелец пересылает результат и готовый текст для чата адресата |
| `ack` | `message_id` пересланного сообщения (строка «[Agent Board] message_id: AB-12» в тексте) |
| `session_id`, `seq`, `event_id` | сессия чата, номер события в ней, уникальный id (дубли отбрасываются) |
| `phase`, `task`, `note`, `result` | этап, задача, пояснение, путь к отчёту |

Напоминания:

- **Переслать** — `done`, `needs_forward: true` или ожидание `owner_forward`/`reviewer`/`coordinator_review` с указанным `to`. «Копировать» кладёт текст с `message_id` в буфер, «Переслал» — ваша отметка (приложение не видит вставку в чужое окно). Напоминание закрывается, когда адресат пришлёт маркер с `ack` (или начнёт ту же задачу).
- **Ждёт ответа** — `waiting` + `owner_input`; **заблокирован** — `blocked`; **лимит модели** — `waiting` + `model_limit`. Закрываются, когда агент снова работает.
- `reviewer` и `resource` без `to` — только статус, без напоминаний.

Событие старше уже полученного в той же сессии (меньший `seq`) попадает в журнал, но статус не меняет. Если `working` не обновлялся 90 минут — «активность не подтверждена» (`stale_minutes` в `config.json`); это не значит, что агент завис.

Текст для агентов: «Справка → Инструкция для агентов», отдельно для каждого проекта.

## Телефон

«Файл → Подключить телефон» показывает ссылку с ключом доступа. Без ключа с других устройств сервер отвечает 403. На телефоне звук и вибрация при новых напоминаниях (коснитесь страницы один раз, чтобы браузер разрешил звук).

## Запуск без установки

```bash
python fetch_assets.py
python server.py        # http://127.0.0.1:8098/
```
