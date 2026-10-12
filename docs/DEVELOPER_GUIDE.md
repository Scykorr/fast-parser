# Руководство разработчика Fast Parser

Дата: 11.10.2026. Описание сверено с кодом 0.13.1; требования — [AGENTS.md](../AGENTS.md).
Архитектура: [ARCHITECTURE.md](ARCHITECTURE.md), продолжение: [CURSOR_HANDOFF.md](CURSOR_HANDOFF.md).

## 1. Стек и границы проекта

Python, FastAPI/Starlette, Uvicorn, httpx, Pydantic, BeautifulSoup, Jinja2, tzdata;
SQLite через стандартный sqlite3; pytest для проверок. UI — обычные HTML/CSS/JS,
без React, npm-сборки или внешнего CDN. `requirements.lock` фиксирует зависимости,
`pyproject.toml` содержит метаданные и диапазон Python >=3.13. На исходном компьютере
использовался Python 3.14.6. PostgreSQL/Redis/Celery не внедрены и для локального
профиля сейчас не нужны. Docker подготовлен, реальная работоспособность не подтверждена.

Проект рассчитан на локальное приложение и один активный collector. Удобство
разработки не должно отменять бесплатность источников, TTL или блокировки.

## 2. Первый запуск в Cursor

Открой корень репозитория, а не только папку docs. Выбери интерпретатор
`.venv\Scripts\python.exe` в редакторе. Скопированная `.venv` с другого компьютера
не является переносимым окружением: пересоздай ее из lock-файла.

```powershell
py -0p
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m fast_parser validate-config
.\.venv\Scripts\python.exe -m fast_parser serve
```

Если Python 3.14 отсутствует, сначала выбери установленный поддерживаемый >=3.13
и проверь lock/тесты на нем; не заявляй поддержку версии только по декларации.
Текущее окружение не удалять при наличии `.venv`: сначала узнать его версию.
Активация окружения не обязательна при вызове полного пути интерпретатора.

UI: http://127.0.0.1:8000/. API: `/api/v1`. Документация API в приложении: `/docs`;
это собственный шаблон, не штатный Swagger UI. `/openapi.json` дает машинную схему.
Проверки: `/health/live`, `/health/ready`. Пути `/health` в приложении нет.

Остановить managed-server: `stop.cmd` либо `python -m fast_parser stop --port 8000`.
Для другого порта: `start.ps1 -Port 8001` / `serve --port 8001` и `stop.cmd --port 8001`.
Не использовать taskkill по имени Python или убивать любой процесс, занявший порт.

## 3. Файлы проекта

| Путь | Роль |
|---|---|
| `fast_parser/app.py` | ASGI-приложение, startup/lifespan, API, middleware, static/templates |
| `fast_parser/collector.py` | HTTP, квоты, backoff, планы/пагинация, lease, параллельные источники |
| `fast_parser/storage.py` | SQLite v3, транзакции, фильтры/курсоры, TTL, изменения, настройки |
| `fast_parser/domain.py` | Match, utcnow/parse_time/stamp |
| `fast_parser/config.py` | Settings и проверка временных окон |
| `fast_parser/sources.py` | Реестр официальных префиксов и source IDs |
| `fast_parser/official.py` | LaLiga HTML/Next JSON |
| `fast_parser/english.py` | FPL/Premier League и EFL; также совместимые экспорты реестра |
| `fast_parser/italian.py` | Lega Serie A public JSON и Lega B HTML |
| `fast_parser/german.py` | Bundesliga SSR ng-state |
| `fast_parser/russian.py` | ФНЛ public JSON, российский catalog, причина отсутствия РПЛ |
| `fast_parser/adapters.py` | Legacy OpenLigaDB/Sky Sports и SchemaError |
| `fast_parser/verification.py` | CoverageVerification и требования к доказательствам |
| `fast_parser/lifecycle.py` | Bind localhost, managed stop-token, graceful shutdown |
| `fast_parser/__main__.py` | CLI |
| `fast_parser/static/app.js` | Страны/лиги/матчи, пометки, настройки, детали, клиентская пагинация |
| `fast_parser/static/style.css` | Легкий responsive UI |
| `fast_parser/templates/` | Главная страница и собственное описание API |
| `tests/` | Синтетические проверки с отдельной SQLite и mock HTTP |
| `scripts/` | check, benchmark, browser smoke и историческая сверка Испании |
| `data/` | Игнорируемая рабочая БД/планы/контроль процесса/QA |

