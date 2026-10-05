"""
Monitor de precios iPhone 17/18 Pro y Pro Max y Samsung Galaxy S26 Ultra en tiendas chilenas.
Envía un mensaje a Telegram cuando aparece un producto nuevo o cambia un precio,
y te avisa si una tienda deja de devolver resultados (señal de que el sitio cambió o te bloquearon).

Uso:
    export TELEGRAM_TOKEN="123456:ABC..."
    export TELEGRAM_CHAT_ID="123456789"
    python scraper.py            # corrida normal (notifica y guarda estado)
    python scraper.py --check    # diagnóstico: no notifica ni guarda, imprime resultados
                                 # por tienda y guarda screenshot/HTML en debug/ si una URL no devuelve nada
    EXTRA_STORES=1 python scraper.py   # incluye las tiendas opcionales (más ruido)
"""
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
STATE_FILE = BASE / "prices.json"
DEBUG_DIR = BASE / "debug"

# Placeholders: {q} = "iphone%2017%20pro" · {qd} = "iphone-17-pro"
# URLs validadas con --check (oct 2026). Descartadas:
#   Lider     → bloqueo anti-bot ("Robot or human?")
#   PCFactory → no vende iPhone (búsqueda "iphone" sin resultados)
#   SPDigital → no vende iPhone (la búsqueda devuelve solo accesorios)
STORES = {
    "Falabella": ["https://www.falabella.com/falabella-cl/search?Ntt={q}"],
    # "iphone pro" ya trae 17 y 18 (cada URL va en sesión nueva: Ripley bloquea la 2ª búsqueda de una sesión)
    "Ripley": [
        "https://simple.ripley.cl/search/iphone%20pro",
        "https://simple.ripley.cl/search/samsung%20s26%20ultra",
    ],
    "Paris": [
        "https://www.paris.cl/tecnologia/celulares/iphone/",
        "https://www.paris.cl/search?q={q}",
    ],
    # solo Apple. Sin buscador funcional; solo lista el modelo vigente (si vuelve a vender 17 Pro, agregar su categoría)
    "MacOnline": ["https://www.maconline.com/t/iphone-18-pro-iphone-18-pro-max"],
    # Solo Tiendas Oficiales (vendedores verificados). El buscador y las fichas de producto de ML piden
    # iniciar sesión; las páginas de tienda oficial no. Ojo: muestran sus destacados, no todo el catálogo.
    "MercadoLibre": [
        "https://www.mercadolibre.cl/tienda/apple",
        "https://www.mercadolibre.cl/tienda/samsung",
    ],
}
# Tiendas cuyas páginas ya son solo de vendedores verificados: no se revisa la línea "Por X"
OFFICIAL_ONLY = {"MercadoLibre"}
# Vendedores externos que sí se aceptan por tienda, aunque no sean la tienda misma (en minúsculas)
ALLOWED_SELLERS = {
    "Falabella": {"samsung"},  # tienda oficial de Samsung dentro de Falabella ("Por Samsung")
}

# Tiendas "parecidas" opcionales (más resultados de terceros / marketplace).
OPTIONAL_STORES = {
    "Hites": ["https://www.hites.com/search?q={q}"],
    "AbcDin": ["https://www.abcdin.cl/search?q={q}"],
    "LaPolar": ["https://www.lapolar.cl/search?q={q}"],
}
if os.environ.get("EXTRA_STORES") == "1":
    STORES.update(OPTIONAL_STORES)

SEARCHES = ["iphone 18 pro", "iphone 17 pro", "samsung s26 ultra"]  # "pro" trae también los "Max"

