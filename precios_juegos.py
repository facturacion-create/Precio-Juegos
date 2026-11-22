#!/usr/bin/env python3
"""
Comparador de precios de juegos: Steam, Xbox y Nintendo eShop.

Solo usa la biblioteca estándar de Python. Levanta un servidor web con una
interfaz HTML pensada para el celular.

Uso local:
    python precios_juegos.py            # abre http://localhost:8000
    PORT=5000 python precios_juegos.py  # otro puerto

En la nube (Render, etc.) el puerto llega en la variable de entorno PORT.
"""

import json
import os
import re
import struct
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USER_AGENT = "Mozilla/5.0 (compatible; precios-juegos/2.0)"
TIMEOUT = 12  # segundos por consulta
CACHE_TTL = 15 * 60  # 15 minutos

# País -> (idioma Xbox, moneda esperada). Nintendo usa el mismo código de país.
PAISES = {
    "AR": {"nombre": "Argentina", "locale": "es-AR"},
    "CL": {"nombre": "Chile", "locale": "es-CL"},
    "CO": {"nombre": "Colombia", "locale": "es-CO"},
    "MX": {"nombre": "México", "locale": "es-MX"},
    "BR": {"nombre": "Brasil", "locale": "pt-BR"},
    "US": {"nombre": "Estados Unidos", "locale": "en-US"},
    "ES": {"nombre": "España", "locale": "es-ES"},
}

# Países donde además de la eShop local se muestra la de EE.UU. como
# alternativa (mucha gente en Argentina usa una cuenta de EE.UU.).
NINTENDO_REFERENCIA_US = {"AR"}

# Claves públicas que usa la propia web de Nintendo of America para buscar.
ALGOLIA_APP = "U3B6GR4UA3"
ALGOLIA_KEY = "a29c6927638bfd8cee23993e51e721c9"

PALABRAS_EXTRA = (
    "soundtrack", "dlc", "upgrade", "pack", "demo", "theme", "season pass",
    "expansion", "bundle", "lote", "tema", "actualizacion", "add-on", "addon",
)


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

_cache = {}
_cache_lock = threading.Lock()


def cacheado(clave, funcion, ttl=CACHE_TTL):
    ahora = time.time()
    with _cache_lock:
        if clave in _cache and ahora - _cache[clave][0] < ttl:
            return _cache[clave][1]
    valor = funcion()
    with _cache_lock:
        _cache[clave] = (ahora, valor)
        if len(_cache) > 500:  # limpieza simple
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:250]:
                del _cache[k]
    return valor


def http_json(url, data=None, headers=None):
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def normalizar(texto):
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"[™®©]", "", texto.lower())
    texto = re.sub(r"[^a-z0-9]+", " ", texto)
    return texto.strip()


EDICIONES = (
    "edicion", "edition", "deluxe", "definitive", "definitiva", "complete",
    "completa", "goty", "game of the year", "remastered", "ultimate", "standard",
    "estandar", "gold", "premium", "for windows", "para windows", "windows",
    "nintendo switch", "switch 2", "solo pc",
)


def sin_edicion(titulo):
    """'Hollow Knight: Edición Corazón Vacío' -> 'Hollow Knight'."""
    partes = re.split(r"\s*(?:[:\u2013\u2014(]|\s-\s)\s*", titulo, maxsplit=1)
    if len(partes) == 2 and any(e in normalizar(partes[1]) for e in EDICIONES):
        return partes[0]
    return titulo


def puntaje(consulta, titulo):
    """Qué tan parecido es un título a lo que se buscó (más alto = mejor)."""
    q, t = normalizar(consulta), normalizar(sin_edicion(titulo))
    if not q or not t:
        return 0.0
    s = SequenceMatcher(None, q, t).ratio()
    if t == q:
        s += 1.0
    elif t.startswith(q + " ") or t.startswith(q):
        s += 0.3
    elif q in t:
        s += 0.15
    if any(p in t and p not in q for p in PALABRAS_EXTRA):
        s -= 0.5
    return round(s, 3)


def ordenar(consulta, items):
    for it in items:
        it["puntaje"] = puntaje(consulta, it["titulo"])
    items.sort(key=lambda it: it["puntaje"], reverse=True)
    return items


PUNTAJE_MINIMO = 0.75  # por debajo, el resultado probablemente es otro juego


