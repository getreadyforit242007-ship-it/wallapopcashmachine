"""
Bot de alertas de Wallapop -- version "single-shot" pensada para correr
como cron job (GitHub Actions), no como proceso siempre-vivo.

Cada ejecucion:
  1. Carga el estado guardado (mediana + ids ya vistos) desde alert_state.json
  2. Si la mediana tiene mas de MEDIAN_REFRESH_HOURS, la recalcula
  3. Revisa los anuncios mas nuevos y avisa por Telegram si hay chollo
  4. Guarda el estado actualizado (el workflow de GitHub Actions se encarga
     de hacer commit de este fichero para que persista entre ejecuciones)

Variables de entorno requeridas:
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests

import wallapop_market as wm

# ============ CONFIGURACION ============

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SEARCHES = [
    {
        "name": "Nintendo Switch OLED",
        "keywords": "nintendo switch oled",
        "title_contains": ["switch"],
        "title_excludes": ["funda", "soporte", "mando", "case", "protector", "cristal", "cable",
                            "adaptador", "base", "cargador", "auriculares", "guitarra", "volante",
                            "silla", "monitor", "pantalla"],
        "min_price": 130,
    },
    {
        "name": "PS5 Slim Disco",
        "keywords": "ps5 slim disco",
        "title_contains": ["ps5", "playstation 5"],
        "title_excludes": ["ps4", "ps3", "xbox", "soporte", "funda", "mando", "digital",
                            "unidad lectora", "ssd", "disco duro", "auriculares", "lente", "optic",
                            "volante", "guitarra", "silla", "monitor", "pantalla", "pc gaming", "rtx"],
        "min_price": 150,
    },
    {
        "name": "PS5 Slim Digital",
        "keywords": "ps5 slim digital",
        "title_contains": ["ps5", "playstation 5"],
        "title_excludes": ["ps4", "ps3", "xbox", "soporte", "funda", "mando", "disco",
                            "lector", "ssd", "disco duro", "auriculares", "volante", "guitarra",
                            "silla", "monitor", "pantalla", "aniversario", "30 aniversario"],
        "min_price": 150,
    },
    {
        "name": "Xbox Series X",
        "keywords": "xbox series x",
        "title_contains": ["series x"],
        "title_excludes": ["series s", "xbox 360", "xbox one",
                            "volante", "mando suelto", "auriculares", "cascos", "guitarra",
                            "coleccionista", "collector", "funda", "soporte", "cargador", "cable",
                            "adaptador", "base de carga", "teclado", "raton", "ratón", "silla",
                            "monitor", "pantalla", "tcl", "gigabyte", "rock band", "starfield",
                            "metaphor", "indiana jones", "elden ring", "assassin", "crimson desert",
                            "outrun", "scuf", "astro", "beoplay", "bang & olufsen", "thrustmaster",
                            "logitech", "avatar"],
        "min_price": 150,
    },
    {
        "name": "Steam Deck",
        "keywords": "steam deck",
        "title_contains": ["steam deck"],
        "title_excludes": ["funda", "soporte", "case", "dock", "cargador", "cable",
                            "edicion limitada", "edición limitada"],
        "min_price": 150,
    },
]

BUY_THRESHOLD_PCT = 0.20
MEDIAN_REFRESH_HOURS = 24
MEDIAN_SWEEP_PAGES = 10
MAX_SEEN_IDS = 3000

STATE_FILE = "alert_state.json"


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram] faltan credenciales, no se envia")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": False,
        }, timeout=15)
        if r.status_code != 200:
            print(f"[telegram] error {r.status_code}: {r.text}")
    except requests.RequestException as e:
        print(f"[telegram] excepcion enviando mensaje: {e}")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def is_madrid(item):
    loc = item.get("location", {})
    city = loc.get("city", "").lower()
    region = loc.get("region", "").lower()
    return "madrid" in city or "madrid" in region


def compute_median(search_cfg, session):
    print(f"[{search_cfg['name']}] recalculando mediana...")
    search_id, lat, lon = wm.get_search_id(search_cfg["keywords"], wm.DEFAULT_LAT, wm.DEFAULT_LON, session)

    all_items = []
    next_page = None
    for _ in range(MEDIAN_SWEEP_PAGES):
        data = wm.fetch_page(session, search_cfg["keywords"], search_id, lat, lon,
                              min_price=search_cfg["min_price"], next_page=next_page)
        items = data.get("data", {}).get("section", {}).get("items", [])
        if not items:
            break
        all_items.extend(items)
        next_page = data.get("meta", {}).get("next_page")
        if not next_page:
            break

    filtered = wm.filter_by_title(all_items, search_cfg["title_contains"], search_cfg["title_excludes"])
    madrid_items = [it for it in filtered if is_madrid(it)]

    stats = wm.analyze(madrid_items)
    if not stats:
        print(f"[{search_cfg['name']}] sin datos suficientes para mediana")
        return None

    print(f"[{search_cfg['name']}] mediana Madrid: {stats['median_price']} EUR ({stats['count']} anuncios)")
    return stats["median_price"]


def poll_new_listings(search_cfg, median, seen_ids, session):
    search_id, lat, lon = wm.get_search_id(search_cfg["keywords"], wm.DEFAULT_LAT, wm.DEFAULT_LON, session)

    params = {
        "keywords": search_cfg["keywords"],
        "source": "search_box",
        "order_by": "newest",
        "search_id": search_id,
        "latitude": lat,
        "longitude": lon,
        "section_type": "organic_search_results",
        "search_country": "ES",
        "min_sale_price": search_cfg["min_price"],
    }
    resp = session.get(f"{wm.API_BASE}/search/section", params=params, headers=wm.HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("data", {}).get("section", {}).get("items", [])

    filtered = wm.filter_by_title(items, search_cfg["title_contains"], search_cfg["title_excludes"])
    madrid_items = [it for it in filtered if is_madrid(it)]

    new_alerts = 0
    for item in madrid_items:
        item_id = item["id"]
        if item_id in seen_ids:
            continue
        seen_ids.append(item_id)

        price = item["price"]["amount"]
        if median and price <= median * (1 - BUY_THRESHOLD_PCT):
            pct_below = round((1 - price / median) * 100, 1)
            city = item.get("location", {}).get("city", "?")
            url = f"https://es.wallapop.com/item/{item['web_slug']}"
            text = (
                f"CHOLLO - {search_cfg['name']}\n\n"
                f"{item['title']}\n"
                f"Precio: {price:.0f} EUR ({pct_below}% por debajo de la mediana de {median:.0f} EUR)\n"
                f"Ciudad: {city}\n\n"
                f"Ver anuncio: {url}"
            )
            print(f"  -> ALERTA: {item['title'][:50]} a {price} EUR")
            send_telegram(text)
            new_alerts += 1

    if len(seen_ids) > MAX_SEEN_IDS:
        del seen_ids[: len(seen_ids) - MAX_SEEN_IDS]

    return new_alerts


def main():
    state = load_state()
    session = requests.Session()
    now = datetime.now(timezone.utc)

    for search_cfg in SEARCHES:
        name = search_cfg["name"]
        if name not in state:
            state[name] = {"median": None, "median_updated_at": None, "seen_ids": []}

        entry = state[name]
        needs_refresh = (
            entry["median"] is None
            or entry["median_updated_at"] is None
            or (now - datetime.fromisoformat(entry["median_updated_at"])).total_seconds() > MEDIAN_REFRESH_HOURS * 3600
        )
        if needs_refresh:
            median = compute_median(search_cfg, session)
            if median:
                entry["median"] = median
                entry["median_updated_at"] = now.isoformat()

        try:
            poll_new_listings(search_cfg, entry["median"], entry["seen_ids"], session)
        except Exception as e:
            print(f"[{name}] error en poll: {e}", file=sys.stderr)

    save_state(state)
    print(f"[{now.strftime('%H:%M:%S')} UTC] ciclo completado")


if __name__ == "__main__":
    main()