# Para agregar un modelo: su regex aquí, una búsqueda en SEARCHES y una palabra clave en TITLE_RE.
MODELS = [
    ("Galaxy S26 Ultra", re.compile(r"s\s*26\s*ultra", re.I)),
    ("iPhone 18 Pro Max", re.compile(r"iphone\s*18\s*pro\s*max", re.I)),
    ("iPhone 18 Pro", re.compile(r"iphone\s*18\s*pro(?!\s*max)", re.I)),
    ("iPhone 17 Pro Max", re.compile(r"iphone\s*17\s*pro\s*max", re.I)),
    ("iPhone 17 Pro", re.compile(r"iphone\s*17\s*pro(?!\s*max)", re.I)),
]
# Línea del nombre del producto dentro de la tarjeta (no la de la marca, p. ej. "SAMSUNG" en Falabella)
TITLE_RE = r"iphone|galaxy|s\s?26"
EXCLUDE = re.compile(
    r"funda|case|carcasa|protector|mica|vidrio|l[aá]mina|cable|cargador|"
    r"correa|magsafe|airtag|soporte|reacondicionado|usado|seminuevo|compatible|\bkit\b",
    re.I,
)
# Etiquetas que se muestran en el mensaje. E-SIM: versión sin bandeja SIM física (suele ser importada).
ESIM_RE = re.compile(r"\be[\s-]?sim\b", re.I)
IMPORT_RE = re.compile(r"importad|versi[oó]n usa|garant[ií]a (?:de )?\d+ meses", re.I)
# Señales de vendedor de marketplace (no la tienda): esos productos se descartan
MARKETPLACE_RE = re.compile(r"vendido por|vendido y enviado|vendedor|marketplace", re.I)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MIN_PRICE = 300_000  # descarta cuotas y accesorios (CLP)
ZERO_RUNS_ALERT = 12  # avisar si una tienda no devuelve nada N corridas seguidas (~1 h corriendo cada 5 min)
IMG_SIZE = 320  # lado máximo (px) de la foto enviada a Telegram; más chico = foto más pequeña

# Precios que solo valen pagando con la tarjeta de la propia tienda: no se consideran.
# Falabella no aparece a propósito: su precio con CMR sí se cuenta.
# Selector CSS del bloque de precio con tarjeta, dentro de la tarjeta del producto.
CARD_ONLY = {
    "Paris": '[data-testid="paris-pod-price"]:has([data-testid*="cencosud-card"])',
}
# Ripley (sus datos internos): priceNumber = internet, masterPriceNumber = normal,
# ripleyPriceNumber = solo con Tarjeta Ripley (se ignora).

JS_EXTRACT = """
([cardOnly, titleRe]) => {
  const kw = new RegExp(titleRe, 'i');
  const out = [];
  const seen = new Set();
  const abs = s => (s && !s.startsWith('data:')) ? new URL(s, location.href).href : '';
  // la imagen más grande de la tarjeta = la del producto (las chicas son sellos/íconos);
  // las lazy traen la URL en data-src/srcset
  const imgOf = el => {
    let best = '', area = -1;
    for (const im of el.querySelectorAll('img')) {
      const r = im.getBoundingClientRect(), a = r.width * r.height;
      if (a <= area) continue;
      const c = [im.currentSrc, im.getAttribute('src'), im.dataset.src,
                 (im.getAttribute('srcset') || '').trim().split(/\\s+/)[0]];
      for (const s of c) {
        try { const u = abs(s); if (u) { best = u; area = a; break; } } catch (e) {}
      }
    }
    return best;
  };
  document.querySelectorAll('a[href]').forEach(a => {
    let node = a, text = '';
    // sube solo hasta la primera caja con precio (la tarjeta del propio producto);
    // si sigue subiendo agarra el título de otro producto de la grilla
    for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
      text = node.innerText || '';
      if (text.includes('$')) break;
    }
    if (!text.includes('$') || !kw.test(text)) return;
    if (seen.has(a.href)) return;
    seen.add(a.href);
    const excl = cardOnly ? [...node.querySelectorAll(cardOnly)].map(e => e.innerText).join(' ') : '';
    // si la foto está en una caja hermana (MercadoLibre), sube hasta 2 niveles más,
    // solo mientras ese nivel tenga links a un único producto (no la grilla entera)
    let image = imgOf(node);
    const key = h => h.split(/[?#]/)[0];
    for (let up = node.parentElement, i = 0; !image && up && i < 2; up = up.parentElement, i++) {
      const hrefs = new Set([...up.querySelectorAll('a[href]')].map(x => key(x.href)));
      if (hrefs.size > 1) break;
      image = imgOf(up);
    }
    out.push({href: a.href, text: text.slice(0, 600), image, exclude: excl});
  });
  // Datos estructurados (JSON-LD): Ripley trae aquí los productos aunque no pinte las tarjetas.
  const fmt = p => '$' + Math.round(Number(p)).toLocaleString('es-CL');
  // Ripley: el JSON-LD mezcla el precio con Tarjeta Ripley; sus datos internos (__NEXT_DATA__)
  // traen los precios separados (y el vendedor) por SKU, que es el final de la URL del producto
  const ripley = {};
  const scan = o => {
    if (Array.isArray(o)) return o.forEach(scan);
    if (!o || typeof o !== 'object') return;
    if ('ripleyPriceNumber' in o && o.sku)
      ripley[o.sku] = {prices: [o.priceNumber, o.masterPriceNumber], seller: o.seller || ''};
    Object.values(o).forEach(scan);
  };
  try { scan(JSON.parse(document.getElementById('__NEXT_DATA__').textContent)); } catch (e) {}
  const ld = [], ldSeen = new Set();
  const walk = o => {
    if (Array.isArray(o)) return o.forEach(walk);
    if (!o || typeof o !== 'object') return;
    if (o['@type'] === 'Product' && o.name && o.url && !ldSeen.has(o.url)) {
      let prices = [], seller = '';
      const grab = x => {
        if (Array.isArray(x)) return x.forEach(grab);
        if (!x || typeof x !== 'object') return;
        ['price', 'lowPrice'].forEach(k => { if (Number(x[k]) > 0) prices.push(fmt(x[k])); });
        grab(x.priceSpecification);
      };
      const own = ripley[o.url.split(/[/?#]/).filter(Boolean).pop()];
      if (own) {
        prices = own.prices.filter(p => Number(p) > 0).map(fmt);
        seller = own.seller;
      } else grab(o.offers);
      ldSeen.add(o.url);
      let img = [].concat(o.image || [])[0] || '';
      if (typeof img === 'object') img = img.url || img.contentUrl || '';
      ld.push({href: o.url, name: o.name, ld: true, image: abs(img),
               text: o.name + '\\n' + (seller ? 'Por ' + seller + '\\n' : '') + prices.join(' ')});
    }
    Object.values(o).forEach(walk);
  };
  document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
    try { walk(JSON.parse(s.textContent)); } catch (e) {}
  });
  if (!out.length) return ld;
  // Con tarjetas visibles el JSON-LD solo se usa para completar imágenes que aún no cargaban
  // (Falabella carga las de abajo al hacer scroll): mismo link o, si no, el nombre más largo presente
  const path = u => { try { return new URL(u, location.href).pathname; } catch (e) { return u; } };
  for (const it of out) {
    if (it.image) continue;
    const m = ld.find(l => path(l.href) === path(it.href))
      || ld.filter(l => it.text.includes(l.name)).sort((x, y) => y.name.length - x.name.length)[0];
    if (m) it.image = m.image;
  }
  return out;
}
"""