def resultado_tienda(tienda, items=None, error=None, aviso=None, buscar_url=None, acepta_global=False):
    items = items or []
    return {
        "tienda": tienda,
        "ok": error is None,
        "error": error,
        "aviso": aviso,
        "resultados": items,
        "coincide": bool(items) and items[0].get("puntaje", 1) >= PUNTAJE_MINIMO,
        "buscar_url": buscar_url,
        # Tiendas que aceptan tarjeta Global (emitida afuera): sin IVA ni IIBB.
        "acepta_global": acepta_global,
    }


def describir_error(e):
    if isinstance(e, urllib.error.HTTPError):
        return f"La tienda respondió con error {e.code}"
    if isinstance(e, urllib.error.URLError):
        return "No se pudo conectar con la tienda"
    if isinstance(e, TimeoutError):
        return "La tienda tardó demasiado en responder"
    return f"Error inesperado: {type(e).__name__}"


# --------------------------------------------------------------------------
# Steam
# --------------------------------------------------------------------------

def buscar_steam(consulta, pais):
    url = "https://store.steampowered.com/api/storesearch/?" + urllib.parse.urlencode(
        {"term": consulta, "cc": pais, "l": "spanish"}
    )
    datos = http_json(url)
    items = []
    for it in datos.get("items", []):
        if it.get("type") != "app":
            continue
        precio = it.get("price")
        if precio:
            final = precio.get("final", 0) / 100
            inicial = precio.get("initial", 0) / 100
            moneda = precio.get("currency", "")
        else:
            final = inicial = 0.0
            moneda = ""
        items.append({
            "titulo": it.get("name", ""),
            "precio": final,
            "precio_original": inicial if inicial > final else None,
            "moneda": moneda,
            "gratis": not precio,
            "url": f"https://store.steampowered.com/app/{it.get('id')}/",
            "imagen": it.get("tiny_image"),
        })
    return resultado_tienda(
        "Steam", ordenar(consulta, items)[:5],
        buscar_url="https://store.steampowered.com/search/?term=" + urllib.parse.quote(consulta),
    )


# --------------------------------------------------------------------------
# Xbox (Microsoft Store)
# --------------------------------------------------------------------------

def _xbox_ids_autosuggest(consulta, pais, locale):
    url = "https://displaycatalog.mp.microsoft.com/v7.0/productFamilies/autosuggest?" + \
        urllib.parse.urlencode({
            "market": pais, "languages": locale, "query": consulta,
            "productFamilyNames": "Games", "topProducts": 10,
        })
    ids = []
    for grupo in http_json(url).get("Results", []):
        for p in grupo.get("Products", []):
            if p.get("Type") == "Game":
                ids.append(p["ProductId"])
    return ids


def _xbox_ids_storeedge(consulta, pais, locale, familia="Windows.Xbox"):
    url = "https://storeedgefd.dsx.mp.microsoft.com/v9.0/search?" + urllib.parse.urlencode({
        "market": pais, "locale": locale, "query": consulta,
        "deviceFamily": familia,
    })
    datos = http_json(url)
    ids = []
    for r in (datos.get("Payload") or {}).get("SearchResults", [])[:10]:
        if r.get("ProductFamilyName", "Games") == "Games":
            ids.append(r["ProductId"])
    return ids


def _xbox_precio(producto):
    """Precio de compra real (ignora las entradas de Game Pass, que valen 0)."""
    for dsa in producto.get("DisplaySkuAvailabilities", []):
        for a in dsa.get("Availabilities", []):
            if "Purchase" not in a.get("Actions", []):
                continue
            if a.get("Remediations"):  # "Upsell": incluido con Game Pass
                continue
            p = (a.get("OrderManagementData") or {}).get("Price")
            if p:
                return p
        break  # solo el primer SKU (el juego base)
    return None


def _xbox_plataformas(producto):
    plat = set()
    for dsa in producto.get("DisplaySkuAvailabilities", [])[:1]:
        for a in dsa.get("Availabilities", []):
            for c in ((a.get("Conditions") or {}).get("ClientConditions") or {}).get("AllowedPlatforms", []):
                plat.add(c.get("PlatformName"))
    return plat


