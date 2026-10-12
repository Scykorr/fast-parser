# Источники: текущие контракты и добавление адаптера

Код 0.13.2, описание 13.10.2026. Реальные контрольные проверки источников в паспортах
датированы 06.10.2026; это не утверждение об их доступности сегодня.
Политика обязательна: [OFFICIAL_SOURCES.md](OFFICIAL_SOURCES.md), [AGENTS.md](../AGENTS.md).

## 1. Матрица подключений

| Страна/лига | Source | Competition ID образца 2026 | Адаптер/доступ |
|---|---|---|---|
| Испания / Примера | laliga_reference | ll:primera-division:2026 | official.py, рабочий HTML |
| Испания / Сегунда | laliga_reference | ll:segunda-division:2026 | official.py, та же квота |
| Англия / Premier League | premierleague_official | pl:premier-league:2026 | english.py, public FPL JSON |
| Англия / Championship | efl_official | efl:championship:2026 | english.py, public EFL JSON |
| Италия / Serie A | seriea_official | sa:serie-a:2026 | italian.py, public JSON |
| Италия / Serie B | legab_official | sb:serie-b:2026 | italian.py, HTML |
| Германия / Bundesliga | bundesliga_official | dfl:bundesliga:2026 | german.py, SSR JSON |
| Германия / 2. Bundesliga | bundesliga_official | dfl:2bundesliga:2026 | german.py, та же квота |
| Россия / Первая лига | fnl_official | fnl:first-league:2026 | russian.py, public JSON |
| Россия / РПЛ | rpl_official | rpl:premier-league:2026 | Только catalog/reason; worker/адаптера нет |
| Сохраненный legacy | openligadb / skysports_html | ol:... / sky:... | adapters.py, official_only исключает сбор |

Registry `sources.py`: constants, SOURCE_BY_PREFIX, OFFICIAL_SOURCES/PREFIXES,
source_for_comp. Имя laliga_reference историческое, это текущий production organiser,
не только инструмент независимой проверки. РПЛ в официальном реестре описывает
происхождение заявленной записи, а не подключенный работоспособный источник.

## 2. LaLiga

HTML `https://www.laliga.com/laliga-easports/resultados` и
`https://www.laliga.com/laliga-hypermotion/resultados`, отдельные URL сезона/тура.
`#__NEXT_DATA__` → props.pageProps; проверяются competition/season/gameweek.
PreMatch/FullTime известны, другие raw_status остаются unknown. Score финала int >=0,
название nickname/name. Времена с явным offset, source_url добавляет collector.
Match ID `ll:<provider-id>` пока без года; это отличие от других адаптеров, не
менять без анализа существующих клиентов/markers/changes.

official_plan сначала содержит 2 основных страницы, затем туры около окна с запасом
7 дней по приблизительной дате тура. Фильтр Match точный. Основная страница текущего
тура не должна немедленно дублироваться запросом его URL. Минуты/тайм не подтверждены.
Паспорт: [OFFICIAL_SOURCES.md](OFFICIAL_SOURCES.md), историческая сверка
[SPANISH_VERIFICATION.md](SPANISH_VERIFICATION.md).

## 3. Англия

Premier League/FPL: `https://fantasy.premierleague.com/api/bootstrap-static/`, затем
`/fixtures/`. В БД из bootstrap сохраняются справочник команд/сезон, не персональные
данные игроков. Кеш 24h. season-year определяется по расписанию deadline.
FPL finished = финал; finished_provisional без окончательного finished = unknown;
started = live; minutes только если реально дан в live. Тайм/конец null.
Полный fixture response фильтруется окном и pending; неизвестные kickoff требуют
проверки поведения при будущем расширении, не считать автоматически днем запроса.

EFL: `https://multi-club-matches.webapi.gc.eflservices.co.uk/v2/matches`,
competitionID=10, seasonID, from/to, page.size=100. Поле kickOffDateUTC не содержит
offset, но явно называется UTC; TBC скрывает точный kickoff. PreMatch/FullTime известны.
next URL проверяет HTTPS, host/path, область/сезон/размер и следующий page.number.
efl_cursor сохраняет next/base/seen/pages, максимум 50 страниц и запрет повторов ID.
Id команды — числовой token официального crest; без него партия не импортируется.
Смена области сбрасывает cursor. Незавершенные старые матчи расширяют запрос назад.
Паспорт: [ENGLISH_SOURCES.md](ENGLISH_SOURCES.md).