PRICE_RE = re.compile(r"\$\s*([\d]{1,3}(?:\.\d{3})+)")
RANGE_RE = re.compile(r"(\$\s*[\d.]+)\s*-\s*\$\s*[\d.]+")
BY_SELLER_RE = re.compile(r"^Por\s+(\S.{0,40})$", re.M)
MORE_BTN = re.compile(r"ver m[aá]s|cargar m[aá]s|mostrar m[aá]s", re.I)


def parse_item(raw, store=""):
    text = raw["text"]
    title = next((l.strip() for l in text.splitlines() if re.search(TITLE_RE, l, re.I)), "")
    if not title or EXCLUDE.search(title):
        return None
    model = next((name for name, rx in MODELS if rx.search(title)), None)
    if not model:
        return None
    # el link debe ser de ese mismo modelo (descarta Xiaomi "17T Pro", menús, footer, etc.).
    # Los productos JSON-LD traen su propio nombre (y Ripley usa URLs sin nombre), no aplica.
    slug = re.sub(r"[-_/+]|%20", " ", urlparse(raw["href"]).path)
    if not raw.get("ld") and not dict(MODELS)[model].search(slug):
        return None
    # "$1.699.990 - $2.479.990" es un rango de capacidades (Falabella), no precio normal vs oferta
    prices = [int(p.replace(".", "")) for p in PRICE_RE.findall(RANGE_RE.sub(r"\1", text))]
    for p in PRICE_RE.findall(raw.get("exclude", "")):  # precios solo con tarjeta de la tienda
        if (n := int(p.replace(".", ""))) in prices:
            prices.remove(n)
    prices = [p for p in prices if p >= MIN_PRICE]
    if not prices:
        return None
    # Marketplace fuera: Falabella/Ripley indican el vendedor en una línea "Por X"
    # (Ripley pone "MARKETPLACE" sin nombre); si no es la propia tienda, se descarta
    if store not in OFFICIAL_ONLY:
        seller = BY_SELLER_RE.search(text)
        seller = seller.group(1).strip().lower() if seller else ""
        if seller and seller not in (store.lower(), *ALLOWED_SELLERS.get(store, ())):
            return None
        if not seller and MARKETPLACE_RE.search(text):
            return None
    price, list_price = min(prices), max(prices)  # el menor es el que pagas; el mayor, el normal
    tags = []
    if ESIM_RE.search(text):
        tags.append("E-SIM")
    if IMPORT_RE.search(text):
        tags.append("Importado")
    return {
        "model": model,
        "title": title,
        "price": price,
        "list_price": list_price,
        "discount": round((list_price - price) * 100 / list_price),
        "url": raw["href"].split("#")[0],  # ML agrega #polycard_... que cambia según el carrusel
        "image": raw.get("image", ""),
        "tags": tags,
    }