## 4. Конфигурация

Настройки хранятся в metadata.settings в SQLite; отдельный config.json не требуется.
В UI редактирование отправляется PUT `/api/v1/settings` с Origin и CSRF.
CLI `validate-config` читает/валидирует текущие настройки и не выполняет сбор.

| Поле | По умолчанию | Ограничение/смысл |
|---|---|---|
| timezone | Europe/Moscow | IANA; UI и часы активности, точные timestamps все равно UTC |
| mode | rolling | rolling либо absolute |
| lookback_days | 3 | 0–3 |
| lookahead_days | 7 | 1–90 |
| date_from/date_to | null | Для absolute обязательны ISO с timezone, start < end |
| fixtures_interval_seconds | 21600 | 900–604800 |
| results_interval_seconds | 300 | 120–86400 |
| live_interval_seconds | 120 | 120–86400 |
| final_recheck_interval_seconds | 21600 | 900–86400 |
| active_windows | [] | До 10 окон HH:MM-HH:MM; [] = постоянно; ночное окно поддержано |
| enabled_leagues | bl1, bl2, bl3, pl, la1, ucl, dfb | Legacy OpenLigaDB shortcuts; можно сохранить без строк каталога. Другой отсутствующий shortcut отклоняется. Официальные workers этот список не читают |
| html_enabled | true | Legacy Sky; не выключает официальные HTML-адаптеры |
| official_only | true | По умолчанию и при чтении старых настроек без этого поля |

Для rolling границы — now минус lookback и now плюс lookahead. Для absolute сбор
дополнительно ограничивает начало `max(date_from, now-3days)`. Это защита рабочего
TTL, не исторический архив. Валидатор абсолютного окна использует `(end-start).days`:
если требуется строго 90*24 часа до секунды, это отдельная небольшая задача с тестом.
Настроенная частота не отменяет минимальный интервал источника. Возле начала матча
worker выбирает ближайший допустимый опрос; расписание не является жестким SLA.

Env-переменные: `FAST_PARSER_DB` — путь БД, `FAST_PARSER_WORKER=0` — web без worker.
ROOT вычисляется из расположения пакета. При обычном импорте `app.py` создается
module-level app и Store; с worker по умолчанию также seed официального каталога.
Фоновая задача запускается только в lifespan. Для изоляции выставляй env ДО импорта
или используй `create_app(temp_path, start_worker=False)`.

В чистом официальном каталоге исторических shortcuts может не быть. `save_settings`
всё равно сохраняет timezone/окно и этот список. Shortcut вне списка и вне каталога
отклоняется сообщением «Неизвестная лига: сначала обновите каталог». Не выключать
`official_only` и не запускать discovery, чтобы обойти проверку.

## 5. Команды CLI и их реальные пределы

| Команда | Действие |
|---|---|
| serve [--port] | Managed localhost web + встроенный worker |
| stop [--port] | Graceful stop только управляемого экземпляра |
| run-worker | Отдельный collector, без web; конкурирует за тот же lease |
| validate-config | Вывод валидированных Settings |
| coverage-report | Каталог/состояния/пометки из текущей БД |
| plan-collection | Исторический расчет OpenLigaDB; не точный план всех официальных задач |
| source-check | В официальном режиме один цикл каждого рабочего источника; не полная сверка |
| sync-fixtures / sync-results | Один и тот же общий механизм обхода; не два независимых парсера |
| cleanup --dry-run | Число истекших карточек без удаления |
| cleanup | Ограниченная пачка удаления и служебная очистка |

CLI источников требует свободный collector lease. Если serve уже работает, используй
кнопку проверки в UI либо сначала останови его. `False` может означать «пока не due»
или «загружен справочник», а не ошибка и не ноль матчей. Cooldown в CLI ожидается
ограниченно; бесконечный manual retry неприемлем. РПЛ не входит в рабочий обход CLI.
В `official_only=false` текущая ветка CLI обходит legacy; web worker, напротив,
продолжает официальные источники плюс разрешенный legacy. Не скрывай это различие.

## 6. Автотесты и изоляция

```powershell
.\scripts\check.ps1
node --check fast_parser/static/app.js
```

check.ps1 создает отдельный `data/pytest-<GUID>` для pytest basetemp, затем compileall.
Для проверки без любых startup-сидов в рабочей БД, используй отдельный процесс:

