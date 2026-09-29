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