def load_all(page):
    """Hace scroll (lazy-load) y pulsa 'Ver más' para cargar más productos."""
    for _ in range(6):
        page.mouse.wheel(0, 5000)
        page.wait_for_timeout(700)
    for _ in range(5):
        btn = page.get_by_role("button", name=MORE_BTN)
        try:
            if btn.count() == 0 or not btn.first.is_visible():
                break
            btn.first.click(timeout=3000)
            page.wait_for_timeout(1500)
        except Exception:
            break


def urls_for(store_urls):
    urls = []
    for tpl in store_urls:
        for q in SEARCHES:
            u = tpl.format(q=quote(q), qd=q.replace(" ", "-"))
            if u not in urls:  # las páginas de categoría no dependen de q
                urls.append(u)
    return urls


def scrape(debug=False):
    items = {}
    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        # BROWSER_CHANNEL=chrome usa el Chrome instalado (GitHub Actions); sin él, el Chromium de Playwright
        channel = os.environ.get("BROWSER_CHANNEL") or None
        browser = pw.chromium.launch(headless=True, channel=channel)
        # Intercalado: primero la 1ª URL de cada tienda, luego la 2ª, etc. (menos ráfagas por tienda)
        tasks = sorted(
            ((n, store, url) for store, store_urls in STORES.items()
             for n, url in enumerate(urls_for(store_urls))),
            key=lambda t: t[0],
        )
        stats = dict.fromkeys(STORES, 0)
        for n, store, url in tasks:
            found = 0
            # sesión nueva (cookies limpias) por URL: Ripley bloquea la 2ª búsqueda de una misma sesión
            ctx = browser.new_context(locale="es-CL", user_agent=UA)
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(4000)
                load_all(page)
                for raw in page.evaluate(JS_EXTRACT, [CARD_ONLY.get(store, ""), TITLE_RE]):
                    it = parse_item(raw, store)
                    if it:
                        it["store"] = store
                        key = f"{store}|{it['url']}"
                        found += 1
                        # el mismo producto puede salir en categoría y búsqueda con precios distintos
                        # (Paris): se queda el menor para no avisar subidas/bajadas falsas
                        prev = items.get(key)
                        if prev and prev["price"] <= it["price"]:
                            continue
                        items[key] = it
                        if debug:
                            print(
                                f"      {it['model']:<18} {clp(it['price']):>11} -{it['discount']:>2}%"
                                f"  {'img' if it['image'] else '---'}  {it['title'][:45]}"
                                + (f"  [{', '.join(it['tags'])}]" if it["tags"] else "")
                            )
            except Exception as e:  # una tienda caída no debe romper el resto
                print(f"[WARN] {store} {url}: {e}", file=sys.stderr)
            stats[store] += found
            if debug:
                print(f"  {store:<13} {found:>3}  {url}")
                if found == 0:
                    stem = DEBUG_DIR / f"{store}_{n}"
                    try:
                        page.screenshot(path=f"{stem}.png", full_page=True)
                        Path(f"{stem}.html").write_text(page.content(), encoding="utf-8")
                    except Exception:
                        pass
            ctx.close()
        browser.close()
    return items, stats


def clp(n):
    return "$" + f"{n:,}".replace(",", ".")


TREND = {
    "nuevo": "🆕 NUEVO",
    "bajó": "🔻 BAJÓ DE PRECIO",
    "subió": "🔺 SUBIÓ DE PRECIO",
}