## 4. Италия

Serie A: `https://api-sdp.legaseriea.it/v1/serie-a/football`, явный competitionId.
Сначала seasons для нужного YYYY/YYYY+1, затем seasonId URL matches. seasonId кеш 24h,
токены/ключи не требуются. competitionId/seasonId/seasonName проверяются в ответе.
YYYY-MM-DDZ при isUnknownKickOffTime=true — date-only, не ошибочный ISO timestamp.
finished требует score, не подтвержденные фазы остаются unknown.

Lega B: `https://www.legab.it/seriebkt/calendario/<year>-<next>/stagione-regolare`.
Выбранный option подтверждает текущий тур; matchday-list/data-match-id подтверждают
карточки. Италоязычная текстовая дата парсится с Europe/Rome и сезонной проверкой;
разрешены только обнаруженные соседние туры. Без явного часа kickoff=null. Id клуба — slug официального логотипа; «Ore» не записывается в raw_status.
Новые финальные стадии нельзя добавить как еще один номер регулярного тура.
Паспорт: [ITALIAN_SOURCES.md](ITALIAN_SOURCES.md).

## 5. Германия

`https://www.bundesliga.com/en/bundesliga/matchday` и `/en/2bundesliga/matchday`,
отдельные URL /<year>-<next>/<round>. HTML script ng-state содержит public transfer-state.
Выбирается полная связка competitionId/seasonId/matchday; на странице могут быть
данные другой лиги и другой недели, их нельзя взять первым найденным массивом.
Внутренние Firebase URL из ключей не запрашиваются.
plannedKickOff/kickOff с offset, matchDateFixed управляет точностью, date-only день
Europe/Berlin. PRE_MATCH/FINAL_WHISTLE подтверждены; неизвестная фаза = unknown.
Две лиги, один source/квота. german_plan обходит регулярные туры 1–34, соседние туры
проверяет относительно окна. Playoff-route/live остаются открытыми.
Паспорт: [GERMAN_SOURCES.md](GERMAN_SOURCES.md).

## 6. Россия

FNL: сайт `https://fnl.pro/pari/matches` использует `https://fnl-app.fnl.pro`.
activeSeason?leagueId=100 → seasonId; календарь matches/date с mouth/year,
limit=10/offset/move=ALL. Группы дней расплющиваются в карточки; count считается
по матчам, не количеству групп. Каждая страница обязана дать ожидаемый объем.
Сезонный/месячный контракт проверяется, fnl_plan хранит увиденные ID/offset/due.

Дата без зоны интерпретируется Europe/Moscow, что подтверждено public JS сайта.
00:00/date-only не является точным временем. date важнее estimatedDateTo/range.
FUTURE нули счета скрываются, END требует счет; BEGIN вместе с явной фазой дает
live/paused/period. Только простая числовая строка минуты принимается, неизвестный
формат не превращается в добавленное время по догадке. Реальный live еще проверить.
Pending с точным старым kickoff добавляют месяц к плану. Без известного одного дня
матч нельзя произвольно импортировать в выбранное окно.

РПЛ: premierliga.ru/matches на контрольном запросе ушла в showcaptcha; РФС показывает
старые сезоны, UEFA отказала. Нет parser/fetch task/CLI-loop РПЛ. Для подключения
понадобится рабочий официальный контракт, а не reset_source. См.
[RUSSIAN_SOURCES.md](RUSSIAN_SOURCES.md). РПЛ-недоступность сейчас seed в app.py,
reason в russian.py и префиксные запреты в API: удалить их только вместе с адаптером.

## 7. Как добавлять источник: законченная последовательность

1. Зафиксировать страну/лиги/сезон и организатора; найти официальную публичную
   страницу и атрибуцию. Wikipedia — справочник структуры лиг, не источник результатов.
2. Прочитать robots/условия доступа, сделать небольшую контролируемую проверку.
   Не тратить бюджет и не превращать бесплатный trial в обязательную зависимость.