def buscar_xbox(consulta, pais):
    locale = PAISES.get(pais, {}).get("locale", "es-AR")
    buscar_url = f"https://www.xbox.com/{locale}/search/results/games?q=" + urllib.parse.quote(consulta)
    busquedas = [
        lambda: _xbox_ids_storeedge(consulta, pais, locale, "Windows.Xbox"),
        lambda: _xbox_ids_storeedge(consulta, pais, locale, "Windows.Desktop"),
        lambda: _xbox_ids_autosuggest(consulta, pais, locale),
    ]
    ids, errores = [], []
    # Las búsquedas de Microsoft devuelven cosas distintas: se combinan.
    for futuro in [_pool_xbox.submit(b) for b in busquedas]:
        try:
            for i in futuro.result():
                if i not in ids:
                    ids.append(i)
        except Exception as e:  # una puede fallar y las otras no
            errores.append(describir_error(e))
    if not ids:
        if errores:
            return resultado_tienda("Xbox", error=errores[0], buscar_url=buscar_url)
        return resultado_tienda("Xbox", buscar_url=buscar_url)

    url = "https://displaycatalog.mp.microsoft.com/v7.0/products?" + urllib.parse.urlencode({
        "bigIds": ",".join(ids[:30]), "market": pais, "languages": locale,
    })
    items = []
    for prod in http_json(url).get("Products", []):
        if prod.get("ProductKind") != "Game":
            continue
        precio = _xbox_precio(prod)
        if not precio:
            continue
        titulo = (prod.get("LocalizedProperties") or [{}])[0].get("ProductTitle", "")
        lista = float(precio.get("ListPrice") or 0)
        msrp = float(precio.get("MSRP") or 0)
        plat = _xbox_plataformas(prod)
        solo_pc = "Windows.Xbox" not in plat and "Windows.Desktop" in plat
        items.append({
            "titulo": titulo + (" (solo PC)" if solo_pc else ""),
            "precio": lista,
            "precio_original": msrp if msrp > lista else None,
            "moneda": precio.get("CurrencyCode", ""),
            "gratis": lista == 0,
            "url": f"https://www.xbox.com/{locale}/games/store/x/{prod['ProductId']}",
        })
    items = ordenar(consulta, items)
    # A igual parecido, preferir los que se juegan en consola.
    items.sort(key=lambda it: (it["puntaje"] - (0.05 if "(solo PC)" in it["titulo"] else 0)), reverse=True)
    return resultado_tienda("Xbox", items[:5], buscar_url=buscar_url)


# --------------------------------------------------------------------------
# Nintendo eShop
# --------------------------------------------------------------------------

def _nintendo_buscar(consulta):
    url = f"https://{ALGOLIA_APP.lower()}-dsn.algolia.net/1/indexes/*/queries"
    datos = http_json(
        url,
        data={"requests": [{
            "indexName": "store_game_en_us",
            "query": consulta,
            "params": "hitsPerPage=10",
        }]},
        headers={"x-algolia-application-id": ALGOLIA_APP, "x-algolia-api-key": ALGOLIA_KEY},
    )
    juegos = []
    for h in datos["results"][0].get("hits", []):
        nsuid = h.get("nsuid")
        if not nsuid or h.get("topLevelCategoryCode") not in (None, "GAMES"):
            continue
        if "upgrade pack" in (h.get("title") or "").lower():
            continue  # mejoras gratis/pagas para Switch 2, no el juego
        plataforma = "Switch 2" if h.get("platformCode") == "NINTENDO_SWITCH_2" else "Switch"
        titulo = re.sub(r"\s+", " ", h.get("title", "")).strip()
        if plataforma == "Switch 2" and "switch 2" not in titulo.lower():
            titulo += " (Switch 2)"
        juegos.append({
            "nsuid": str(nsuid),
            "titulo": titulo,
            "url": "https://www.nintendo.com" + (h.get("url") or ""),
        })
    return juegos


def _nintendo_precios(nsuids, pais):
    url = "https://api.ec.nintendo.com/v1/price?" + urllib.parse.urlencode({
        "country": pais, "lang": "es" if pais != "US" else "en", "ids": ",".join(nsuids),
    })
    precios = {}
    for p in http_json(url).get("prices", []):
        if p.get("sales_status") != "onsale" or "regular_price" not in p:
            continue
        regular = float(p["regular_price"]["raw_value"])
        oferta = p.get("discount_price")
        precios[str(p["title_id"])] = {
            "precio": float(oferta["raw_value"]) if oferta else regular,
            "precio_original": regular if oferta else None,
            "moneda": p["regular_price"].get("currency", ""),
        }
    return precios


