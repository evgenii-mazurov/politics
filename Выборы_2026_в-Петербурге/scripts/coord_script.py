"""
Геокодирование адресов УИК через официальный Yandex Geocoder HTTP API:
- Возвращаются координаты именно объекта, а не центр окна карты после поиска.
- API отдает поле precision (exact/number/near/range/street/other),
  по которому видно, насколько точно найден адрес. Таким образом, сомнительные строки
  можно сразу отфильтровать и проверить руками.

Получить бесплатный API-ключ: https://developer.tech.yandex.ru/services/
(сервис "JavaScript API и HTTP Геокодер")

Что заменить для нового парсинга:
- API_KEY
- INPUT_PATH
- OUTPUT_PATH
"""

import re
import time
import pandas as pd
import requests

API_KEY = "здесь указать ключ в кавычках"  # указать ключ Yandex Geocoder
INPUT_PATH = r"здесь указать путь к файлу, из которого берем адреса"
OUTPUT_PATH = r"здесь указать путь к файлу, в который сохранятся адреса с координатами"

GEOCODER_URL = "https://geocode-maps.yandex.ru/1.x/"


def normalize_address(address: str) -> str:
    """Приводит адрес к виду, более удобному для геокодера."""
    address = address.strip()

    # убираем почтовый индекс в начале (он не помогает геокодеру, иногда мешает)
    address = re.sub(r"^\d{6},\s*", "", address)

    # диапазон домов "108–110" / "108-110" -> берём первый номер
    address = re.sub(r"(д\.\s*\d+)[–-]\d+", r"\1", address)

    # явно добавляем город, если его нет у меня это был Петербург; если нужно указать другой - прописать; если город есть в самом адресе - можно не открывать
    # if "санкт-петербург" not in address.lower() and "спб" not in address.lower():
        address = f"Санкт-Петербург, {address}"

    return address


def geocode_address(address: str, api_key: str) -> dict:
    """Запрашивает координаты и precision у Yandex Geocoder API."""
    params = {
        "apikey": api_key,
        "geocode": address,
        "format": "json",
        "results": 1,
    }
    resp = requests.get(GEOCODER_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    feature_member = data["response"]["GeoObjectCollection"]["featureMember"]
    if not feature_member:
        return {"latitude": None, "longitude": None, "precision": "not_found", "found_address": None}

    geo_object = feature_member[0]["GeoObject"]
    lon, lat = map(float, geo_object["Point"]["pos"].split())
    precision = geo_object["metaDataProperty"]["GeocoderMetaData"].get("precision", "unknown")
    found_address = geo_object["metaDataProperty"]["GeocoderMetaData"].get("text")

    return {
        "latitude": lat,
        "longitude": lon,
        "precision": precision,
        "found_address": found_address,
    }


def main():
    df = pd.read_excel(INPUT_PATH)

    cache = {}
    results = []

    for _, row in df.iterrows():
        raw_address = str(row["address"]).strip()
        uik = row["uik_name"]
        district = row["district"]

        norm_address = normalize_address(raw_address)

        if norm_address in cache:
            geo = cache[norm_address]
        else:
            try:
                geo = geocode_address(norm_address, API_KEY)
            except Exception as e:
                geo = {"latitude": None, "longitude": None, "precision": f"error: {e}", "found_address": None}
            cache[norm_address] = geo
            time.sleep(0.3)  # вежливая пауза между запросами

        status = "OK" if geo["precision"] in ("exact", "number") else "ПРОВЕРИТЬ"
        print(f'{uik}: {raw_address} -> {geo["latitude"]}, {geo["longitude"]} '
              f'[{geo["precision"]}] {status}')

        results.append({
            "uik_name": uik,
            "district": district,
            "address": raw_address,
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "precision": geo["precision"],
            "found_address": geo["found_address"],
            "needs_review": status == "ПРОВЕРИТЬ",
        })

    result_df = pd.DataFrame(results)
    result_df.to_excel(OUTPUT_PATH, index=False)

    n_review = result_df["needs_review"].sum()
    print(f"\nГотово! Сохранено в {OUTPUT_PATH}")
    print(f"Адресов, требующих проверки (precision не exact/number): {n_review} из {len(result_df)}")


if __name__ == "__main__":
    main()