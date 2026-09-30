# Географические данные

Источник: [Natural Earth](https://www.naturalearthdata.com/).
[Условия использования](https://www.naturalearthdata.com/about/terms-of-use/): public domain.

Исходный репозиторий: https://github.com/nvkelso/natural-earth-vector
Коммит: `ca96624a56bd078437bca8184e78163e5039ad19`.

Использованы `ne_50m_admin_0_countries.geojson` и `ne_10m_admin_1_states_provinces.geojson`.
Геометрии упрощены с сохранением топологии, координаты округлены до четырёх знаков.
Некоторые отдельные территории с общим ISO-кодом объединены. Для объектов без ISO
используются идентификаторы Natural Earth. Это обзорная карта; границы и названия
соответствуют исходному набору данных и не являются юридическим утверждением.

Сборка: `uv run python scripts/build_geography.py`.

`country_iso3.json` — соответствие кодов этой же ревизии Natural Earth для запросов ADM2.

## Административные подрегионы

Подрегионы ADM2 загружаются отдельно по стране из [geoBoundaries gbOpen](https://www.geoboundaries.org/).
[Описание API](https://www.geoboundaries.org/api.html), [репозиторий](https://github.com/wmgeolab/geoBoundaries).
Используется `simplifiedGeometryGeoJSON`; этот набор уже упрощён поставщиком и не является кадастровой картой.
Исходные файлы не включены в поставку StreetJev. Кэш `data/boundaries/<ISO3>-ADM2.json.gz` сохраняет
метаданные `boundaryID`, год, поставщика, `boundaryLicense`, `licenseSource`, `boundarySourceURL` и URL загрузки.
Условия исходных наборов различаются: например, AUT ADM2 — CC BY-SA 2.0, RUS ADM2 — ODbL 1.0.
Атрибуция geoBoundaries расположена на карте, сведения о загруженных странах — в её подсказке и экспорте.
Не следует считать все наборы gbOpen имеющими одну и ту же лицензию или дату актуальности.

`driving_side.json` — локальная копия справочника стороны движения из проекта пользователя
[guess-map](https://github.com/WismutNaN/guess-map), `src-tauri/src/seed/driving_side.rs`.
Точная ревизия и дата импорта записаны в JSON. Неизвестные коды и Антарктида не получают
предполагаемую сторону движения. При запуске guess-map не требуется.

Поправка покрытия использует растровый слой Google Street View © Google, как в guess-map
(`src-tauri/src/tile_server.rs`). Это сетевой, не документированный публичным API слой;
при недоступности приложение продолжает работу без поправки. Изображения пользователя
не передаются: запрос содержит только координаты тайла. Сами тайлы не сохраняются.
# Подробная подложка

Векторные тайлы MVT загружаются при просмотре с
`https://vector.openstreetmap.org/shortbread_v1/{z}/{x}/{y}.mvt`.
Используются дорожные линии и подписи, названия населённых пунктов, линии/полигоны рек и озёр.
Ледники, рельеф, растительность, здания и POI не отрисовываются. Русское и местное имена берутся из MVT;
автоматический перевод отсутствующих названий не выполняется. Данные Google Maps не используются в подложке.
© OpenStreetMap contributors; данные доступны по ODbL: https://www.openstreetmap.org/copyright.
Атрибуция находится на карте. Политика использования: https://operations.osmfoundation.org/policies/vector/.
Тайлы не включены в репозиторий, загрузка только текущего вида с локальным HTTP-кэшем
и кэшем разобранных линий/подписей, с учётом срока действия.