def buscar_nintendo(consulta, pais):
    juegos = ordenar(consulta, _nintendo_buscar(consulta))[:6]
    if not juegos:
        return [resultado_tienda("Nintendo eShop")]
    nsuids = [j["nsuid"] for j in juegos]

    salida = []
    paises = [pais]
    if pais in NINTENDO_REFERENCIA_US:
        paises = [pais, "US"]  # primero la región local, después EE.UU.
    for p in paises:
        try:
            precios = _nintendo_precios(nsuids, p)
        except Exception as e:
            salida.append(resultado_tienda(f"Nintendo eShop ({p})", error=describir_error(e)))
            continue
        items = []
        for j in juegos:
            if j["nsuid"] in precios:
                pr = precios[j["nsuid"]]
                items.append({
                    "titulo": j["titulo"], "url": j["url"], "puntaje": j["puntaje"],
                    "gratis": pr["precio"] == 0, **pr,
                })
        nombre = "Nintendo eShop"
        aviso = None
        if len(paises) > 1:
            nombre = "Nintendo eShop " + ("EE.UU." if p == "US" else PAISES[p]["nombre"])
            if p == "US":
                aviso = "Necesitás una cuenta de Nintendo de EE.UU. Acepta tarjeta Global (sin IVA ni IIBB)."
        salida.append(resultado_tienda(
            nombre, items, aviso=aviso,
            buscar_url="https://www.nintendo.com/us/search/#q=" + urllib.parse.quote(consulta),
            acepta_global=(p == "US" and pais == "AR"),
        ))
    return salida


# --------------------------------------------------------------------------
# Cotización del dólar (Argentina)
# --------------------------------------------------------------------------

# Con tarjeta argentina, las compras en dólares se convierten al dólar oficial.
# El "dólar tarjeta" (oficial + 30% de Ganancias) NO aplica a videojuegos desde
# abril de 2025 (RG ARCA 5677), así que no se usa por defecto.
# "cripto" se usa como aproximación de la cotización de las billeteras con
# tarjeta Global (emitida afuera), que cada una fija a su manera.
TIPOS_DOLAR = {"oficial": "Oficial", "bolsa": "MEP", "cripto": "Cripto"}


def cotizacion_dolar():
    def traer():
        datos = http_json("https://dolarapi.com/v1/dolares")
        salida = {}
        for d in datos:
            casa = d.get("casa")
            if casa in TIPOS_DOLAR and d.get("venta"):
                salida[casa] = {
                    "nombre": TIPOS_DOLAR[casa],
                    "venta": d["venta"],
                    "actualizado": d.get("fechaActualizacion"),
                }
        return salida
    return cacheado("dolar", traer, ttl=30 * 60)


# --------------------------------------------------------------------------
# Búsqueda combinada
# --------------------------------------------------------------------------

_pool = ThreadPoolExecutor(max_workers=12)
_pool_xbox = ThreadPoolExecutor(max_workers=12)  # separado para evitar bloqueos


def _seguro(nombre, funcion, *args):
    try:
        r = funcion(*args)
        return r if isinstance(r, list) else [r]
    except Exception as e:
        print(f"[{nombre}] {type(e).__name__}: {e}", file=sys.stderr)
        return [resultado_tienda(nombre, error=describir_error(e))]


def buscar_todo(consulta, pais):
    def hacer():
        tareas = [
            _pool.submit(_seguro, "Steam", buscar_steam, consulta, pais),
            _pool.submit(_seguro, "Xbox", buscar_xbox, consulta, pais),
            _pool.submit(_seguro, "Nintendo eShop", buscar_nintendo, consulta, pais),
        ]
        tiendas = []
        for t in tareas:
            tiendas.extend(t.result())
        return {"consulta": consulta, "pais": pais, "tiendas": tiendas}
    resultado = cacheado(("buscar", normalizar(consulta), pais), hacer)
    # No guardar en caché respuestas donde fallaron todas las tiendas.
    if not any(t["ok"] for t in resultado["tiendas"]):
        with _cache_lock:
            _cache.pop(("buscar", normalizar(consulta), pais), None)
    return resultado


# --------------------------------------------------------------------------
# Ícono (PNG generado sin dependencias)
# --------------------------------------------------------------------------

_SIGNO = [  # signo $ de 5x7
    "..#..",
    ".####",
    "#.#..",
    ".###.",
    "..#.#",
    "####.",
    "..#..",
]