def caption(it):
    lines = [TREND[it["trend"]], f"{it['model']} · {it['store']}", it["title"]]
    if it.get("tags"):
        lines.append("🏷️ " + " · ".join(it["tags"]))
    price = f"💰 {clp(it['price'])}"
    if it.get("discount"):
        price += f"  (-{it['discount']}% · normal {clp(it['list_price'])})"
    lines.append(price)
    if it["trend"] != "nuevo":
        pct = (it["price"] - it["prev_price"]) * 100 / it["prev_price"]
        pct = f"{pct:+.1f}".replace(".", ",")
        lines.append(f"Antes: {clp(it['prev_price'])}  ({pct}%)")
    lines.append(it["url"])
    return "\n".join(lines)


def diff(old, new):
    """Marca cada producto nuevo o con cambio de precio con su tendencia y lo devuelve."""
    changed = []
    for key, it in new.items():
        prev = old.get(key)
        if prev is None:
            it["trend"] = "nuevo"
        elif it["price"] != prev["price"]:
            it["trend"] = "bajó" if it["price"] < prev["price"] else "subió"
            it["prev_price"] = prev["price"]
        else:
            it["trend"] = prev.get("trend", "nuevo")  # sin cambio: conserva la última tendencia
            continue
        changed.append(it)
    return changed


def tg(method, **kw):
    """Llama a la API de Telegram; si pide esperar (429), espera y reintenta una vez."""
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/{method}"
    for _ in range(2):
        r = requests.post(url, timeout=30, **kw)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        return r
    return r


def fetch_image(url):
    """Descarga la imagen y la pasa a JPEG (las tiendas sirven WebP, que Telegram no siempre acepta)."""
    url = re.sub(r"width=\d+,height=\d+", f"width={IMG_SIZE},height={IMG_SIZE}", url)  # Falabella
    data = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    data.raise_for_status()
    try:
        from PIL import Image
    except ImportError:  # sin Pillow se manda tal cual
        return data.content
    img = Image.open(io.BytesIO(data.content))
    if img.mode != "RGB":  # PNG/WebP con transparencia → fondo blanco
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.convert("RGBA").getchannel("A"))
        img = bg
    img.thumbnail((IMG_SIZE, IMG_SIZE))  # foto chica en Telegram
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return out.getvalue()


def notify(alerts, products):
    chat = os.environ["TELEGRAM_CHAT_ID"]
    for text in alerts:
        tg("sendMessage", data={"chat_id": chat, "text": text}).raise_for_status()
    for it in products:
        cap = caption(it)
        sent = False
        if it.get("image"):
            # se descarga y se sube: varias tiendas no dejan que Telegram baje la imagen directo
            try:
                r = tg("sendPhoto", data={"chat_id": chat, "caption": cap[:1024]},
                       files={"photo": ("producto.jpg", fetch_image(it["image"]))})
                sent = r.ok
                if not sent:
                    print(f"[WARN] Telegram rechazó la foto: {r.text[:200]}", file=sys.stderr)
            except Exception as e:  # imagen caída o formato raro: no debe impedir el aviso
                print(f"[WARN] imagen {it['image']}: {e}", file=sys.stderr)
        if not sent:  # sin imagen o falló la foto: va solo el texto
            tg("sendMessage", data={"chat_id": chat, "text": cap,
                                    "disable_web_page_preview": True}).raise_for_status()
        time.sleep(1.1)  # límite de Telegram: ~1 mensaje/segundo por chat


def main():
    check = "--check" in sys.argv
    old = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    new, stats = scrape(debug=check)

    if check:
        print("\nResumen por tienda:")
        for s, n in stats.items():
            print(f"  {s:<13} {n:>3} {'OK' if n else '← SIN RESULTADOS (mira debug/)'}")
        return

    # Salud: cuántas corridas seguidas lleva cada tienda sin resultados
    health = old.get("_health", {})
    alerts = []
    for store, n in stats.items():
        health[store] = 0 if n else health.get(store, 0) + 1
        if health[store] == ZERO_RUNS_ALERT:
            alerts.append(
                f"⚠️ {store}: {ZERO_RUNS_ALERT} corridas seguidas sin resultados "
                "(¿cambió el sitio o te bloquearon?)"
            )

    changes = diff(old, new)
    if changes or alerts:
        notify(alerts, changes)
    print(f"{len(new)} productos, {len(changes)} cambios, {len(alerts)} alertas")

    old.update(new)  # conserva lo último visto aunque una tienda falle esta vez
    old["_health"] = health
    STATE_FILE.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