```powershell
$parserOldWorker = $env:FAST_PARSER_WORKER
$parserOldDb = $env:FAST_PARSER_DB
$env:FAST_PARSER_WORKER = '0'
$env:FAST_PARSER_DB = Join-Path (Get-Location) ('data\test-bootstrap-' + [guid]::NewGuid() + '.sqlite3')
try {
    .\scripts\check.ps1
} finally {
    $env:FAST_PARSER_WORKER = $parserOldWorker
    $env:FAST_PARSER_DB = $parserOldDb
}
```

Пример сохраняет и восстанавливает env другого профиля после проверки. Без изоляции импорт глобального app способен затронуть startup
метаданные локальной БД, хотя HTTP-тесты используют свои временные приложения.

Для одного файла: `python -m pytest tests/test_russian.py -q --basetemp=data/pytest-russian-<unique>`.
Не использовать уже существующий каталог в basetemp: pytest может его очистить.
Последний продуктовый прогон 06.10.2026: 131 passed; новое выполнение фиксировать отдельно.
Проверки — свойства поведения, а не копия строк реализации; не писать тест на каждую
обратимую редакционную правку. Никакой сети в unit-тестах не требуется.

| Группа тестов | Защищаемое поведение |
|---|---|
| test_retention, test_recovery, test_v080 | TTL, регрессия финалов, курсоры/changes, lease/ошибки/миграции |
| test_runtime, test_adapters | HTTP/схема, лимиты/legacy и ошибки |
| test_lifecycle | Безопасная остановка и занятый порт |
| test_verification | Пометки/основания отдельно от успешного fetch |
| test_official_collection, test_laliga_official | Официальная LaLiga, происхождение/контракт |
| test_english, test_italian, test_german, test_russian | Контракты, время, окно, пагинация стран |
| test_spanish_verification | Историческая процедура сопоставления источников |

## 7. Браузер и нагрузка

`node scripts/test_browser.cjs` требует Node.js, модуль Playwright, доступный Edge,
запущенный сервер и реальные данные. Env: PLAYWRIGHT_MODULE (абсолютный путь модуля),
FAST_PARSER_URL (по умолчанию localhost:8000), BROWSER_CHANNEL (по умолчанию msedge).
Эти персональные пути не вшивать в код. UI не имеет обязательной зависимости от Node.

Важное ограничение: общий smoke ищет сохраненные international Sky карточки с
state=verified и предполагает начальные строки. Чистая official_only база этому
не отвечает. Скрипт также временно меняет настройки/verification и делает скриншот
в data/qa; его нельзя называть чисто read-only. Для официальной лиги нужен отдельный
сценарий либо переработанный smoke на изолированной БД. Проверять country по value,
не точной подписи: UI добавляет число лиг. Для async загрузки ожидать response/строки,
не фиксированную задержку. Browser закрывать в finally.

Минимум для измененного адаптера: страна → официальная league ID → команды/счет →
карточка/source_url → пометка покрытия → sync allowed/disabled → 390px → ошибок JS нет.
Наличие 9/10 карточек — проверка выборки, не доказательство полноты всего сезона.

`scripts.benchmark` использует отдельную БД в data/benchmarks и 100000 синтетических
матчей. Это in-process ASGI замер, не скорость реального интернета и не гарантия SLA.
Не запускать большой benchmark после каждой правки docs; только при изменении БД/API
или необходимости сравнения производительности.

## 8. Рабочий цикл изменения

1. Прочитать требования, git status и затронутые функции/тесты.
2. Зафиксировать воспроизведение проблемы без секретов/реального архива.
3. Сформулировать инвариант и небольшое изменение.
4. Добавить содержательные regression-тесты, если меняется поведение.
5. Запустить нужные тесты, затем полный check, если затронут общий контракт.
6. Отдельно выполнить ограниченную реальную проверку/браузер при необходимости.
7. Обновить docs, не объявляя незавершенный пилот выполненным.
8. Проверить git diff --check и содержимое новых файлов; runtime не должен попасть в Git.
9. По просьбе пользователя — commit/push; перед пушем убедиться в нужном remote/branch.

UTF-8 читать и писать явно (`Path.read_text(encoding='utf-8')`). На Windows default
encoding может быть cp1251: в прошлой задаче чтение без encoding испортило русские
диагностические строки, regression-тесты обнаружили это. Не сохранять mojibake обратно.
