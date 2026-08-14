"""
Bot de análisis de mercado para Wallapop.
Busca un artículo, recorre todas las páginas de resultados y calcula
estadísticas de precio (medio, mediana, min, max) y cuántos hay listados.

Uso:
    python3 wallapop_market.py "ps5 slim disco"
    python3 wallapop_market.py "ps5 slim disco" --max-price 600 --pages 10
"""

import argparse
import csv
import statistics
import sys
import time

import requests

API_BASE = "https://api.wallapop.com/api/v3"

# Cabeceras de dispositivo capturadas del navegador. No incluyen ningún
# token de sesión/autorización -- el endpoint de búsqueda es público.
HEADERS = {
    "accept": "application/json; sequence=v2",
    "accept-language": "es,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    "deviceos": "0",
    "origin": "https://es.wallapop.com",
    "referer": "https://es.wallapop.com/",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
    "x-appversion": "825840",
    "x-deviceid": "fcd284c5-4ac0-45ae-aba5-75d02e547088",
    "x-deviceos": "0",
}

# Madrid centro por defecto
DEFAULT_LAT = 40.4168
DEFAULT_LON = -3.7038


def get_search_id(keywords, latitude, longitude, session):
    """Llama a /search/components para obtener el search_id que exige /search/section."""
    resp = session.get(
        f"{API_BASE}/search/components",
        params={
            "keywords": keywords,
            "order_by": "most_relevance",
            "source": "search_box",
        },
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    for component in data.get("components", []):
        if component.get("id") == "organic_search_results":
            qp = component["type_data"]["query_params"]
            return qp["search_id"], qp.get("latitude", latitude), qp.get("longitude", longitude)
    raise RuntimeError("No se encontró 'organic_search_results' en la respuesta de components")


def fetch_page(session, keywords, search_id, latitude, longitude, min_price=None, max_price=None, next_page=None):
    params = {
        "keywords": keywords,
        "source": "search_box",
        "order_by": "most_relevance",
        "search_id": search_id,
        "latitude": latitude,
        "longitude": longitude,
        "section_type": "organic_search_results",
        "search_country": "ES",
    }
    if min_price:
        params["min_sale_price"] = min_price
    if max_price:
        params["max_sale_price"] = max_price
    if next_page:
        params["next_page"] = next_page

    resp = session.get(f"{API_BASE}/search/section", params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def search_all(keywords, min_price=None, max_price=None, max_pages=10, latitude=DEFAULT_LAT, longitude=DEFAULT_LON, delay=1.0):
    session = requests.Session()
    search_id, latitude, longitude = get_search_id(keywords, latitude, longitude, session)

    all_items = []
    next_page = None

    for page_num in range(1, max_pages + 1):
        data = fetch_page(session, keywords, search_id, latitude, longitude, min_price, max_price, next_page)
        items = data.get("data", {}).get("section", {}).get("items", [])
        if not items:
            break

        all_items.extend(items)
        print(f"  Página {page_num}: {len(items)} artículos (total acumulado: {len(all_items)})")

        next_page = data.get("meta", {}).get("next_page")
        if not next_page:
            break

        time.sleep(delay)  # no machacar la API

    return all_items


def filter_by_title(items, must_contain=None, must_not_contain=None):
    """Filtra items cuyo título no menciona lo que buscamos de verdad
    (Wallapop mezcla PS3/PS4/Xbox en resultados de 'ps5 slim disco')."""
    if not must_contain and not must_not_contain:
        return items

    must_contain = [t.lower() for t in (must_contain or [])]
    must_not_contain = [t.lower() for t in (must_not_contain or [])]

    filtered = []
    for item in items:
        title = item.get("title", "").lower()
        if must_contain and not any(term in title for term in must_contain):
            continue
        if must_not_contain and any(term in title for term in must_not_contain):
            continue
        filtered.append(item)
    return filtered


def analyze(items):
    prices = [i["price"]["amount"] for i in items if i.get("price")]
    if not prices:
        return None

    return {
        "count": len(items),
        "avg_price": round(statistics.mean(prices), 2),
        "median_price": round(statistics.median(prices), 2),
        "min_price": min(prices),
        "max_price": max(prices),
        "stdev": round(statistics.pstdev(prices), 2) if len(prices) > 1 else 0,
    }


def export_csv(items, path):
    items = sorted(items, key=lambda i: i["price"]["amount"])
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["titulo", "precio_eur", "ciudad", "region", "es_madrid", "url"])
        for item in items:
            loc = item.get("location", {})
            city = loc.get("city", "")
            region = loc.get("region", "")
            es_madrid = "SI" if "madrid" in city.lower() or "madrid" in region.lower() else ""
            url = f"https://es.wallapop.com/item/{item['web_slug']}"
            writer.writerow([item.get("title", ""), item["price"]["amount"], city, region, es_madrid, url])
    print(f"\nCSV exportado: {path} ({len(items)} filas)")


def print_cheapest(items, n=10):
    priced = [i for i in items if i.get("price")]
    priced.sort(key=lambda i: i["price"]["amount"])
    print(f"\nLos {n} más baratos (posibles gangas para comprar y revender):")
    for item in priced[:n]:
        price = item["price"]["amount"]
        title = item["title"][:60]
        city = item.get("location", {}).get("city", "?")
        url = f"https://es.wallapop.com/item/{item['web_slug']}"
        print(f"  {price:>7.2f} EUR | {city:<12} | {title}")
        print(f"           {url}")


def main():
    parser = argparse.ArgumentParser(description="Analiza el mercado de un artículo en Wallapop")
    parser.add_argument("keywords", help="Término de búsqueda, ej: 'ps5 slim disco'")
    parser.add_argument("--min-price", type=float, default=None, help="Precio mínimo a considerar (filtra accesorios/soportes baratos)")
    parser.add_argument("--max-price", type=float, default=None, help="Precio máximo a considerar")
    parser.add_argument("--pages", type=int, default=10, help="Número máximo de páginas a recorrer (cada una ~40 items)")
    parser.add_argument("--lat", type=float, default=DEFAULT_LAT, help="Latitud (por defecto Madrid centro)")
    parser.add_argument("--lon", type=float, default=DEFAULT_LON, help="Longitud (por defecto Madrid centro)")
    parser.add_argument("--top", type=int, default=10, help="Cuántos artículos más baratos mostrar")
    parser.add_argument("--title-contains", nargs="+", default=None,
                         help="El título debe contener al menos uno de estos términos, ej: --title-contains ps5 'playstation 5'")
    parser.add_argument("--title-excludes", nargs="+", default=None,
                         help="Descarta anuncios cuyo título contenga alguno de estos términos, ej: --title-excludes ps4 ps3 xbox")
    parser.add_argument("--export-csv", default=None, help="Ruta donde guardar el listado completo en CSV")
    args = parser.parse_args()

    print(f"Buscando '{args.keywords}' en Wallapop...")
    items = search_all(
        args.keywords,
        min_price=args.min_price,
        max_price=args.max_price,
        max_pages=args.pages,
        latitude=args.lat,
        longitude=args.lon,
    )

    if args.title_contains or args.title_excludes:
        before = len(items)
        items = filter_by_title(items, args.title_contains, args.title_excludes)
        print(f"\nFiltrado por título: {before} -> {len(items)} anuncios")

    stats = analyze(items)
    if not stats:
        print("No se encontraron artículos.")
        sys.exit(1)

    print("\n=== ESTADÍSTICAS ===")
    print(f"Anuncios encontrados:  {stats['count']}")
    print(f"Precio medio:          {stats['avg_price']} EUR")
    print(f"Precio mediana:        {stats['median_price']} EUR")
    print(f"Precio mínimo:         {stats['min_price']} EUR")
    print(f"Precio máximo:         {stats['max_price']} EUR")
    print(f"Desviación estándar:   {stats['stdev']} EUR")

    print_cheapest(items, n=args.top)

    if args.export_csv:
        export_csv(items, args.export_csv)


if __name__ == "__main__":
    main()