3. Если сайт грузит данные через JSON — обнаружить URL/параметры в публичном коде
   сайта. Не подбирать внутренние ID, токены или приватные mobile endpoints.
4. Доказать контракты: команды, ID, сезон, порядок тура/страниц, время/зона,
   future/final/live, значения отсутствующего счета и TBC. Проверить не только HTTP 200.
5. При блокировке остановиться, зарегистрировать доступную часть и явную причину.
   Не представлять пустую выборку как доказательство отсутствия матчей.
6. Добавить чистый parser с SchemaError, используя существующий Match. Проверить
   весь batch до upsert; API-этап не должен частично принять поврежденную партию.
7. Назначить namespace и решить повторное использование provider ID между сезонами.
   Если меняется старый namespace, нужен план миграции и resync клиентов.
8. Добавить source constant/prefix в sources.py. Несколько лиг одного домена/организатора
   делят квоту, если условия не подтверждают отдельные независимые лимиты.
9. Добавить persisted cache/plan/cursor в collector. fetch через общую функцию,
   due до network, bounded pages, end-of-page handling, restart и окно/pending.
10. Зарегистрировать source_cycle и run jobs, app startup catalog, diagnostics,
    manual sync reset нужного плана, CLI dispatch. Добавить capabilities app.py;
    одного изменения sources.py недостаточно.
11. Для временно неподключенного источника нужны supported/allowed/reason и видимая
    UI причина, выключенный sync, честный отчет. Не обещать runtime adapter.
12. Тесты: контракт/время/статусы/score, pagination, window, shared quota, restart,
    unknown fields и malformed response. Затем небольшой реальный прогон и UI.
13. Обновить паспорт, ТЗ, roadmap, риски, отчет, версии по SemVer при изменении поведения.
    Пометка full coverage отдельно, по доказанной выборке/независимой сверке.

## 8. Минимальная матрица regression-тестов

| Вход/сценарий | Ожидаемое поведение |
|---|---|
| Чужая лига/сезон/тур | SchemaError до записи |
| Пустой ответ, count>0 | Не выдавать за корректный пустой сезон |
| Пустой ответ, доказанный count=0 | Допустим по контракту этого API |
| Повторы ID внутри/между страницами | Обнаружить, bounded failure, без бесконечного обхода |
| next на другой host/окно/назад | Отвергнуть до запроса |
| Naive время без доказанной зоны | Отвергнуть/оставить null по явному контракту |
| Date-only/TBC/00:00 placeholder | Не импортировать как точную полночь |
| Будущий 0:0 placeholder | Не показывать сыгранный счет |
| Финал без счета | Карантин, предыдущий финал не терять |
| Неизвестная фаза | unknown/raw_status, не угадывать live |
| Счет 0, bool, отрицательный | 0 принять, bool/отрицательный отвергнуть |
| Матч вне окна, но pending | Повторно проверить согласно persisted pending contract |
| Истекший матч в ответе | Не продлевать TTL/не воскрешать без допустимого исключения |
| 403/CAPTCHA | blocked без обхода/автоматического reset |
| 429 с числом/HTTP-датой | Retry-After сохраняется после рестарта |
| Перезапуск в середине страницы | Не потерять принятую запись/безопасный cursor |
| Две лиги общего source | Один атомарный счетчик квоты |

Синтетические fixtures должны описывать структуру с вымышленными командами, а не
быть полным сохраненным сезоном реальных результатов. Достаточно нескольких
репрезентативных карточек. Исторические JSON/HTML на диске нарушают смысл TTL.

## 9. Модель верификации

Проверенная выборка должна иметь дату, сезон/туры, виды данных, источник основания,
количество совпадений/расхождений и ограничения. Если имеется только официальный
сайт и синтетические тесты, честная пометка — unverified либо обоснованная partial,
а не автоматически verified. При доступном независимом официальном источнике
сравнивать ID/ручные aliases, даты и счет. Несопоставимые названия записывать как
расхождение; не применять автоматическое fuzzy-слияние к production Match.

Не смешивать время обновления сайта, время получения HTTP и время окончания матча.
Последний успешный source fetch не означает, что проверен каждый Match этого source.
Полное расписание, свежий live, финальные стадии, переносы и полный сезон — отдельные
критерии. Продвигать пометку только вместе с основанием/выборкой.
