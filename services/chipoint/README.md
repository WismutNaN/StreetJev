# Локальный Chipoint 2

Необязательный эксперт Street View. Сервис, установщик и версии — в этом репозитории;
окружение, энкодеры и галерея — в `.runtime/chipoint/`. Снимки не отправляются во внешние API.

## Запуск

```powershell
# Первая установка: SigLIP2 SO400M + projection head + галерея 4,89 млн кадров.
uv run python services/chipoint/manage.py setup

# Сервис слушает только loopback, порт 18766.
uv run python services/chipoint/manage.py start
uv run python services/chipoint/manage.py status

# Оба сервиса и приложение; при закрытии останавливаются только запущенные скриптом процессы.
.\run.ps1 -WithModel -WithChipoint
```

В приложении включите **Chipoint** в шапке. Если сервис отсутствует, недоступен или слишком
медленный, imajev продолжает анализ. Таймаут второго эксперта настраивается отдельно (20 с).
Одновременно выполняется максимум один запрос Chipoint; очередь кадров не создаётся.
Отключение переключателя исключает его вклад из текущего раунда и сохраняет результаты imajev.
Уже начавшийся GPU-запрос заканчивается на сервере, его ответ игнорируется; переключатель
не выгружает сервис из VRAM. Для работы без загруженного Chipoint запускайте без `-WithChipoint`.

Дополнительный энкодер — отдельная установка и запуск:

```powershell
uv run python services/chipoint/manage.py setup --towers so400m256 gopt
uv run python services/chipoint/manage.py start --towers so400m256 gopt
```

Каждая галерея занимает около 5 ГБ; дополнительно нужны веса энкодера и PyTorch. Проверенный
профиль — один `so400m256`, CUDA. `--device cpu` доступен, его скорость не измерена.
Версии весов закреплены в `manifest.json`; версии установленного окружения —
`.runtime/chipoint/installed.txt`. При запуске служба читает только локальные веса.

## Что именно реализовано

1. SigLIP2 `webli` -> нормализованный embedding -> опубликованный projection head 512-D.
2. Потоковый поиск cosine top-200 по каждой выбранной memory-mapped галерее, блоками по 250 тыс. строк.
3. Z-score внутри каждого списка, сумма по кандидатам, сохранение top-64 координат.
4. Адаптер приложения превращает scores в веса через softmax с температурой 2.
   Это фиксированная эвристика, **не калибровка вероятности правильного ответа**.
5. Локальный spatial index Natural Earth сопоставляет точки со странами и admin-1.
   Точка вне полигонов увеличивает unknown; ближайшая страна не выдумывается.
6. Перед накоплением: линейное объединение с imajev, веса 35% и 65%, с учётом unknown.

Все 64 точки, их scores, версия модели и диагностика энкодеров сохраняются в SQLite как
исходный ответ. Финальная точка и непрерывное пространственное поле пока не строятся.

## Ограничение опубликованной модели

Это **street retrieval**, а не полный benchmark pipeline. В официальном демо
`ENABLE_RERANKER = False`: точные формулы 23 признаков обученного reranker не опубликованы.
Не подменяем их догадками и не приписываем этой реализации заявленную accuracy полного Chipoint.
Также не загружаются general-photo arm и router: вход приложения — Street View.
Калибровка, spatial kernels, DINOv3 и imajev reranking требуют отдельных проверок.

Источники: [модель и лицензия MIT](https://huggingface.co/chiikabu-labs/chipoint-2),
[контракт признаков](https://huggingface.co/chiikabu-labs/chipoint-2/blob/a150120cd968d48310d092caf26854f109c90b51/RUN_GUIDE.md),
[референсное демо авторов](https://huggingface.co/spaces/chiikabu-labs/chipoint-v2-geolocation/blob/65dd8df82f36608ad819f5ecd4371da16f73704e/app.py).
Проекции и алгоритм retrieval реализованы по опубликованному устройству этих компонентов.

## Контракт и проверки

`GET /health` сообщает model/ready/busy. `POST /v1/geolocate` принимает JSON с полем `image`
(JPEG в base64), возвращает `schema=streetjev.chipoint.v1`, `model`, `mode=street_retrieval`,
`reranker=false`, `candidates=[{id,lat,lon,score}]`, `elapsed_ms`, `towers`.
Занятый сервер отвечает 503; служба не принимает очередь.

```powershell
uv run pytest -q
uv run python scripts/smoke_optional.py
uv run python scripts/smoke_chipoint.py  # нужен запущенный Chipoint
```

Проверенный GPU-запрос по синтетическому рисунку: около 5,7 с первый и 2,2 с повторный.
Это проверка выполнения, а не точности геолокации. Отчёты находятся в `artifacts/chipoint/`.
