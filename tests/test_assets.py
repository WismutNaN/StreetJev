from streetjev.infrastructure.catalog import load_geography


def test_bundled_catalog_has_real_world_coverage():
    catalog, geography = load_geography()
    assert 200 < len(catalog.countries) <= 254
    assert len(catalog.regions) > 4000
    assert {"AT", "DE", "RU", "US", "BR", "AU", "JP"} <= catalog.countries.keys()
    assert len(catalog.regions_of("AT")) == 9
    assert len(catalog.regions_of("DE")) == 16
    assert all(len(catalog.regions_of(code)) <= 254 for code in catalog.countries)
    assert len(geography["features"]) == len(catalog.countries) + len(catalog.regions)
