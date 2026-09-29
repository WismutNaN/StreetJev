# Модуль: внешние адаптеры

Расположение: `src/streetjev/infrastructure/`, `services/imajev/`, `services/chipoint/`.
Отвечает за преобразование внешних данных в значения домена.

Публичные реализации: `ImajevExpert`, `DemoExpert`, `ScreenCapture`, `SqliteRoundRepository`,
`load_geography`. Зависимости: httpx, Pillow, mss, Windows API, sqlite3.

imajev принимает multipart JPEG и JSON, возвращает полный набор вероятностей. Нельзя молча
принимать top-k как полное распределение или заменять unknown нулём. Все региональные вопросы
условны по стране. Выдача модели проходит валидацию до изменения раунда.

SQLite сохраняет raw response, нормализованный evidence и snapshot в одной транзакции.
Изображения не сохраняются. Геоданные поставляются с пакетом, сетевой доступ карте не нужен.
Адаптеры не меняют GUI и не принимают решение о сбросе раунда.

`ChipointExpert` принимает top-64 координат/score от локального сервиса, проверяет контракт,
сопоставляет точки через `GeographicLocator` (Shapely STRtree) и передаёт country/admin1
распределения. Непокрытая полигоном масса становится unknown. Полный список кандидатов
остаётся в raw JSON таблицы `supplementary_observations`, отдельно от ответа imajev.
Сервис использует закреплённые веса и не загружает файлы во время inference.
Подробнее: [Chipoint](../services/chipoint/README.md).
