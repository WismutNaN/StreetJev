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
Используются только дорожные линии и названия населённых пунктов, без фонового картографического стиля.
© OpenStreetMap contributors; данные доступны по ODbL: https://www.openstreetmap.org/copyright.
Атрибуция находится на карте. Политика использования: https://operations.osmfoundation.org/policies/vector/.
Тайлы не включены в репозиторий, загрузка только текущего вида с локальным HTTP-кэшем
и кэшем разобранных линий/подписей, с учётом срока действия.
