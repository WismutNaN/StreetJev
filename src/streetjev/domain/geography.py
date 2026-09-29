from dataclasses import dataclass


@dataclass(frozen=True)
class Place:
    code: str
    name: str
    label: str
    country_code: str | None = None


class GeoCatalog:
    def __init__(self, countries: tuple[Place, ...], regions: tuple[Place, ...]):
        self.countries = {p.code: p for p in countries}
        self.regions = {p.code: p for p in regions}
        if not countries or len(self.countries) != len(countries):
            raise ValueError("Каталог стран пуст или содержит повторные коды")
        if len(self.regions) != len(regions):
            raise ValueError("Повторные коды регионов")
        if any(p.country_code not in self.countries for p in regions):
            raise ValueError("Регион ссылается на неизвестную страну")
        self._by_country = {code: tuple(p for p in regions if p.country_code == code) for code in self.countries}

    def regions_of(self, country: str) -> tuple[Place, ...]:
        return self._by_country[country]