def generar_icono(tam):
    fondo, letra = (124, 58, 237), (255, 255, 255)
    escala = tam // 11
    ox = (tam - 5 * escala) // 2
    oy = (tam - 7 * escala) // 2
    filas = []
    for y in range(tam):
        fila = bytearray([0])
        for x in range(tam):
            gx, gy = (x - ox) // escala, (y - oy) // escala
            pinta = 0 <= gx < 5 and 0 <= gy < 7 and x >= ox and y >= oy and _SIGNO[gy][gx] == "#"
            fila.extend(letra if pinta else fondo)
        filas.append(bytes(fila))
    raw = b"".join(filas)

    def chunk(tipo, datos):
        c = struct.pack(">I", len(datos)) + tipo + datos
        return c + struct.pack(">I", zlib.crc32(tipo + datos) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", tam, tam, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


_iconos = {}


def icono(tam):
    if tam not in _iconos:
        _iconos[tam] = generar_icono(tam)
    return _iconos[tam]


MANIFEST = {
    "name": "Precios de Juegos",
    "short_name": "Precios",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0f0f14",
    "theme_color": "#7c3aed",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}


# --------------------------------------------------------------------------
# Interfaz HTML
# --------------------------------------------------------------------------

HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Precios de Juegos</title>
<meta name="theme-color" content="#7c3aed">
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon-192.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Precios">
<style>
:root{--bg:#f4f4f7;--card:#fff;--txt:#17171c;--sub:#6b6b78;--borde:#e3e3ea;--acento:#7c3aed;--ok:#0f9d58;--warn:#b45309;--err:#c62828}
@media (prefers-color-scheme: dark){:root{--bg:#0f0f14;--card:#1a1a22;--txt:#ececf1;--sub:#9a9aa8;--borde:#2c2c38;--acento:#a78bfa;--ok:#34d399;--warn:#fbbf24;--err:#f87171}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:16px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px;padding-bottom:40px}
main{max-width:640px;margin:0 auto}
h1{font-size:1.4rem;margin:4px 0 14px}
h2{font-size:1rem;margin:18px 0 6px}
form{display:flex;gap:8px;flex-wrap:wrap}
input,select,button{font:inherit;border-radius:12px;border:1px solid var(--borde);background:var(--card);color:var(--txt);padding:12px}
input{flex:1 1 200px;min-width:0}
button{background:var(--acento);color:#fff;border:none;font-weight:600;cursor:pointer}
.ajustes{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:12px 0 4px}
.ajustes label{display:flex;flex-direction:column;gap:4px;font-size:.8rem;color:var(--sub)}
.ajustes select{padding:10px;font-size:.92rem;width:100%}
@media (max-width:380px){.ajustes{grid-template-columns:1fr}}
.cot{font-size:.82rem;color:var(--sub);margin-bottom:6px}
.card{background:var(--card);border:1px solid var(--borde);border-radius:16px;padding:14px;margin:10px 0}
.card.mejor{border:2px solid var(--ok)}
.tienda{font-size:.8rem;text-transform:uppercase;letter-spacing:.05em;color:var(--sub);display:flex;justify-content:space-between;gap:8px}
.badge{background:var(--ok);color:#fff;border-radius:99px;padding:1px 8px;font-size:.72rem;letter-spacing:0;text-transform:none;white-space:nowrap}
.titulo{font-weight:600;margin:4px 0}
a{color:inherit}
.precio{font-size:1.5rem;font-weight:700}
.tachado{text-decoration:line-through;color:var(--sub);font-size:.95rem;margin-left:6px;font-weight:400}
.desc{color:var(--ok);font-weight:600;font-size:.9rem;margin-left:6px}
.det{color:var(--sub);font-size:.82rem}
.aviso{color:var(--warn);font-size:.82rem;margin-top:6px}
.global{margin-top:6px;font-size:.92rem}
.error{color:var(--err)}
.link{color:var(--acento)}
.otros{list-style:none;margin:10px 0 0;padding:10px 0 0;border-top:1px solid var(--borde)}
.otros li{display:flex;justify-content:space-between;gap:10px;padding:5px 0;font-size:.9rem}
.otros li span:last-child{white-space:nowrap;font-weight:600}
.otros .det{font-weight:400;display:block}
.ranking{background:var(--card);border:1px solid var(--borde);border-radius:16px;padding:6px 14px;list-style:none;margin:0}
.ranking li{display:flex;justify-content:space-between;gap:10px;padding:9px 0;border-bottom:1px solid var(--borde)}
.ranking li:last-child{border-bottom:none}
.ranking .t{font-size:.78rem;color:var(--sub);display:block}
.ranking .p{font-weight:700;white-space:nowrap}
.ranking li:first-child .p{color:var(--ok)}
.vacio,.cargando{color:var(--sub);text-align:center;padding:24px}
.pie{color:var(--sub);font-size:.78rem;margin-top:20px}
</style>
</head>
<body>
<main>
<h1>🎮 Precios de Juegos</h1>
<form id="f">
  <input id="q" type="search" placeholder="Nombre del juego…" autocomplete="off" enterkeyhint="search" required>
  <select id="pais" aria-label="País"></select>
  <button type="submit">Buscar</button>
</form>
<div id="panelAR">
  <div class="ajustes">
    <label>Impuestos
      <select id="imp">
        <option value="23">IVA 21% + IIBB 2% (CABA / Bs. As.)</option>
        <option value="24">IVA 21% + IIBB 3%</option>
        <option value="26">IVA 21% + IIBB 5%</option>
        <option value="21">Solo IVA 21%</option>
        <option value="0">Sin impuestos</option>
      </select>
    </label>
    <label>Cómo pagás los dólares
      <select id="tipoDolar"></select>
    </label>
  </div>
  <div class="cot" id="cot"></div>
</div>
<div id="res"><div class="vacio">Buscá un juego para comparar Steam, Xbox y Nintendo.</div></div>
<div class="pie" id="pie"></div>
</main>
<script>
const PAISES = __PAISES__;
const $ = s => document.querySelector(s);
const leer = (k, d) => { try { return localStorage.getItem(k) ?? d } catch { return d } };
const guardar = (k, v) => { try { localStorage.setItem(k, v) } catch {} };
let ultimo = null, dolar = null;

for (const [c, p] of Object.entries(PAISES)) $("#pais").add(new Option(p.nombre, c));
$("#pais").value = leer("pais", "AR");
$("#imp").value = leer("imp2", "23");

const esAR = () => $("#pais").value === "AR";
function fmt(valor, moneda) {
  if (valor === 0) return "Gratis";
  try { return new Intl.NumberFormat("es-AR", {style: "currency", currency: moneda || "USD", maximumFractionDigits: 2}).format(valor) }
  catch { return (moneda || "") + " " + valor.toFixed(2) }
}
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])) }

async function cargarDolar() {
  try {
    const r = await fetch("/api/dolar"); dolar = await r.json();
    if (dolar.error) throw 0;
    const sel = $("#tipoDolar"); sel.innerHTML = "";
    const etiquetas = {oficial: "Tarjeta (oficial)", bolsa: "Tus dólares (MEP)"};
    for (const [k, nombre] of Object.entries(etiquetas)) if (dolar[k]) sel.add(new Option(nombre, k));
    const guardado = leer("tipoDolar2", "oficial");
    sel.value = dolar[guardado] ? guardado : Object.keys(dolar)[0];
  } catch { dolar = null }
  actualizarPanel(); pintar();
}

function actualizarPanel() {
  $("#panelAR").style.display = esAR() ? "" : "none";
  const d = dolar && dolar[$("#tipoDolar").value];
  if (!dolar) $("#cot").textContent = "No se pudo obtener la cotización del dólar: los precios en dólares no se pasan a pesos.";
  else if (d) {
    const f = d.actualizado ? new Date(d.actualizado).toLocaleString("es-AR", {dateStyle: "short", timeStyle: "short"}) : "";
    $("#cot").textContent = `1 US$ = ${fmt(d.venta, "ARS")} (dólar ${d.nombre}${f ? ", " + f : ""})` +
      (dolar.cripto ? ` · Tarjeta Global ≈ ${fmt(dolar.cripto.venta, "ARS")} (dólar cripto, varía según la billetera)` : "");
  }
  $("#pie").textContent = esAR()
    ? "Desde abril de 2025 los videojuegos no pagan la percepción de Ganancias (RG ARCA 5677): con tarjeta argentina o con tus dólares se suma IVA 21% más Ingresos Brutos de tu provincia. Steam, Xbox y la eShop argentina no aceptan tarjeta Global, así que el IVA se paga siempre. La eShop de EE.UU. sí la acepta: con una tarjeta Global no se paga IVA ni IIBB, pero se usa la cotización de esa billetera (acá se aproxima con el dólar cripto). Algunas billeteras suman comisión o dan cashback. Los precios salen de las APIs de cada tienda: confirmá antes de comprar. Más detalle en impuestito.org."
    : "Los precios salen de las APIs de cada tienda y no incluyen impuestos locales. Confirmá en la tienda antes de comprar.";
}

// Calcula el precio final en pesos (con impuestos) y su desglose.
function calcular(it) {
  const base = {valor: it.precio, original: it.precio_original, moneda: it.moneda, desglose: ""};
  if (!esAR()) return base;
  const d = dolar && dolar[$("#tipoDolar").value];
  let factorMoneda;
  if (it.moneda === "ARS") factorMoneda = 1;
  else if (it.moneda === "USD" && d) factorMoneda = d.venta;
  else return base;
  const tasa = Number($("#imp").value) / 100;
  const enPesos = it.precio * factorMoneda;
  const final = enPesos * (1 + tasa);
  const partes = [];
  if (it.moneda === "USD") partes.push(`${fmt(it.precio, "USD")} = ${fmt(enPesos, "ARS")}`);
  else partes.push(`Precio tienda ${fmt(enPesos, "ARS")}`);
  if (tasa > 0 && it.precio > 0) {
    partes.push(`+ IVA ${fmt(enPesos * 0.21, "ARS")}`);
    if (tasa > 0.21) partes.push(`+ IIBB ${fmt(enPesos * (tasa - 0.21), "ARS")}`);
  }
  return {
    valor: final,
    original: it.precio_original ? it.precio_original * factorMoneda * (1 + tasa) : null,
    moneda: "ARS",
    desglose: it.precio > 0 ? partes.join(" ") : "",
  };
}

// Precio con tarjeta Global (sin IVA ni IIBB), solo en tiendas que la aceptan.
function calcularGlobal(t, it) {
  if (!esAR() || !t.acepta_global || it.moneda !== "USD" || !(it.precio > 0) || !dolar || !dolar.cripto) return null;
  return it.precio * dolar.cripto.venta;
}

function pintar() {
  const res = $("#res");
  if (!ultimo) return;
  if (ultimo.error) { res.innerHTML = `<div class="vacio error">${esc(ultimo.error)}</div>`; return }

  const tiendas = ultimo.tiendas.map(t => ({
    t,
    it: t.coincide ? t.resultados[0] : null,
    resto: t.coincide ? t.resultados.slice(1) : t.resultados,
  }));
  for (const x of tiendas) { x.c = x.it && calcular(x.it); x.g = x.it && calcularGlobal(x.t, x.it); }

  // Resumen: el mejor resultado de cada tienda, ordenado por precio final.
  // Las tiendas que aceptan tarjeta Global suman una segunda opción.
  const enRanking = tiendas.filter(x => x.c && x.c.valor > 0);
  for (const x of tiendas) if (x.g) enRanking.push({t: {tienda: x.t.tienda + " con tarjeta Global"}, it: x.it, c: {valor: x.g, moneda: "ARS"}, global: true, de: x});
  const monedas = new Set(enRanking.map(x => x.c.moneda));
  let html = "";
  if (enRanking.length > 1 && monedas.size === 1) {
    enRanking.sort((a, b) => a.c.valor - b.c.valor);
    html += `<h2>Todas las opciones${esAR() && $("#imp").value !== "0" ? ", con impuestos" : ""}</h2><ol class="ranking">${
      enRanking.map(x => `<li><span>${esc(x.t.tienda)}<span class="t">${esc(x.it.titulo)}</span></span><span class="p">${fmt(x.c.valor, x.c.moneda)}</span></li>`).join("")
    }</ol>`;
  }
  const primero = enRanking.length > 1 && monedas.size === 1 ? enRanking[0] : null;
  const mejor = primero && primero.global ? primero.de : primero;

  html += `<h2>Detalle por tienda</h2>` + tiendas.map(x => {
    const {t, it, c, resto} = x;
    let cuerpo;
    if (!t.ok) cuerpo = `<div class="error">⚠️ ${esc(t.error)}</div>`;
    else if (!it) cuerpo = `<div class="det">No se encontró este juego.${t.buscar_url ? ` <a class="link" href="${esc(t.buscar_url)}" target="_blank" rel="noopener">Buscar en la tienda</a>` : ""}</div>`;
    else {
      const pct = c.original ? Math.round((1 - c.valor / c.original) * 100) : 0;
      cuerpo = `<div class="titulo"><a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.titulo)}</a></div>
        <div><span class="precio">${fmt(c.valor, c.moneda)}</span>${c.original ? `<span class="tachado">${fmt(c.original, c.moneda)}</span><span class="desc">-${pct}%</span>` : ""}</div>
        ${c.desglose ? `<div class="det">${esc(c.desglose)}</div>` : ""}
        ${x.g ? `<div class="global">Con tarjeta Global: <b>${fmt(x.g, "ARS")}</b> <span class="det">(${fmt(x.it.precio, "USD")} al dólar cripto, sin IVA ni IIBB)</span></div>` : ""}`;
    }
    if (t.ok && resto.length) {
      cuerpo += `<ul class="otros">${resto.map(o => {
        const oc = calcular(o);
        return `<li><span><a href="${esc(o.url)}" target="_blank" rel="noopener">${esc(o.titulo)}</a>${oc.moneda !== o.moneda ? `<span class="det">${fmt(o.precio, o.moneda)} en la tienda</span>` : ""}</span><span>${fmt(oc.valor, oc.moneda)}</span></li>`;
      }).join("")}</ul>`;
    }
    return `<div class="card${x === mejor ? " mejor" : ""}">
      <div class="tienda"><span>${esc(t.tienda)}</span>${x === mejor ? '<span class="badge">Más barato</span>' : ""}</div>
      ${cuerpo}${t.aviso ? `<div class="aviso">${esc(t.aviso)}</div>` : ""}</div>`;
  }).join("");
  res.innerHTML = html;
}

$("#f").addEventListener("submit", async e => {
  e.preventDefault();
  const q = $("#q").value.trim(); if (!q) return;
  $("#q").blur();
  $("#res").innerHTML = '<div class="cargando">Buscando en las tiendas…</div>';
  try {
    const r = await fetch(`/api/buscar?q=${encodeURIComponent(q)}&pais=${$("#pais").value}`);
    ultimo = await r.json();
  } catch { ultimo = {error: "No se pudo conectar con el servidor. Si recién lo abriste, esperá un minuto y probá de nuevo."} }
  pintar();
});
$("#pais").addEventListener("change", () => { guardar("pais", $("#pais").value); actualizarPanel(); if ($("#q").value.trim()) $("#f").requestSubmit() });
$("#imp").addEventListener("change", () => { guardar("imp2", $("#imp").value); pintar() });
$("#tipoDolar").addEventListener("change", () => { guardar("tipoDolar2", $("#tipoDolar").value); actualizarPanel(); pintar() });
actualizarPanel();
cargarDolar();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Servidor
# --------------------------------------------------------------------------

class Manejador(BaseHTTPRequestHandler):
    server_version = "PreciosJuegos/2.0"

    def _enviar(self, codigo, cuerpo, tipo, cache=None):
        if isinstance(cuerpo, str):
            cuerpo = cuerpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(cuerpo)

    def _json(self, datos, codigo=200):
        self._enviar(codigo, json.dumps(datos, ensure_ascii=False), "application/json; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        args = urllib.parse.parse_qs(url.query)
        ruta = url.path

        if ruta == "/":
            pagina = HTML.replace("__PAISES__", json.dumps(PAISES, ensure_ascii=False))
            return self._enviar(200, pagina, "text/html; charset=utf-8")
        if ruta == "/api/buscar":
            q = (args.get("q") or [""])[0].strip()[:100]
            pais = (args.get("pais") or ["AR"])[0].upper()
            if pais not in PAISES:
                pais = "AR"
            if not q:
                return self._json({"error": "Falta el nombre del juego"}, 400)
            return self._json(buscar_todo(q, pais))
        if ruta == "/api/dolar":
            try:
                return self._json(cotizacion_dolar())
            except Exception as e:
                return self._json({"error": describir_error(e)}, 502)
        if ruta == "/manifest.json":
            return self._enviar(200, json.dumps(MANIFEST), "application/manifest+json", "public, max-age=86400")
        m = re.fullmatch(r"/(?:icon-(192|512)|apple-touch-icon)\.png", ruta)
        if m or ruta == "/favicon.ico":
            tam = int(m.group(1)) if m and m.group(1) else 180
            return self._enviar(200, icono(tam), "image/png", "public, max-age=86400")
        if ruta == "/healthz":
            return self._enviar(200, "ok", "text/plain")
        self._enviar(404, "No encontrado", "text/plain; charset=utf-8")

    def log_message(self, formato, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), formato % args))


def main():
    puerto = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    servidor = ThreadingHTTPServer((host, puerto), Manejador)
    print(f"Servidor listo en http://localhost:{puerto}  (Ctrl+C para cortar)", flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
