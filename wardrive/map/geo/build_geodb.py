#!/usr/bin/env python3
"""Build the offline reverse-geocoding dataset from GeoNames.

Replaces the api.bigdatacloud.net lookup the map used to make for every discovered
node. Output is a single gzipped TSV of populated places with their country and
region names already resolved, so the server needs no lookup tables at runtime:

    lat<TAB>lon<TAB>city<TAB>state<TAB>country

cities1000 (places over ~1000 population) is the useful tradeoff here: the nodes being
geocoded come from IP geolocation, which lands on city-level coordinates anyway, so
finer datasets add size without improving the answer.

Run this only when refreshing the dataset; the output is committed to the tileset
volume, not regenerated at runtime.
"""
import csv
import gzip
import io
import os
import sys
import urllib.request
import zipfile

GEONAMES = "https://download.geonames.org/export/dump/"

def fetch(name):
    url = GEONAMES + name
    print(f"  fetching {url}")
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()

def load_countries():
    """ISO country code -> short country name."""
    out = {}
    for line in fetch("countryInfo.txt").decode("utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        f = line.split("\t")
        if len(f) > 4:
            out[f[0]] = f[4]
    return out

def load_admin1():
    """'<country>.<admin1>' -> (region name, geonameid)."""
    out = {}
    for line in fetch("admin1CodesASCII.txt").decode("utf-8").splitlines():
        f = line.split("\t")
        if len(f) >= 4:
            out[f[0]] = (f[1], f[3])
        elif len(f) >= 2:
            out[f[0]] = (f[1], "")
    return out


# Countries whose place names are also stored in the local language. Kept as a list
# because the per-country alternate-name files are small (RU is 12 MB against 195 MB
# for the global dump), so adding one is cheap -- but each one is another download,
# so they are opted into rather than fetched for all 252.
LOCAL_NAME_COUNTRIES = {"RU": "ru"}


# GeoNames' Russian region names are unreliable: some are truncated ("Челябинская"),
# some are in the wrong case ("Нижегородской"), some are colloquial ("Орловщина",
# "Ставрополье"), some abbreviate ("Чукотский АО"), several misspell, and most
# capitalise "Область" and "Край" where Russian does not. The federal subjects are a
# fixed, known list, so this is the authoritative mapping rather than a patch list --
# keyed by the English admin1 name, which is what the filter value and the UI show.
# build warns about any region present in the data but missing here.
RU_REGION_NAMES = {
    "Adygeya Republic": "Республика Адыгея",
    "Altai": "Республика Алтай",
    "Altai Krai": "Алтайский край",
    "Amur Oblast": "Амурская область",
    "Arkhangelskaya": "Архангельская область",
    "Astrakhan Oblast": "Астраханская область",
    "Bashkortostan Republic": "Республика Башкортостан",
    "Belgorod Oblast": "Белгородская область",
    "Bryansk Oblast": "Брянская область",
    "Buryatiya Republic": "Республика Бурятия",
    "Chechnya": "Чеченская Республика",
    "Chelyabinsk": "Челябинская область",
    "Chukotka": "Чукотский автономный округ",
    "Chuvash Republic": "Чувашская Республика",
    "Dagestan": "Республика Дагестан",
    "Ingushetiya Republic": "Республика Ингушетия",
    "Irkutsk Oblast": "Иркутская область",
    "Ivanovo Oblast": "Ивановская область",
    "Jewish Autonomous Oblast": "Еврейская автономная область",
    "Kabardino-Balkariya Republic": "Кабардино-Балкарская Республика",
    "Kaliningrad Oblast": "Калининградская область",
    "Kalmykiya Republic": "Республика Калмыкия",
    "Kaluga Oblast": "Калужская область",
    "Kamchatka": "Камчатский край",
    "Karachayevo-Cherkesiya Republic": "Карачаево-Черкесская Республика",
    "Karelia": "Республика Карелия",
    "Khabarovsk": "Хабаровский край",
    "Khakasiya Republic": "Республика Хакасия",
    "Khanty-Mansia": "Ханты-Мансийский автономный округ — Югра",
    "Kirov Oblast": "Кировская область",
    "Komi": "Республика Коми",
    "Kostroma Oblast": "Костромская область",
    "Krasnodar Krai": "Краснодарский край",
    "Krasnoyarsk Krai": "Красноярский край",
    "Kurgan Oblast": "Курганская область",
    "Kursk Oblast": "Курская область",
    "Kuzbass": "Кемеровская область — Кузбасс",
    "Leningradskaya Oblast'": "Ленинградская область",
    "Lipetsk Oblast": "Липецкая область",
    "Magadan Oblast": "Магаданская область",
    "Mariy-El Republic": "Республика Марий Эл",
    "Mordoviya Republic": "Республика Мордовия",
    "Moscow": "Москва",
    "Moscow Oblast": "Московская область",
    "Murmansk": "Мурманская область",
    "Nenets": "Ненецкий автономный округ",
    "Nizhny Novgorod Oblast": "Нижегородская область",
    "North Ossetia–Alania": "Республика Северная Осетия — Алания",
    "Novgorod Oblast": "Новгородская область",
    "Novosibirsk Oblast": "Новосибирская область",
    "Omsk Oblast": "Омская область",
    "Orenburg Oblast": "Оренбургская область",
    "Oryol oblast": "Орловская область",
    "Penza Oblast": "Пензенская область",
    "Perm Krai": "Пермский край",
    "Primorye": "Приморский край",
    "Pskov Oblast": "Псковская область",
    "Republic of Tyva": "Республика Тыва",
    "Rostov": "Ростовская область",
    "Ryazan Oblast": "Рязанская область",
    "Sakha": "Республика Саха (Якутия)",
    "Sakhalin Oblast": "Сахалинская область",
    "Samara Oblast": "Самарская область",
    "Saratov Oblast": "Саратовская область",
    "Smolensk Oblast": "Смоленская область",
    "St.-Petersburg": "Санкт-Петербург",
    "Stavropol Kray": "Ставропольский край",
    "Sverdlovsk Oblast": "Свердловская область",
    "Tambov Oblast": "Тамбовская область",
    "Tatarstan Republic": "Республика Татарстан",
    "Tomsk Oblast": "Томская область",
    "Tula Oblast": "Тульская область",
    "Tver Oblast": "Тверская область",
    "Tyumen Oblast": "Тюменская область",
    "Udmurtiya Republic": "Удмуртская Республика",
    "Ulyanovsk": "Ульяновская область",
    "Vladimir Oblast": "Владимирская область",
    "Volgograd Oblast": "Волгоградская область",
    "Vologda Oblast": "Вологодская область",
    "Voronezh Oblast": "Воронежская область",
    "Yamalo-Nenets": "Ямало-Ненецкий автономный округ",
    "Yaroslavl Oblast": "Ярославская область",
    "Zabaykalskiy (Transbaikal) Kray": "Забайкальский край",
}

REGION_OVERRIDES = {"RU": RU_REGION_NAMES}


def load_local_names(cc, lang):
    """geonameid -> name in `lang`, from the per-country alternate-names dump.

    Preferred names win over plain ones; historic and colloquial spellings are skipped
    so a city does not come back under a Soviet-era name.
    """
    raw = fetch(f"alternatenames/{cc}.zip")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        data = z.read(f"{cc}.txt").decode("utf-8")
    out, preferred = {}, set()
    for line in data.splitlines():
        f = line.split("\t")
        if len(f) < 4 or f[2] != lang or not f[3]:
            continue
        gid = f[1]
        historic   = len(f) > 7 and f[7] == "1"
        colloquial = len(f) > 6 and f[6] == "1"
        if historic or colloquial:
            continue
        is_pref = len(f) > 4 and f[4] == "1"
        if gid in preferred and not is_pref:
            continue
        if is_pref:
            preferred.add(gid)
        out[gid] = f[3]
    return out

def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "cities.tsv.gz"
    countries = load_countries()
    admin1 = load_admin1()
    print(f"  {len(countries)} countries, {len(admin1)} regions")

    local = {}
    for cc, lang in LOCAL_NAME_COUNTRIES.items():
        names = load_local_names(cc, lang)
        local[cc] = names
        print(f"  {len(names)} {lang} names for {cc}")

    raw = fetch("cities1000.zip")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        data = z.read("cities1000.txt").decode("utf-8")

    rows = 0
    missing_regions = set()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with gzip.open(out_path, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        for line in data.splitlines():
            f = line.split("\t")
            if len(f) < 15:
                continue
            gid, name, lat, lon, cc, admin = f[0], f[1], f[4], f[5], f[8], f[10]
            country = countries.get(cc, cc)
            state, state_gid = admin1.get(f"{cc}.{admin}", ("", ""))
            # Local-language city and region names, blank for countries not opted in.
            names = local.get(cc) or {}
            city_local  = names.get(gid, "")
            state_local = names.get(state_gid, "") if state_gid else ""
            # The language is recorded per row rather than assumed from the country, so
            # adding a second entry to LOCAL_NAME_COUNTRIES cannot leave the consumer
            # reading German names out of a column something called "ru".
            # Curated region names win over GeoNames'. Recorded even when GeoNames had
            # nothing, which is why loc_lang is decided after the override is applied.
            override = REGION_OVERRIDES.get(cc, {}).get(state)
            if override:
                state_local = override
            elif state and cc in REGION_OVERRIDES:
                missing_regions.add(state)
            loc_lang = LOCAL_NAME_COUNTRIES.get(cc, "") if (city_local or state_local) else ""
            # The ISO code rides along with the English name so the browser can render
            # the country in the reader's language via Intl.DisplayNames. This file has no
            # localised names of its own, and a code costs three bytes a row.
            w.writerow([f"{float(lat):.4f}", f"{float(lon):.4f}", name, state, country, cc,
                        city_local, state_local, loc_lang])
            rows += 1

    if missing_regions:
        print(f"  WARNING: {len(missing_regions)} region(s) with no curated name, "
              f"falling back to GeoNames: {', '.join(sorted(missing_regions))}")

    size = os.path.getsize(out_path)
    print(f"wrote {out_path}: {rows} places, {size / 1e6:.1f} MB")

if __name__ == "__main__":
    main()
