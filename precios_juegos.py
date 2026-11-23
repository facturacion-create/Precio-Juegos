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

import html
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher, get_close_matches
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
NINTENDO_REFERENCIA_US = set()  # vacío: en Argentina se muestra solo la eShop local

# Claves públicas que usa la propia web de Nintendo of America para buscar.
ALGOLIA_APP = "U3B6GR4UA3"
ALGOLIA_KEY = "a29c6927638bfd8cee23993e51e721c9"

# Palabras que indican que algo NO es el juego en sí.
# Fuertes: siempre (aunque la tienda lo clasifique como juego, como hace Xbox con las mejoras).
EXTRA_FUERTES = (
    "soundtrack", "dlc", "upgrade", "demo", "season pass", "add-on", "addon", "actualizacion",
    "mejora", "guidebook", "artbook", "art book", "expansion pass", "costume", "skin",
)
# Débiles: solo si la tienda no confirma que es un juego ("Lote" en Xbox suele ser el juego completo).
EXTRA_DEBILES = ("pack", "theme", "expansion", "bundle", "lote", "tema")
PALABRAS_EXTRA = EXTRA_FUERTES + EXTRA_DEBILES


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


ROMANOS = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}


def normalizar(texto):
    texto = re.sub(r"[™®©]", "", texto or "")  # antes de NFKD, que convierte ™ en "TM"
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c)).lower()
    texto = re.sub(r"[^a-z0-9]+", " ", texto)
    # Números romanos como números, para que 'GTA V' y 'GTA 5' sean lo mismo.
    texto = " ".join(ROMANOS.get(p, p) for p in texto.split())
    return texto.strip()


# Abreviaturas y nombres alternativos comunes -> cómo se buscan en las tiendas.
ALIAS = {
    "gta": "grand theft auto", "rdr": "red dead redemption", "rdr2": "red dead redemption 2",
    "cod": "call of duty", "botw": "zelda breath of the wild", "totk": "zelda tears of the kingdom",
    "tlou": "the last of us", "gow": "god of war", "nfs": "need for speed", "ff": "final fantasy",
    "acnh": "animal crossing new horizons", "bg3": "baldur's gate 3", "fh5": "forza horizon 5",
    "fh4": "forza horizon 4", "mk": "mortal kombat", "mk8": "mario kart 8", "ssbu": "super smash bros ultimate",
    "smash": "super smash bros", "spiderman": "marvel's spider-man", "spider-man": "marvel's spider-man", "pokemon": "pokémon", "re": "resident evil",
    "ac": "assassin's creed", "dbz": "dragon ball z", "kh": "kingdom hearts", "mgs": "metal gear solid",
    "tw3": "the witcher 3", "witcher": "the witcher", "rocket": "rocket league",
}


_A_ROMANO = {v: k.upper() for k, v in ROMANOS.items() if k != "x"}

NUMEROS_ES = {"uno": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5", "seis": "6",
              "siete": "7", "ocho": "8", "nueve": "9", "diez": "10"}

# Palabras de títulos de juegos reales, para corregir errores de tipeo ('cyberpunck' -> 'cyberpunk').
# Se llena con los más vendidos de Steam, el catálogo de Nintendo y de Game Pass, y cada búsqueda.
_vocabulario = set()
_titulos_conocidos = set()  # títulos normalizados, para corregir según el contexto
_vocab_lock = threading.Lock()


def sumar_vocabulario(titulos):
    norm = {normalizar(t) for t in titulos if t}
    nuevas = {p for t in norm for p in t.split() if len(p) >= 3 and not p.isdigit()}
    with _vocab_lock:
        _vocabulario.update(nuevas)
        _titulos_conocidos.update(norm)


def corregir(consulta):
    """Corrige palabras que no existen en ningún título conocido. Devuelve la
    consulta corregida (o la misma si no hay nada que corregir)."""
    with _vocab_lock:
        vocab = list(_vocabulario)
        titulos = list(_titulos_conocidos)
    if not vocab:
        return consulta
    vocab_set = set(vocab)
    palabras = normalizar(consulta).split()
    salida = []
    for i, p in enumerate(palabras):
        if len(p) >= 4 and not p.isdigit() and p not in vocab_set:
            cerca = get_close_matches(p, vocab, n=8, cutoff=0.7)
            otras = [w for j, w in enumerate(palabras) if j != i and w in vocab_set]

            def en_contexto(c):  # ¿hay un título real con esta palabra y las otras buscadas?
                return any(c in t.split() and all(w in t.split() for w in otras) for t in titulos) if otras else False
            # Preferir: que aparezca junto a las otras palabras en un título real ('elden ring'),
            # mismas letras en otro orden ('rign' -> 'ring'), misma primera letra, más parecida.
            cerca.sort(key=lambda c: (not en_contexto(c), sorted(c) != sorted(p), c[0] != p[0],
                                      -SequenceMatcher(None, p, c).ratio(), abs(len(c) - len(p))))
            salida.append(cerca[0] if cerca else p)
        else:
            salida.append(p)
    return " ".join(salida)


def armar_vocabulario():
    """Títulos populares de Steam y Nintendo (para el corrector)."""
    titulos = []
    for inicio in (0, 100, 200, 300, 400):
        try:
            url = "https://store.steampowered.com/search/results/?" + urllib.parse.urlencode({
                "filter": "topsellers", "category1": 998, "cc": "AR", "l": "spanish",
                "infinite": 1, "count": 100, "start": inicio,
            })
            titulos += re.findall(r'<span class="title">(.*?)</span>', http_json(url).get("results_html", ""))
        except Exception as e:
            print(f"[vocabulario steam] {type(e).__name__}: {e}", file=sys.stderr)
    try:  # Nintendo: los 1000 más populares (la API entrega de a 500 como máximo)
        datos = http_json(
            f"https://{ALGOLIA_APP.lower()}-dsn.algolia.net/1/indexes/*/queries",
            data={"requests": [{"indexName": "store_game_en_us", "query": "",
                                "params": f"hitsPerPage=500&page={pag}&attributesToRetrieve=title"} for pag in (0, 1)]},
            headers={"x-algolia-application-id": ALGOLIA_APP, "x-algolia-api-key": ALGOLIA_KEY},
        )
        titulos += [h.get("title", "") for r in datos["results"] for h in r.get("hits", [])]
    except Exception as e:
        print(f"[vocabulario nintendo] {type(e).__name__}: {e}", file=sys.stderr)
    sumar_vocabulario(html.unescape(t) for t in titulos)


def con_romanos(consulta):
    """'grand theft auto 6' -> 'grand theft auto VI' (algunas tiendas solo
    encuentran el juego escrito como en el título). None si no cambia nada."""
    nueva = " ".join(_A_ROMANO.get(p, p) for p in consulta.split())
    return nueva if nueva != consulta else None


def expandir_consulta(consulta):
    """'gta 5' -> 'grand theft auto 5', 're4' -> 'resident evil 4'. Devuelve la
    consulta tal cual si no hay nada que expandir."""
    if re.match(r"spider\s+man\b", consulta.strip(), re.I):
        consulta = "spiderman" + consulta.strip()[len("spider man"):]
    palabras = consulta.strip().split()
    ya = set(normalizar(consulta).split())
    salida = []
    for i, p in enumerate(palabras):
        clave_ = re.sub(r"[^a-z0-9]", "", p.lower())
        m = re.fullmatch(r"([a-z]+)(\d+)", clave_)  # 're4', 'gta5', 'fh5'
        # No expandir si lo buscado ya trae el nombre completo ("Marvel's Spider-Man").
        if i > 0 and clave_ in NUMEROS_ES:  # 'the witcher tres' -> 'the witcher 3'
            salida.append(NUMEROS_ES[clave_])
        elif clave_ in ALIAS and normalizar(ALIAS[clave_]).split()[0] in ya - {clave_}:
            salida.append(p)
        elif clave_ in ALIAS and (clave_ not in ("re", "ac", "mk", "ff", "kh") or i == 0):
            salida.append(ALIAS[clave_])
        elif m and m.group(1) in ALIAS and clave_ not in ALIAS:
            salida.append(f"{ALIAS[m.group(1)]} {m.group(2)}")
        else:
            salida.append(p)
    return " ".join(salida)


EDICIONES = (
    "edicion", "edition", "deluxe", "definitive", "definitiva", "complete",
    "completa", "goty", "game of the year", "remastered", "ultimate", "standard",
    "estandar", "gold", "premium", "for windows", "para windows", "windows",
    "nintendo switch", "switch 2", "solo pc",
)


_SEPARADOR = re.compile(r"\s*(?:[:\u2013\u2014]|\s-\s)\s*")


def sin_edicion(titulo):
    """Saca la edición y la plataforma del final, de a una parte por vez:
    'Hollow Knight: Edición Corazón Vacío' -> 'Hollow Knight'
    'Clive Barker's Hellraiser: Revival - Deluxe Edition (Switch 2)'
        -> 'Clive Barker's Hellraiser: Revival'."""
    t = titulo.strip()
    while True:
        antes = t
        # '(Switch 2)', '(solo PC)', '(PC)' al final
        m = re.search(r"\s*\(([^()]*)\)\s*$", t)
        if m and (any(e in normalizar(m.group(1)) for e in EDICIONES) or normalizar(m.group(1)) == "pc"):
            t = t[:m.start()]
        # ': Edición X', ' - Deluxe Edition', ' – Nintendo Switch 2 Edition' (la última parte)
        partes = _SEPARADOR.split(t)
        if len(partes) > 1 and any(e in normalizar(partes[-1]) for e in EDICIONES):
            ultimo = list(_SEPARADOR.finditer(t))[-1]
            parte = partes[-1]
            # 'ReFantazio Atlus 35th Anniversary Edition': sacar solo la edición
            # (hasta 3 palabras antes de 'Edition') y conservar el subtítulo real.
            resto = re.sub(r"(?:\s*\S+){0,3}\s*\b(edition|[eé]dici[oó]n)\b.*$", "", parte, flags=re.I).strip()
            empieza_con_edicion = any(normalizar(parte).startswith(e) for e in EDICIONES)
            if resto and not empieza_con_edicion and resto != parte:
                t = t[:ultimo.end()] + resto
            else:
                t = t[:ultimo.start()]
        if t == antes or not t:
            return t or titulo


IDIOMAS = ("language", "idioma", "spanish", "english", "german", "french", "italian",
           "portuguese", "japanese", "korean", "chinese", "russian", "polish")


def clave(titulo):
    """Nombre base del juego, sin edición ni plataforma.
    'Red Dead Redemption 2: Edición Definitiva' -> 'red dead redemption 2'."""
    t = normalizar(sin_edicion(titulo))
    t = re.sub(r"^(edicion \w+|version) de ", "", t)  # 'Edición definitiva de X', 'Versión de X'
    t = re.sub(r"\s+para xbox (one|series x s|series)$", "", t)
    t = re.sub(r"(\s+(para|for) (windows|pc|xbox)\b.*)$", "", t)
    t = re.sub(r"(\s+\w+)?\s+(edition|edicion)$", "", t)  # 'X Tarnished Edition'
    t = re.sub(r"\s+edicion \w+$", "", t)  # 'X Edición Deluxe'
    return t.strip() or normalizar(titulo)


def es_extra_por_nombre(titulo, confirmado_juego=False):
    t = " " + normalizar(titulo) + " "
    palabras = EXTRA_FUERTES + IDIOMAS if confirmado_juego else PALABRAS_EXTRA + IDIOMAS
    return any(f" {p} " in t for p in palabras)


def puntaje(consulta, item):
    """Qué tan parecido es un resultado a lo que se buscó (más alto = mejor).
    Se compara lo buscado tal cual y también sin la edición, porque los
    resultados se comparan sin la edición ('X: Complete Edition' -> 'x')."""
    return max(_puntaje(normalizar(consulta), item), _puntaje(clave(consulta), item))


def _puntaje(q, item):
    t = item["clave"]
    if not q or not t:
        return 0.0
    s = SequenceMatcher(None, q, t).ratio()
    if t == q:
        s += 1.0
    elif set(q.split()) <= set(t.split()):  # 'silksong' en 'hollow knight silksong'
        s += 0.55 + (0.1 if t.startswith(q) else 0)
    elif q in t:
        s += 0.15
    if item["tipo"] == "extra":
        s -= 0.6
    elif item["tipo"] == "edicion":
        s -= 0.02  # a igual nombre, primero el juego base
    return round(s, 3)


def preparar(consulta, items, tipo_origen=None):
    """Completa clave, tipo y puntaje de cada resultado y los ordena."""
    for it in items:
        it["clave"] = clave(it["titulo"])
        tipo = it.pop("_tipo", None) or "juego"
        confirmado = it.pop("_es_juego", False)  # la tienda confirma que es un juego
        if tipo != "extra" and es_extra_por_nombre(it["titulo"], confirmado):
            tipo = "extra"
        if tipo == "juego" and it["clave"] != normalizar(it["titulo"]):
            tipo = "edicion"
        it["tipo"] = tipo
        it["puntaje"] = puntaje(consulta, it)
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

# ---- Portadas ----
def portada_steam(appid):
    u = f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg"
    return u, u  # (banner, miniatura)


def portada_xbox(prod):
    imgs = {}
    for im in (prod.get("LocalizedProperties") or [{}])[0].get("Images") or []:
        imgs.setdefault(im.get("ImagePurpose"), "https:" + im["Uri"] if im.get("Uri", "").startswith("//") else im.get("Uri"))
    banner = imgs.get("TitledHeroArt") or imgs.get("SuperHeroArt") or imgs.get("BoxArt") or imgs.get("Poster")
    mini = imgs.get("BoxArt") or imgs.get("FeaturePromotionalSquareArt") or imgs.get("Poster") or banner
    return (banner + "?w=800" if banner else None), (mini + "?w=160&h=160" if mini else None)


def portada_nintendo(hit):
    img, cuadrada = hit.get("productImage"), hit.get("productImageSquare")
    banner = f"https://assets.nintendo.com/image/upload/ar_16:9,c_fill,w_800/f_auto/q_auto/{img}" if img else None
    if cuadrada and not cuadrada.startswith("http"):
        cuadrada = f"https://assets.nintendo.com/image/upload/c_fill,w_160,h_160/f_auto/q_auto/{cuadrada}"
    mini = cuadrada or (f"https://assets.nintendo.com/image/upload/c_fill,w_160,h_160/f_auto/q_auto/{img}" if img else None)
    return banner, mini


def _steam_tipo(appid):
    """'game', 'dlc', 'music', 'demo'... (o None si no se pudo saber)."""
    def traer():
        try:
            d = http_json(f"https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic")
            info = d.get(str(appid)) or {}
            return (info.get("data") or {}).get("type") if info.get("success") else None
        except Exception:
            return None
    return cacheado(("steam_tipo", appid), traer, ttl=7 * 24 * 3600)


def buscar_steam(consulta, pais):
    url = "https://store.steampowered.com/api/storesearch/?" + urllib.parse.urlencode(
        {"term": consulta, "cc": pais, "l": "spanish"}
    )
    datos = http_json(url)
    items = []
    for it in datos.get("items", []):
        if it.get("type") != "app":
            continue
        if re.search(r"hentai|\b18\+|nsfw|uncensored|brothel|lewd", (it.get("name") or "").lower()):
            continue  # contenido adulto que aparece al buscar palabras comunes
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
            "imagen": portada_steam(it.get("id"))[0], "mini": portada_steam(it.get("id"))[1],
            "_id": it.get("id"),
        })
    # Steam no dice en la búsqueda si algo es juego o DLC: se consulta aparte.
    tipos = list(_pool_sub.map(lambda it: _steam_tipo(it["_id"]), items))
    for it, tipo in zip(items, tipos):
        it.pop("_id", None)
        if tipo and tipo != "game":
            it["_tipo"] = "extra"  # dlc, music (soundtrack), demo, etc.
    return resultado_tienda(
        "Steam", preparar(consulta, items)[:10],
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
    romanos = con_romanos(consulta)
    if romanos:  # 'GTA 6' solo aparece buscando 'Grand Theft Auto VI'
        busquedas.insert(0, lambda: _xbox_ids_autosuggest(romanos, pais, locale))
    ids, errores = [], []
    # Las búsquedas de Microsoft devuelven cosas distintas: se combinan.
    for futuro in [_pool_sub.submit(b) for b in busquedas]:
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
            "imagen": portada_xbox(prod)[0], "mini": portada_xbox(prod)[1],
            "_es_juego": True,  # Microsoft dice que es un juego (ProductKind = Game), aunque se llame "Lote"
            "precio": lista,
            "precio_original": msrp if msrp > lista else None,
            "moneda": precio.get("CurrencyCode", ""),
            "gratis": lista == 0,
            "url": f"https://www.xbox.com/{locale}/games/store/x/{prod['ProductId']}",
        })
    items = preparar(consulta, items)
    # A igual parecido, preferir los que se juegan en consola.
    items.sort(key=lambda it: (it["puntaje"] - (0.05 if "(solo PC)" in it["titulo"] else 0)), reverse=True)
    return resultado_tienda("Xbox", items[:10], buscar_url=buscar_url)


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
            "params": "hitsPerPage=15",
        }]},
        headers={"x-algolia-application-id": ALGOLIA_APP, "x-algolia-api-key": ALGOLIA_KEY},
    )
    juegos = []
    for h in datos["results"][0].get("hits", []):
        nsuid = str(h.get("nsuid") or "")
        if not nsuid.isdigit() or h.get("topLevelCategoryCode") not in (None, "GAMES"):
            continue  # sin precio en la eShop (p. ej. juegos de celular)
        if "upgrade pack" in (h.get("title") or "").lower():
            continue  # mejoras gratis/pagas para Switch 2, no el juego
        plataforma = "Switch 2" if h.get("platformCode") == "NINTENDO_SWITCH_2" else "Switch"
        titulo = re.sub(r"\s+", " ", h.get("title", "")).strip()
        if plataforma == "Switch 2" and "switch 2" not in titulo.lower():
            titulo += " (Switch 2)"
        dlc = h.get("dlcType")  # None = juego, 'ROM Bundle' = edición completa, otro = DLC
        juegos.append({
            "nsuid": nsuid,
            "titulo": titulo,
            "imagen": portada_nintendo(h)[0], "mini": portada_nintendo(h)[1],
            "url": "https://www.nintendo.com" + (h.get("url") or ""),
            "_tipo": "extra" if dlc and dlc != "ROM Bundle" else None,
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
    juegos = preparar(consulta, _nintendo_buscar(consulta))[:12]
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
                    "imagen": j.get("imagen"), "mini": j.get("mini"),
                    "clave": j["clave"], "tipo": j["tipo"],
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
# Mega descuentos
# --------------------------------------------------------------------------

# Géneros comunes a las tres tiendas (cada una los nombra distinto).
GENEROS = {
    "Acción": ("accion", "action"),
    "Aventura": ("aventura", "adventure"),
    "Rol": ("rol", "role playing", "rpg", "jrpg"),
    "Estrategia": ("estrategia", "strategy"),
    "Disparos": ("disparos", "shooter", "shooting", "fps"),
    "Deportes": ("deportes", "sports"),
    "Carreras": ("carreras", "racing", "conduccion"),
    "Lucha": ("lucha", "fighting"),
    "Plataformas": ("plataformas", "platformer"),
    "Terror": ("terror", "horror"),
    "Puzles": ("puzles", "puzzle"),
    "Simulación": ("simulacion", "simulation"),
    "Indie": ("indie",),
    "Familiar": ("family", "familiar", "party", "fiesta", "kids"),
}


def generos_de(etiquetas):
    salida = []
    for genero, claves in GENEROS.items():
        for e in etiquetas:
            t = " " + normalizar(e) + " "
            if any(f" {c} " in t for c in claves):
                salida.append(genero)
                break
    return salida


def _etiquetas_steam():
    """{id de etiqueta de Steam: nombre}, para saber el género de cada juego."""
    def traer():
        return {d["tagid"]: d["name"] for d in http_json("https://store.steampowered.com/tagdata/populartags/spanish")}
    return cacheado("etiquetas_steam", traer, ttl=24 * 3600)


def _popularidad(items, clave, mayor_es_mejor=False):
    """Pone en cada item 'pop' entre 0 (el más popular de su tienda) y 1."""
    orden = sorted(items, key=lambda i: i.get(clave) or 0, reverse=mayor_es_mejor)
    for n, i in enumerate(orden):
        i["pop"] = round(n / max(1, len(orden) - 1), 3)
        i.pop(clave, None)


def _descuentos_steam(pais):
    """Los más vendidos de Steam que están en oferta (solo juegos)."""
    url = "https://store.steampowered.com/search/results/?" + urllib.parse.urlencode({
        "specials": 1, "filter": "topsellers", "category1": 998, "cc": pais,
        "l": "spanish", "infinite": 1, "count": 100,
    })
    h = http_json(url).get("results_html", "")
    try:
        etiquetas = _etiquetas_steam()
    except Exception:
        etiquetas = {}
    items = []
    for fila in h.split('<a href="')[1:]:
        appid = re.search(r'data-ds-appid="(\d+)"', fila)
        titulo = re.search(r'<span class="title">(.*?)</span>', fila, re.S)
        desc = re.search(r'data-discount="(\d+)"', fila)
        final = re.search(r'data-price-final="(\d+)"', fila)
        if not (appid and titulo and desc and final) or int(desc.group(1)) <= 0:
            continue
        pct = int(desc.group(1))
        precio = int(final.group(1)) / 100
        tags = re.search(r'data-ds-tagids="\[([\d,]*)\]"', fila)
        nombres = [etiquetas.get(int(t), "") for t in (tags.group(1).split(",") if tags and tags.group(1) else [])]
        items.append({
            "tienda": "Steam", "titulo": html.unescape(titulo.group(1)).strip(),
            "generos": generos_de(nombres), "_rango": len(items),
            "mini": portada_steam(appid.group(1))[1],
            "precio": precio, "precio_original": round(precio / (1 - pct / 100), 2) if pct < 100 else None,
            "moneda": "USD" if pais == "AR" else "", "descuento": pct,
            "url": f"https://store.steampowered.com/app/{appid.group(1)}/",
        })
    _popularidad(items, "_rango")  # vienen ordenados por más vendidos
    return items


def _descuentos_nintendo(pais, minimo):
    """Ofertas de la eShop (EE.UU. según Algolia) con el precio de cada región."""
    url = f"https://{ALGOLIA_APP.lower()}-dsn.algolia.net/1/indexes/*/queries"
    datos = http_json(
        url,
        data={"requests": [{
            "indexName": "store_game_en_us", "query": "",
            "params": urllib.parse.urlencode({
                "hitsPerPage": 100,
                "filters": 'topLevelFilters:"Deals"',
                "numericFilters": f"price.percentOff>={minimo}",
            }),
        }]},
        headers={"x-algolia-application-id": ALGOLIA_APP, "x-algolia-api-key": ALGOLIA_KEY},
    )
    juegos = {}
    for h_ in datos["results"][0].get("hits", []):
        nsuid = str(h_.get("nsuid") or "")
        if not nsuid.isdigit() or (h_.get("dlcType") and h_.get("dlcType") != "ROM Bundle"):
            continue
        titulo = re.sub(r"\s+", " ", h_.get("title", "")).strip()
        if h_.get("platformCode") == "NINTENDO_SWITCH_2" and "switch 2" not in titulo.lower():
            titulo += " (Switch 2)"
        juegos[nsuid] = {"titulo": titulo, "url": "https://www.nintendo.com" + (h_.get("url") or ""),
                         "generos": generos_de(h_.get("gameGenreLabels") or []),
                         "mini": portada_nintendo(h_)[1],
                         "_rango": h_.get("popularityRank") or 99999}
    salida = []
    regiones = [pais, "US"] if pais in NINTENDO_REFERENCIA_US else [pais]
    ids = list(juegos)
    for region in regiones:
        nombre = "Nintendo eShop " + ("EE.UU." if region == "US" else PAISES[region]["nombre"]) if len(regiones) > 1 else "Nintendo eShop"
        for i in range(0, len(ids), 50):  # la API acepta hasta 50 por consulta
            for nsuid, pr in _nintendo_precios(ids[i:i + 50], region).items():
                if not pr["precio_original"]:
                    continue  # en esta región no está en oferta
                pct = round((1 - pr["precio"] / pr["precio_original"]) * 100)
                salida.append({
                    "tienda": nombre, **juegos[nsuid], "precio": pr["precio"],
                    "precio_original": pr["precio_original"], "moneda": pr["moneda"],
                    "descuento": pct, "acepta_global": region == "US" and pais == "AR",
                })
    _popularidad(salida, "_rango")
    return salida


# Listas públicas del catálogo de Game Pass (consola y PC). Incluyen casi todos
# los juegos de Xbox, Bethesda y Activision, que no están en otras tiendas.
GAMEPASS_LISTAS = ("f6f1f99f-9b49-4ccd-b3bf-4d9767a77f5e", "fdd9e2a7-0fee-49f6-ad69-4354098401ff")


def _xbox_ids_gamepass(pais):
    ids = []
    for lista in GAMEPASS_LISTAS:
        try:
            datos = http_json(f"https://catalog.gamepass.com/sigls/v2?id={lista}&language=es-ar&market={pais}")
            ids.extend(x["id"] for x in datos[1:] if isinstance(x, dict) and "id" in x)
        except Exception as e:
            print(f"[game pass] {type(e).__name__}: {e}", file=sys.stderr)
    return ids


def _descuentos_xbox(titulos, pais):
    """Microsoft no publica una lista de ofertas. Se revisan los precios de:
    - los juegos del catálogo de Game Pass (incluye los exclusivos de Xbox), y
    - los juegos en oferta en otras tiendas (las promos suelen coincidir)."""
    locale = PAISES.get(pais, {}).get("locale", "es-AR")

    def ids_de(titulo):
        try:
            return _xbox_ids_autosuggest(titulo, pais, locale)[:3]
        except Exception:
            return []
    ids = list(dict.fromkeys(_xbox_ids_gamepass(pais)))
    vistos = set(ids)
    for lista in _pool_sub.map(ids_de, titulos):
        for i in lista:
            if i not in vistos:
                vistos.add(i)
                ids.append(i)

    def lote(k):
        url = "https://displaycatalog.mp.microsoft.com/v7.0/products?" + urllib.parse.urlencode({
            "bigIds": ",".join(ids[k:k + 20]), "market": pais, "languages": locale,
        })
        try:
            return http_json(url).get("Products", [])
        except Exception:
            return []
    salida = []
    for productos in _pool_sub.map(lote, range(0, len(ids), 20)):
        sumar_vocabulario((p.get("LocalizedProperties") or [{}])[0].get("ProductTitle", "") for p in productos)
        for prod in productos:
            if prod.get("ProductKind") != "Game":
                continue
            pr = _xbox_precio(prod)
            if not pr:
                continue
            lista_, msrp = float(pr.get("ListPrice") or 0), float(pr.get("MSRP") or 0)
            if lista_ <= 0 or msrp <= lista_:
                continue
            titulo = (prod.get("LocalizedProperties") or [{}])[0].get("ProductTitle", "")
            uso = {u.get("AggregateTimeSpan"): u.get("RatingCount") or 0
                   for u in ((prod.get("MarketProperties") or [{}])[0].get("UsageData") or [])}
            salida.append({
                "tienda": "Xbox", "titulo": titulo, "precio": lista_, "precio_original": msrp,
                # 'Categories' a veces viene vacío y el género está solo en 'Category'
                "mini": portada_xbox(prod)[1],
                "generos": generos_de(((prod.get("Properties") or {}).get("Categories") or [])
                                      + [(prod.get("Properties") or {}).get("Category") or ""]),
                "_rango": uso.get("30Days", 0),
                "moneda": pr.get("CurrencyCode", ""), "descuento": round((1 - lista_ / msrp) * 100),
                "url": f"https://www.xbox.com/{locale}/games/store/x/{prod['ProductId']}",
            })
    _popularidad(salida, "_rango", mayor_es_mejor=True)  # más calificaciones en 30 días = más jugado
    return salida


def mega_descuentos(pais, minimo=50):
    def traer():
        items, errores = [], []
        for nombre, f in (("Steam", lambda: _descuentos_steam(pais)),
                          ("Nintendo eShop", lambda: _descuentos_nintendo(pais, minimo))):
            try:
                items.extend(f())
            except Exception as e:
                print(f"[descuentos {nombre}] {type(e).__name__}: {e}", file=sys.stderr)
                errores.append(f"{nombre}: {describir_error(e)}")
        try:
            titulos = sorted({re.sub(r"\s*\(Switch 2\)$", "", i["titulo"]) for i in items})
            items.extend(_descuentos_xbox(titulos, pais))
        except Exception as e:
            print(f"[descuentos Xbox] {type(e).__name__}: {e}", file=sys.stderr)
            errores.append(f"Xbox: {describir_error(e)}")
        items = [i for i in items if i["descuento"] >= minimo and i["precio"] > 0]
        # Sin repetidos (en Xbox la versión de consola y la de PC son productos distintos).
        unicos = {}
        for i in sorted(items, key=lambda i: i["precio"]):
            unicos.setdefault((i["tienda"], normalizar(i["titulo"])), i)
        items = sorted(unicos.values(), key=lambda i: (-i["descuento"], normalizar(i["titulo"])))
        return {"pais": pais, "minimo": minimo, "items": items, "errores": errores}
    return cacheado(("descuentos", pais, minimo), traer, ttl=60 * 60)


# --------------------------------------------------------------------------
# Búsqueda combinada
# --------------------------------------------------------------------------

_pool = ThreadPoolExecutor(max_workers=12)
_pool_sub = ThreadPoolExecutor(max_workers=16)  # separado para evitar bloqueos


def _seguro(nombre, funcion, *args):
    try:
        r = funcion(*args)
        return r if isinstance(r, list) else [r]
    except Exception as e:
        print(f"[{nombre}] {type(e).__name__}: {e}", file=sys.stderr)
        return [resultado_tienda(nombre, error=describir_error(e))]


def detectar_juegos(tiendas):
    """Agrupa los resultados de todas las tiendas por juego (clave) y devuelve
    los candidatos ordenados: el primero es el que probablemente se buscó."""
    grupos = {}
    for t in tiendas:
        for it in t["resultados"]:
            if it.get("tipo") == "extra" or it["puntaje"] < PUNTAJE_MINIMO:
                continue
            g = grupos.setdefault(it["clave"], {"clave": it["clave"], "puntaje": 0, "tiendas": set(), "titulos": [],
                                                "pago": False, "imagenes": []})
            if it.get("imagen"):
                # Preferir la portada del juego base antes que la de una edición
                g["imagenes"].append((it.get("tipo") != "juego", it["imagen"], it.get("mini") or it["imagen"]))
            g["puntaje"] = max(g["puntaje"], it["puntaje"])
            g["pago"] = g["pago"] or it["precio"] > 0
            g["tiendas"].add(t["tienda"])
            g["titulos"].append((it["tipo"] != "juego", len(it["titulo"]), it["titulo"]))
    salida = []
    for g in grupos.values():
        # Nombre para mostrar: el del juego base más corto (sin edición ni plataforma).
        titulo = min(g["titulos"])[2]
        titulo = re.sub(r"\s*\((Switch 2|solo PC)\)$", "", titulo)
        img = min(g["imagenes"]) if g["imagenes"] else (False, None, None)
        salida.append({"clave": g["clave"], "titulo": titulo, "puntaje": g["puntaje"],
                       "n": len(g["tiendas"]), "_pago": g["pago"], "imagen": img[1], "mini": img[2]})
    # Un juego que está en varias tiendas es más probablemente el que se buscó.
    salida.sort(key=lambda g: (g["_pago"], round(g["puntaje"] + 0.2 * (g["n"] - 1), 1), g["n"], g["puntaje"]), reverse=True)
    for g in salida:
        g.pop("_pago")
    return salida[:12]


BUSCADORES = {"Steam": buscar_steam, "Xbox": buscar_xbox, "Nintendo eShop": buscar_nintendo}


def fusionar(tiendas, nuevas, consulta):
    """Suma a cada tienda los resultados nuevos (sin repetir) y los vuelve a
    puntuar contra lo que buscó el usuario."""
    for nt in nuevas:
        t = next((t for t in tiendas if t["tienda"] == nt["tienda"]), None)
        if not t or not nt.get("ok"):
            continue
        if not t["ok"]:  # la primera vez falló y ahora respondió
            t.update(ok=True, error=None)
        urls = {r["url"] for r in t["resultados"]}
        for r in nt["resultados"]:
            if r["url"] not in urls:
                urls.add(r["url"])
                t["resultados"].append(r)
        for r in t["resultados"]:
            r["puntaje"] = puntaje(consulta, r)
        t["resultados"].sort(key=lambda r: r["puntaje"], reverse=True)
        t["coincide"] = bool(t["resultados"]) and t["resultados"][0]["puntaje"] >= PUNTAJE_MINIMO


def buscar_todo(consulta, pais):
    original = consulta
    consulta = expandir_consulta(consulta)

    corregido = [False]

    def hacer():
        nonlocal consulta
        tareas = [_pool.submit(_seguro, nombre, f, consulta, pais) for nombre, f in BUSCADORES.items()]
        tiendas = []
        for t in tareas:
            tiendas.extend(t.result())

        # Errores de tipeo: si nada coincide bien, probar con la búsqueda corregida.
        juegos = detectar_juegos(tiendas)
        if not juegos or juegos[0]["puntaje"] < 1.4:
            corregida = corregir(consulta)
            if corregida != normalizar(consulta):
                nuevas = []
                for lista in _pool.map(lambda n: _seguro(n, BUSCADORES[n], corregida, pais), BUSCADORES):
                    nuevas.extend(lista)
                consulta = corregida
                corregido[0] = True
                fusionar(tiendas, nuevas, consulta)

        # Segunda vuelta para no perder juegos que la búsqueda de una tienda escondió.
        extra = []  # (tienda, texto a buscar)
        # 1) La secuela más nueva: si aparecen las partes 3, 4 y 5, probar la 6
        #    (Microsoft solo encuentra 'Grand Theft Auto VI' escrito exacto).
        base = normalizar(consulta)
        if not re.search(r"\d", base):
            numeros = [int(m.group(1)) for t in tiendas for r in t["resultados"]
                       for m in [re.match(re.escape(base) + r"\s+(\d+)\b", r.get("clave", ""))] if m]
            if numeros and max(numeros) < 10:
                siguiente = _A_ROMANO.get(str(max(numeros) + 1))
                extra += [(n, f"{consulta} {siguiente}") for n in BUSCADORES]
        # 2) Juegos encontrados en una tienda pero no en otra: buscarlos ahí por su nombre exacto.
        for g in detectar_juegos(tiendas)[:6]:
            for t in tiendas:
                if t["ok"] and not any(r.get("clave") == g["clave"] for r in t["resultados"]):
                    nombre = next((n for n in BUSCADORES if t["tienda"].startswith(n)), None)
                    if nombre:
                        extra.append((nombre, g["titulo"]))
        extra = list(dict.fromkeys(extra))[:18]
        if extra:
            for nuevas in _pool.map(lambda e: _seguro(e[0], BUSCADORES[e[0]], e[1], pais), extra):
                fusionar(tiendas, nuevas, consulta)
        sumar_vocabulario(r["titulo"] for t in tiendas for r in t["resultados"])
        return {"consulta": original, "buscado": consulta, "corregido": corregido[0], "pais": pais,
                "tiendas": tiendas, "juegos": detectar_juegos(tiendas)}
    clave_cache = ("buscar", normalizar(consulta), pais)
    resultado = dict(cacheado(clave_cache, hacer), consulta=original)
    # No guardar en caché respuestas donde fallaron todas las tiendas.
    if not any(t["ok"] for t in resultado["tiendas"]):
        with _cache_lock:
            _cache.pop(clave_cache, None)
    return resultado


# --------------------------------------------------------------------------
# Ícono y favicon (archivos en la carpeta static/)
# --------------------------------------------------------------------------

CARPETA_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
ICONOS = {  # ruta pedida -> (archivo, tipo)
    "/favicon.ico": ("icon-32.png", "image/png"),
    "/favicon.png": ("icon-32.png", "image/png"),
    "/apple-touch-icon.png": ("icon-180.png", "image/png"),
    "/apple-touch-icon-precomposed.png": ("icon-180.png", "image/png"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
}
_iconos = {}


def icono(ruta):
    """Devuelve (bytes, tipo) del ícono pedido, o None si no existe."""
    if ruta not in ICONOS:
        return None
    archivo, tipo = ICONOS[ruta]
    if archivo not in _iconos:
        try:
            with open(os.path.join(CARPETA_STATIC, archivo), "rb") as f:
                _iconos[archivo] = f.read()
        except OSError:
            return None
    return _iconos[archivo], tipo


MANIFEST = {
    "name": "Precios de Juegos",
    "short_name": "Precios",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0f0f14",
    "theme_color": "#7c3aed",
    "icons": [
        {"src": "/icon-192.png?v=2", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
        {"src": "/icon-512.png?v=2", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
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
<link rel="manifest" href="/manifest.json?v=2">
<link rel="icon" type="image/png" sizes="32x32" href="/favicon.png?v=2">
<link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png?v=2">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png?v=2">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Precios">
<style>
:root{--bg:#f4f4f7;--card:#fff;--txt:#17171c;--sub:#6b6b78;--borde:#e3e3ea;--acento:#7c3aed;--ok:#0f9d58;--okbg:#e7f6ee;--warn:#b45309;--err:#c62828}
@media (prefers-color-scheme: dark){:root{--bg:#0f0f14;--card:#1a1a22;--txt:#ececf1;--sub:#9a9aa8;--borde:#2c2c38;--acento:#a78bfa;--ok:#34d399;--okbg:#10291f;--warn:#fbbf24;--err:#f87171}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:16px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px;padding-top:max(16px,env(safe-area-inset-top));padding-bottom:40px}
main{max-width:640px;margin:0 auto}
h1{font-size:1.25rem;margin:4px 0 12px}
h2{font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;color:var(--sub);margin:22px 0 8px;font-weight:600}
a{color:inherit}
form{display:flex;gap:8px}
input,select,button{font:inherit;border-radius:12px;border:1px solid var(--borde);background:var(--card);color:var(--txt);padding:12px}
input{flex:1;min-width:0}
button{background:var(--acento);color:#fff;border:none;font-weight:600;cursor:pointer}
button.sec{background:var(--card);color:var(--acento);border:1px solid var(--borde);padding:8px 12px;font-size:.9rem}
details>summary{cursor:pointer;list-style:none}
details>summary::-webkit-details-marker{display:none}
/* Ajustes */
.ajustes{margin:10px 0 0;font-size:.85rem;color:var(--sub)}
.ajustes>summary{padding:4px 0}
.ajustes>summary::after{content:" ▾"}
.ajustes[open]>summary::after{content:" ▴"}
.grilla{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
.grilla label{display:flex;flex-direction:column;gap:4px;font-size:.78rem}
.grilla label.ancho{grid-column:1/-1}
.grilla select{padding:10px;font-size:.92rem;width:100%}
.cot{font-size:.8rem;margin-top:8px}
/* Resultado */
.jcard{background:var(--card);border:1px solid var(--borde);border-radius:16px;margin:8px 0;overflow:hidden}
.jcard>summary{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:13px 14px}
.jcard>summary::after{content:"›";color:var(--sub);font-size:1.3rem;transition:transform .15s}
.jcard[open]>summary::after{transform:rotate(90deg)}
.jcard[open]>summary{border-bottom:1px solid var(--borde)}
.jtit{flex:1;min-width:0;font-weight:700}
.jprecio{font-weight:700;white-space:nowrap;text-align:right}
.jprecio small{display:block;font-size:.72rem;font-weight:600;color:var(--sub)}
.jbody{padding:10px 12px 12px}
.jbody .lista{border-radius:12px}
.jacciones{display:flex;justify-content:flex-end;margin-bottom:8px}
.corregido{font-size:.88rem;color:var(--sub);margin:2px 0 6px}
.portada{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:16px;margin:6px 0 10px;background:var(--borde)}
.portada.chica{border-radius:12px;margin-top:0;aspect-ratio:21/8}
.mini{flex:none;width:48px;height:48px;object-fit:cover;border-radius:10px;background:var(--borde)}
.mini-wrap{position:relative;flex:none;display:flex}
.mini-wrap .mini{width:56px;height:56px}
.mini-wrap .pct{position:absolute;left:-6px;bottom:-6px;min-width:0;font-size:.7rem;padding:2px 5px;border-radius:7px}
.mini-wrap .mini[style*="none"] + .pct{position:static}
.volver{background:none;color:var(--acento);border:none;padding:6px 0;margin-top:14px;font-weight:600;font-size:.95rem}
.juego{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-top:6px}
.juego h3{font-size:1.5rem;line-height:1.2;margin:0}
.guardar{flex:none}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px;font-size:.85rem;align-items:center;color:var(--sub)}
.chip{background:var(--card);border:1px solid var(--borde);border-radius:99px;padding:5px 11px;color:var(--txt);cursor:pointer;font-size:.85rem}
.lista{background:var(--card);border:1px solid var(--borde);border-radius:16px;overflow:hidden}
.fila{border-bottom:1px solid var(--borde)}
.fila:last-child{border-bottom:none}
.fila>summary{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:13px 14px}
.fila.mejor>summary{background:var(--okbg)}
.fila>summary>span:first-child{flex:1;min-width:0}
.fila .tienda{font-weight:600}
.fila .nota{display:block;font-size:.78rem;color:var(--sub);font-weight:400}
.fila .precio{font-weight:700;font-size:1.15rem;white-space:nowrap;text-align:right}
.fila .precio small{display:block;font-size:.72rem;font-weight:600;color:var(--ok)}
.fila.mejor .precio{color:var(--ok)}
.fila>summary::after{content:"›";color:var(--sub);font-size:1.3rem;transition:transform .15s}
.fila[open]>summary::after{transform:rotate(90deg)}
.det{padding:0 14px 14px;font-size:.88rem;color:var(--sub)}
.det p{margin:6px 0}
.det .link{color:var(--acento);font-weight:600}
.det ul{margin:6px 0 0;padding-left:18px}
.det li{margin:3px 0}
.det .warn{color:var(--warn)}
.tachado{text-decoration:line-through}
.falta{font-size:.85rem;color:var(--sub);margin-top:10px}
.falta a{color:var(--acento)}
.extras{margin-top:12px;font-size:.9rem}
.extras>summary{color:var(--acento);padding:6px 0}
.extras ul{list-style:none;padding:0;margin:4px 0 0;background:var(--card);border:1px solid var(--borde);border-radius:12px}
.extras li{display:flex;justify-content:space-between;gap:10px;padding:8px 12px;border-bottom:1px solid var(--borde)}
.extras li:last-child{border-bottom:none}
.extras li span:last-child{white-space:nowrap;font-weight:600}
.extras .t{display:block;font-size:.75rem;color:var(--sub)}
/* Mis juegos */
.bib{background:var(--card);border:1px solid var(--borde);border-radius:16px;overflow:hidden}
.bib-item{display:flex;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid var(--borde)}
.bib-item:last-child{border-bottom:none}
.bib-item .info{flex:1;min-width:0;cursor:pointer}
.bib-item .nom{font-weight:600}
.bib-item .sub{font-size:.8rem;color:var(--sub)}
.bib-item .p{font-weight:700;white-space:nowrap;text-align:right}
.bib-item .p small{display:block;font-size:.72rem;font-weight:600}
.baja{color:var(--ok)}.sube{color:var(--err)}
.x{background:none;border:none;color:var(--sub);font-size:1.1rem;padding:4px 6px}
.acciones{display:flex;justify-content:space-between;align-items:center;margin-top:8px;gap:8px}
.alerta{background:var(--okbg);border:1px solid var(--ok);border-radius:16px;padding:12px 14px;margin-top:16px}
.alerta>b{display:block;margin-bottom:4px}
.alerta-item{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:8px 0;border-top:1px solid var(--borde);cursor:pointer}
.alerta-item:first-of-type{border-top:none}
.alerta-item .t{display:block;font-size:.8rem;color:var(--ok);font-weight:600}
.alerta-item .p{font-weight:700;white-space:nowrap}
.nuevo{background:var(--ok);color:#fff;border-radius:6px;padding:1px 5px;font-size:.68rem;font-weight:700;vertical-align:middle}
.enoferta{color:var(--ok);font-weight:600}
.tabs{display:flex;gap:6px;margin-top:18px;border-bottom:1px solid var(--borde)}
.tab{background:none;color:var(--sub);border:none;border-bottom:2px solid transparent;border-radius:0;padding:10px 8px;font-weight:600}
.tab.activa{color:var(--txt);border-bottom-color:var(--acento)}
.filtros{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0 8px}
.filtros.desliza{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;padding-bottom:4px;margin-right:-16px;padding-right:16px}
.filtros.desliza .chip{flex:none}
.chip.activa{background:var(--acento);border-color:var(--acento);color:#fff}
.ofertas{background:var(--card);border:1px solid var(--borde);border-radius:16px;overflow:hidden}
.oferta{display:flex;align-items:center;gap:10px;padding:11px 14px;border-bottom:1px solid var(--borde);cursor:pointer}
.oferta:last-child{border-bottom:none}
.oferta .pct{flex:none;background:var(--ok);color:#fff;font-weight:700;font-size:.82rem;border-radius:8px;padding:4px 6px;min-width:48px;text-align:center}
.oferta .info{flex:1;min-width:0}
.oferta .nom{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.oferta .sub{font-size:.78rem;color:var(--sub)}
.oferta .p{text-align:right;white-space:nowrap;font-weight:700}
.oferta .p small{display:block;font-weight:400;font-size:.75rem;color:var(--sub);text-decoration:line-through}
.vacio,.cargando{color:var(--sub);text-align:center;padding:28px 10px}
.error{color:var(--err)}
.pie{color:var(--sub);font-size:.76rem;margin-top:24px}
</style>
</head>
<body>
<main>
<h1>🎮 Precios de Juegos</h1>
<form id="f">
  <input id="q" type="search" placeholder="¿Qué juego buscás?" autocomplete="off" enterkeyhint="search" required>
  <button type="submit">Buscar</button>
</form>
<details class="ajustes" id="ajustes">
  <summary id="resumenAjustes">Ajustes</summary>
  <div class="grilla">
    <label class="ancho">Impuestos
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
    <label>País
      <select id="pais"></select>
    </label>
  </div>
  <div class="cot" id="cot"></div>
</details>
<div id="res"></div>
<div class="pie" id="pie"></div>
</main>
<script>
const PAISES = __PAISES__;
const $ = s => document.querySelector(s);
const leer = (k, d) => { try { return localStorage.getItem(k) ?? d } catch { return d } };
const guardar = (k, v) => { try { localStorage.setItem(k, v) } catch {} };
let ultimo = null, dolar = null, juegoSel = null;

for (const [c, p] of Object.entries(PAISES)) $("#pais").add(new Option(p.nombre, c));
$("#pais").value = leer("pais", "AR");
$("#imp").value = leer("imp2", "23");

const esAR = () => $("#pais").value === "AR";
function fmt(valor, moneda) {
  if (valor === 0) return "Gratis";
  try { return new Intl.NumberFormat("es-AR", {style: "currency", currency: moneda || "USD", maximumFractionDigits: moneda === "ARS" && valor >= 1000 ? 0 : 2}).format(valor) }
  catch { return (moneda || "") + " " + valor.toFixed(2) }
}
// Imagen que se oculta sola si no carga (algunas tiendas no tienen portada para todo).
const img = (src, cls) => src ? `<img class="${cls}" src="${esc(src)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display='none'">` : "";
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])) }

/* ---------- Dólar y ajustes ---------- */
async function cargarDolar() {
  try {
    const r = await fetch("/api/dolar"); dolar = await r.json();
    if (dolar.error) throw 0;
    const sel = $("#tipoDolar"); sel.innerHTML = "";
    const etiquetas = {oficial: "Tarjeta (oficial)", bolsa: "Tus dólares (MEP)"};
    for (const [k, nombre] of Object.entries(etiquetas)) if (dolar[k]) sel.add(new Option(nombre, k));
    const g = leer("tipoDolar2", "oficial");
    sel.value = dolar[g] ? g : "oficial";
  } catch { dolar = null }
  actualizarAjustes(); pintar();
}

function actualizarAjustes() {
  const ar = esAR();
  $("#imp").closest("label").style.display = ar ? "" : "none";
  $("#tipoDolar").closest("label").style.display = ar ? "" : "none";
  const imp = $("#imp").selectedOptions[0].textContent.replace(" (CABA / Bs. As.)", "");
  $("#resumenAjustes").textContent = ar
    ? `⚙️ ${PAISES.AR.nombre} · ${$("#imp").value === "0" ? "sin impuestos" : imp} · ${$("#tipoDolar").selectedOptions[0]?.textContent || "dólar oficial"}`
    : `⚙️ ${PAISES[$("#pais").value].nombre} · precios sin impuestos locales`;
  const d = dolar && dolar[$("#tipoDolar").value];
  if (!ar) $("#cot").textContent = "";
  else if (!dolar) $("#cot").textContent = "No se pudo obtener la cotización del dólar.";
  else if (d) {
    const f = d.actualizado ? new Date(d.actualizado).toLocaleString("es-AR", {dateStyle: "short", timeStyle: "short"}) : "";
    $("#cot").textContent = `1 US$ = ${fmt(d.venta, "ARS")} (dólar ${d.nombre}${f ? ", " + f : ""})` +
      "";
  }
  $("#pie").textContent = ar
    ? "Precios finales con tarjeta argentina: precio de la tienda + IVA 21% + Ingresos Brutos (los juegos no pagan la percepción de Ganancias desde abril de 2025). Confirmá siempre en la tienda antes de comprar."
    : "Precios de las APIs de cada tienda, sin impuestos locales. Confirmá en la tienda antes de comprar.";
}

/* ---------- Cálculo de precios ---------- */
function calcular(it) {
  const r = {valor: it.precio, original: it.precio_original, moneda: it.moneda, partes: []};
  if (!esAR()) return r;
  const d = dolar && dolar[$("#tipoDolar").value];
  let cambio;
  if (it.moneda === "ARS") cambio = 1;
  else if (it.moneda === "USD" && d) cambio = d.venta;
  else return r;
  const tasa = Number($("#imp").value) / 100;
  const base = it.precio * cambio;
  r.valor = base * (1 + tasa);
  r.original = it.precio_original ? it.precio_original * cambio * (1 + tasa) : null;
  r.moneda = "ARS";
  if (it.precio > 0) {
    r.partes.push(it.moneda === "USD" ? `${fmt(it.precio, "USD")} en la tienda = ${fmt(base, "ARS")}` : `Precio en la tienda: ${fmt(base, "ARS")}`);
    if (tasa > 0) r.partes.push(`IVA 21%: ${fmt(base * 0.21, "ARS")}`);
    if (tasa > 0.21) r.partes.push(`IIBB ${Math.round((tasa - 0.21) * 100)}%: ${fmt(base * (tasa - 0.21), "ARS")}`);
  }
  return r;
}

// El resultado de una tienda que corresponde al juego elegido (el juego base,
// no una edición ni un DLC), más las otras ediciones del mismo juego.
function elegir(t, clave) {
  const mismos = t.resultados.filter(r => r.clave === clave && r.tipo !== "extra");
  if (!mismos.length) return null;
  const hayPago = mismos.some(r => r.precio > 0);
  mismos.sort((a, b) => (a.tipo !== "juego") - (b.tipo !== "juego") || (hayPago ? (a.precio <= 0) - (b.precio <= 0) : 0) || a.precio - b.precio);
  return {it: mismos[0], otras: mismos.slice(1)};
}

// Todas las formas de comprar el juego, ordenadas por precio final.
function opciones(datos, clave) {
  const filas = [], faltan = [];
  for (const t of datos.tiendas) {
    const e = t.ok ? elegir(t, clave) : null;
    if (!e) { faltan.push(t); continue; }
    const c = calcular(e.it);
    filas.push({t, ...e, c, nombre: t.tienda});
    if (esAR() && t.acepta_global && e.it.moneda === "USD" && e.it.precio > 0 && dolar && dolar.cripto) {
      filas.push({t, it: e.it, otras: [], global: true, nombre: t.tienda.replace("Nintendo eShop", "eShop") + " · tarjeta Global",
                  c: {valor: e.it.precio * dolar.cripto.venta, moneda: "ARS", partes: [`${fmt(e.it.precio, "USD")} al dólar cripto (≈ cotización de billeteras Global)`, "Sin IVA ni IIBB"]}});
    }
  }
  const monedas = new Set(filas.map(f => f.c.moneda));
  if (monedas.size <= 1) filas.sort((a, b) => (a.c.valor <= 0) - (b.c.valor <= 0) || a.c.valor - b.c.valor);
  return {filas, faltan, comparables: monedas.size <= 1};
}

/* ---------- Mis juegos (se guarda en este celular) ---------- */
function biblioteca() { try { return JSON.parse(leer("biblioteca", "[]")) } catch { return [] } }
function guardarBiblioteca(b) { guardar("biblioteca", JSON.stringify(b)) }
const enBiblioteca = clave => biblioteca().some(j => j.clave === clave && j.pais === $("#pais").value);

function mejorOpcion(datos, clave) {
  const {filas, comparables} = opciones(datos, clave);
  const f = filas.find(f => f.c.valor > 0);
  return f && comparables ? {tienda: f.nombre, valor: f.c.valor, moneda: f.c.moneda} : null;
}

// La oferta más barata del juego entre todas las tiendas (o null si no está en oferta).
function mejorOferta(datos, clave) {
  const {filas} = opciones(datos, clave);
  const f = filas.find(f => f.c.valor > 0 && f.c.original);
  return f ? {tienda: f.nombre, valor: f.c.valor, moneda: f.c.moneda, pct: Math.round((1 - f.c.valor / f.c.original) * 100)} : null;
}

function actualizarEnBiblioteca(datos, clave) {
  const b = biblioteca(); const j = b.find(j => j.clave === clave && j.pais === datos.pais);
  if (!j) return;
  const m = mejorOpcion(datos, clave);
  if (m && (!j.mejor || Math.round(m.valor) !== Math.round(j.mejor.valor))) j.anterior = j.mejor;
  if (m) j.mejor = m;
  const g = (datos.juegos || []).find(g => g.clave === clave);
  if (g && g.mini) j.mini = g.mini;
  const o = mejorOferta(datos, clave);
  // "nueva" = la oferta no estaba la última vez que se revisó (para resaltarla)
  if (o) o.nueva = !j.oferta || j.oferta.tienda !== o.tienda || j.oferta.pct !== o.pct || !!j.oferta.nueva;
  j.oferta = o;
  j.fecha = Date.now();
  guardarBiblioteca(b);
}

function alternarGuardado(clave) {
  const g = ultimo.juegos.find(g => g.clave === clave);
  let b = biblioteca();
  if (enBiblioteca(clave)) b = b.filter(j => !(j.clave === clave && j.pais === $("#pais").value));
  else b.unshift({clave, titulo: g.titulo, mini: g.mini, pais: $("#pais").value, mejor: mejorOpcion(ultimo, clave), oferta: mejorOferta(ultimo, clave), fecha: Date.now()});
  guardarBiblioteca(b); pintar();
}

let vista = leer("vista", "mis"), ofertas = null, filtroPct = Number(leer("filtroPct", "50")), filtroTienda = leer("filtroTienda", "todas"), mostrarOfertas = 40;
let ordenOfertas = leer("ordenOfertas", "descuento"), filtroGenero = leer("filtroGenero", "todos");

function pintarInicio() {
  const tabs = `<div class="tabs"><button class="tab${vista === "mis" ? " activa" : ""}" data-vista="mis">⭐ Mis juegos${(n => n ? ` <span class="nuevo">🔥${n}</span>` : "")(biblioteca().filter(j => j.oferta).length)}</button>
    <button class="tab${vista === "ofertas" ? " activa" : ""}" data-vista="ofertas">🔥 Mega descuentos</button></div>`;
  return pintarAlertas() + tabs + (vista === "ofertas" ? pintarOfertas() : pintarBiblioteca());
}

async function cargarOfertas() {
  ofertas = {cargando: true}; pintar();
  try { ofertas = await (await fetch(`/api/descuentos?pais=${$("#pais").value}`)).json() }
  catch { ofertas = {error: true} }
  pintar();
}

function pintarOfertas() {
  if (!ofertas || ofertas.pais !== $("#pais").value && !ofertas.cargando && !ofertas.error) { setTimeout(cargarOfertas); return '<div class="cargando">Buscando ofertas…</div>' }
  if (ofertas.cargando) return '<div class="cargando">Buscando ofertas…</div>';
  if (ofertas.error) return '<div class="vacio error">No se pudieron cargar las ofertas. Probá de nuevo en un rato.</div>';
  const tiendas = [...new Set(ofertas.items.map(i => i.tienda))];
  // Géneros presentes, de más a menos ofertas
  const cuenta = {};
  for (const i of ofertas.items) for (const g of i.generos || []) cuenta[g] = (cuenta[g] || 0) + 1;
  const generos = Object.keys(cuenta).sort((a, b) => cuenta[b] - cuenta[a]);
  let lista = ofertas.items.filter(i => i.descuento >= filtroPct && (filtroTienda === "todas" || i.tienda === filtroTienda)
    && (filtroGenero === "todos" || (i.generos || []).includes(filtroGenero)));
  if (ordenOfertas === "populares") lista = [...lista].sort((a, b) => (a.pop ?? 1) - (b.pop ?? 1) || b.descuento - a.descuento);
  const chip = (attr, val, actual, texto) => `<button class="chip${val == actual ? " activa" : ""}" ${attr}="${esc(val)}">${esc(texto)}</button>`;
  return `<div class="filtros">${chip("data-orden", "descuento", ordenOfertas, "🔥 Mayor descuento")}${chip("data-orden", "populares", ordenOfertas, "⭐ Más vendidos")}</div>
    <div class="filtros">${[50, 70, 90].map(p => chip("data-pct", p, filtroPct, `-${p}% o más`)).join("")}</div>
    <div class="filtros">${chip("data-tienda", "todas", filtroTienda, "Todas las tiendas")}${tiendas.map(t => chip("data-tienda", t, filtroTienda, t.replace("Nintendo eShop", "eShop"))).join("")}</div>
    <div class="filtros desliza">${chip("data-genero", "todos", filtroGenero, "Todos los géneros")}${generos.map(g => chip("data-genero", g, filtroGenero, g)).join("")}</div>
    ${lista.length ? `<div class="ofertas">${lista.slice(0, mostrarOfertas).map(i => {
      const c = calcular(i);
      return `<div class="oferta" data-buscar="${esc(i.titulo.replace(/\s*\(Switch 2\)$/, ""))}">
        <span class="mini-wrap">${img(i.mini, "mini")}<span class="pct">-${i.descuento}%</span></span>
        <span class="info"><div class="nom">${esc(i.titulo)}</div><div class="sub">${esc(i.tienda)}${(i.generos || []).length ? " · " + esc([...new Set([filtroGenero, ...i.generos].filter(g => (i.generos || []).includes(g)))].slice(0, 2).join(", ")) : ""}</div></span>
        <span class="p">${fmt(c.valor, c.moneda)}${c.original ? `<small>${fmt(c.original, c.moneda)}</small>` : ""}</span></div>`;
    }).join("")}</div>
    ${lista.length > mostrarOfertas ? `<div class="acciones"><span></span><button class="sec" id="masOfertas">Ver más (${lista.length - mostrarOfertas})</button></div>` : ""}`
    : '<div class="vacio">No hay ofertas con ese filtro.</div>'}
    <div class="falta">Tocá un juego para comparar su precio en todas las tiendas. "Más vendidos": según los más vendidos de Steam, el ranking de popularidad de Nintendo y los más jugados de Xbox. Steam: los más vendidos en oferta. Xbox no publica su lista de ofertas: se revisan los juegos del catálogo de Game Pass (incluye los exclusivos) y los que están en oferta en otras tiendas.${ofertas.errores?.length ? " (" + esc(ofertas.errores.join(", ")) + ")" : ""}</div>`;
}

function pintarBiblioteca() {
  const b = biblioteca();
  if (!b.length) return `<div class="vacio">Buscá un juego para ver cuánto pagarías en Steam, Xbox y Nintendo.<br><br>Tocá ☆ en un juego para guardarlo en <b>Mis juegos</b> y seguir su precio.</div>`;
  return `<div class="bib" style="margin-top:12px">${b.map((j, i) => {
    let cambio = "";
    if (j.anterior && j.mejor && j.anterior.moneda === j.mejor.moneda) {
      const dif = j.mejor.valor - j.anterior.valor;
      cambio = `<small class="${dif < 0 ? "baja" : "sube"}">${dif < 0 ? "▼ bajó" : "▲ subió"} ${fmt(Math.abs(dif), j.mejor.moneda)}</small>`;
    }
    const f = new Date(j.fecha).toLocaleDateString("es-AR", {day: "numeric", month: "short"});
    return `<div class="bib-item">${img(j.mini, "mini")}
      <div class="info" data-buscar="${esc(j.titulo)}" data-clave="${esc(j.clave)}" data-pais="${esc(j.pais)}"><div class="nom">${esc(j.titulo)}</div>
        <div class="sub">${j.oferta ? `<span class="enoferta">🔥 -${j.oferta.pct}% en ${esc(j.oferta.tienda)}</span>` : j.mejor ? `Más barato en ${esc(j.mejor.tienda)}` : "Sin precio"} · ${f}${j.pais !== "AR" ? " · " + esc(PAISES[j.pais]?.nombre || j.pais) : ""}</div></div>
      <div class="p">${j.mejor ? fmt(j.mejor.valor, j.mejor.moneda) : "—"}${cambio}</div>
      <button class="x" data-borrar="${i}" aria-label="Quitar">✕</button></div>`;
  }).join("")}</div>
  <div class="acciones"><span class="sub" style="font-size:.78rem;color:var(--sub)">${revisando ? esc(revisando) : "Se revisan solos al abrir la app. Se guarda en este celular."}</span><button class="sec" id="actualizarTodo"${revisando ? " disabled" : ""}>↻ Actualizar</button></div>`;
}

/* ---------- Alertas de ofertas de Mis juegos ---------- */
let revisando = "";

async function revisarBiblioteca() {
  const b = biblioteca();
  if (!b.length || revisando) return;
  for (let i = 0; i < b.length; i++) {
    revisando = `Revisando precios ${i + 1}/${b.length}…`; if (!ultimo) pintar();
    try {
      const r = await fetch(`/api/buscar?q=${encodeURIComponent(b[i].titulo)}&pais=${b[i].pais}`);
      actualizarEnBiblioteca(await r.json(), b[i].clave);
    } catch {}
  }
  revisando = ""; guardar("ultimaRevision", String(Date.now()));
  if (!ultimo) pintar();
}
async function actualizarTodo() { await revisarBiblioteca() }

// Al abrir la app (o volver a ella), revisar Mis juegos si pasó más de media hora.
function revisarSiHaceFalta() {
  if (Date.now() - Number(leer("ultimaRevision", "0")) > 30 * 60 * 1000) revisarBiblioteca();
}
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") revisarSiHaceFalta() });

function pintarAlertas() {
  const enOferta = biblioteca().filter(j => j.oferta && j.pais === $("#pais").value);
  if (!enOferta.length) return "";
  return `<div class="alerta"><b>🔥 ${enOferta.length === 1 ? "Un juego de tu lista está" : enOferta.length + " juegos de tu lista están"} en oferta</b>${enOferta.map(j =>
    `<div class="alerta-item" data-buscar="${esc(j.titulo)}" data-clave="${esc(j.clave)}" data-pais="${esc(j.pais)}">${img(j.mini, "mini")}<span style="flex:1">${esc(j.titulo)}${j.oferta.nueva ? ' <span class="nuevo">NUEVO</span>' : ""}<span class="t">-${j.oferta.pct}% en ${esc(j.oferta.tienda)}</span></span><span class="p">${fmt(j.oferta.valor, j.oferta.moneda)}</span></div>`).join("")}</div>`;
}

// Cuando abrís un juego en oferta, deja de marcarse como "NUEVO".
function marcarVista(clave) {
  const b = biblioteca(); const j = b.find(j => j.clave === clave);
  if (j && j.oferta && j.oferta.nueva) { j.oferta.nueva = false; guardarBiblioteca(b) }
}

/* ---------- Pantalla ---------- */
// Detalle de un juego: lo que pagarías en cada tienda, dónde no está y sus DLC.
function pintarDetalle(g) {
  const {filas, faltan, comparables} = opciones(ultimo, g.clave);
  const mejor = comparables && filas.length > 1 && filas[0].c.valor > 0 ? filas[0] : null;
  let html = filas.length ? `<div class="lista">${filas.map(f => {
    const {it, c, otras} = f;
    const nota = it.titulo.toLowerCase() !== g.titulo.toLowerCase() ? it.titulo : "";
    const pct = c.original ? Math.round((1 - c.valor / c.original) * 100) : 0;
    const det = [`<p><a class="link" href="${esc(it.url)}" target="_blank" rel="noopener">Ver en la tienda ↗</a></p>`];
    if (c.original) det.push(`<p>En oferta: antes <span class="tachado">${fmt(c.original, c.moneda)}</span></p>`);
    if (c.partes.length) det.push(`<p>${c.partes.map(esc).join("<br>")}</p>`);
    if (otras.length) det.push(`<p>Otras ediciones en esta tienda:</p><ul>${otras.map(o => `<li><a href="${esc(o.url)}" target="_blank" rel="noopener">${esc(o.titulo)}</a>: ${fmt(calcular(o).valor, calcular(o).moneda)}</li>`).join("")}</ul>`);
    if (f.t.aviso) det.push(`<p class="warn">${esc(f.t.aviso)}</p>`);
    return `<details class="fila${f === mejor ? " mejor" : ""}"><summary>
      <span><span class="tienda">${esc(f.nombre)}</span>${nota ? `<span class="nota">${esc(nota)}</span>` : ""}</span>
      <span class="precio">${fmt(c.valor, c.moneda)}${f === mejor || pct ? `<small>${[f === mejor ? "Más barato" : "", pct ? `-${pct}%` : ""].filter(Boolean).join(" · ")}</small>` : ""}</span>
    </summary><div class="det">${det.join("")}</div></details>`;
  }).join("")}</div>` : "";
  if (faltan.length) html += `<div class="falta">No está en: ${faltan.map(t => t.buscar_url
    ? `<a href="${esc(t.buscar_url)}" target="_blank" rel="noopener">${esc(t.tienda)}</a>` : esc(t.tienda)).join(", ")}${faltan.some(t => !t.ok) ? " (alguna tienda no respondió)" : ""}</div>`;
  const extras = [];
  for (const t of ultimo.tiendas) for (const r of t.resultados)
    if (r.tipo === "extra" && r.clave.startsWith(g.clave)) extras.push({t, r, c: calcular(r)});
  if (extras.length) html += `<details class="extras"><summary>DLC y extras (${extras.length})</summary><ul>${extras.map(x =>
    `<li><span><a href="${esc(x.r.url)}" target="_blank" rel="noopener">${esc(x.r.titulo)}</a><span class="t">${esc(x.t.tienda)}</span></span><span>${fmt(x.c.valor, x.c.moneda)}</span></li>`).join("")}</ul></details>`;
  return html;
}

const botonGuardar = clave => `<button class="sec guardar" data-guardar="${esc(clave)}">${enBiblioteca(clave) ? "★ Guardado" : "☆ Guardar"}</button>`;
let abiertos = new Set();  // juegos desplegados en la lista de resultados

function pintar() {
  const res = $("#res");
  if (!ultimo) {
    res.innerHTML = pintarInicio();
    const g = res.querySelector(".desliza .chip.activa");  // que el género elegido quede a la vista
    if (g) g.parentElement.scrollLeft = g.offsetLeft - 16;
    return;
  }
  if (ultimo.cargando) { res.innerHTML = '<div class="cargando">Buscando en las tiendas…</div>'; return }
  const volver = '<button class="volver" id="volver">← Volver</button>';
  if (ultimo.error) { res.innerHTML = volver + `<div class="vacio error">${esc(ultimo.error)}</div>`; return }
  if (!ultimo.juegos.length) {
    res.innerHTML = volver + `<div class="vacio">No encontré "${esc(ultimo.consulta)}" en ninguna tienda.<br>Probá con menos palabras o con el nombre en inglés.</div>`;
    return;
  }
  const conImp = esAR() && $("#imp").value !== "0";
  let html = volver;
  if (ultimo.corregido) html += `<div class="corregido">Mostrando resultados para <b>${esc(ultimo.buscado)}</b></div>`;
  if (ultimo.juegos.length === 1) {
    const g = ultimo.juegos[0];
    html += `${img(g.imagen, "portada")}<div class="juego"><h3>${esc(g.titulo)}</h3>${botonGuardar(g.clave)}</div>
      <h2>Lo que pagarías${conImp ? " (con impuestos)" : ""}</h2>${pintarDetalle(g)}`;
  } else {
    // Varios juegos (una saga, nombres parecidos): todos a la vista con su precio más barato.
    if (!abiertos.size) abiertos.add(ultimo.juegos[0].clave);
    html += `<h2>Encontré ${ultimo.juegos.length} juegos${conImp ? " · precios con impuestos" : ""}</h2>` + ultimo.juegos.map(g => {
      const m = mejorOpcion(ultimo, g.clave), o = mejorOferta(ultimo, g.clave);
      const precio = m ? `<span class="jprecio">${fmt(m.valor, m.moneda)}<small>${o && o.tienda === m.tienda ? `-${o.pct}% · ` : ""}${esc(m.tienda)}</small></span>` : `<span class="jprecio"><small>sin precio</small></span>`;
      return `<details class="jcard" data-clave="${esc(g.clave)}"${abiertos.has(g.clave) ? " open" : ""}>
        <summary>${img(g.mini, "mini")}<span class="jtit">${esc(g.titulo)}${enBiblioteca(g.clave) ? " ★" : ""}</span>${precio}</summary>
        <div class="jbody">${img(g.imagen, "portada chica")}<div class="jacciones">${botonGuardar(g.clave)}</div>${pintarDetalle(g)}</div></details>`;
    }).join("");
  }
  res.innerHTML = html;
  for (const g of ultimo.juegos) if (enBiblioteca(g.clave)) { actualizarEnBiblioteca(ultimo, g.clave); marcarVista(g.clave) }
}

/* ---------- Eventos ---------- */
function irAlInicio() { $("#q").value = ""; ultimo = null; pintar(); window.scrollTo({top: 0}) }

async function buscar(q, desdeHistorial, abrir) {
  // Guardar en el historial, así el gesto/botón "atrás" del celular vuelve al inicio.
  if (!desdeHistorial) { try { history.pushState({q}, "", "#" + encodeURIComponent(q)) } catch {} }
  $("#q").value = q; $("#q").blur();
  ultimo = {cargando: true}; abiertos = new Set(abrir ? [abrir] : []); pintar();
  try {
    const r = await fetch(`/api/buscar?q=${encodeURIComponent(q)}&pais=${$("#pais").value}`);
    ultimo = await r.json();
  } catch { ultimo = {error: "No se pudo conectar con el servidor. Si recién lo abriste, esperá un minuto y probá de nuevo."} }
  pintar();
}
$("#res").addEventListener("toggle", e => {
  const c = e.target.closest && e.target.closest(".jcard");
  if (c && e.target === c) c.open ? abiertos.add(c.dataset.clave) : abiertos.delete(c.dataset.clave);
}, true);
$("#f").addEventListener("submit", e => { e.preventDefault(); const q = $("#q").value.trim(); if (q) buscar(q) });
$("#q").addEventListener("input", () => { if (!$("#q").value && ultimo && !ultimo.cargando) { ultimo = null; pintar() } });
$("#res").addEventListener("click", e => {
  const el = e.target.closest("[data-guardar],[data-buscar],[data-borrar],[data-vista],[data-pct],[data-tienda],[data-orden],[data-genero],#actualizarTodo,#masOfertas,#volver");
  if (!el) return;
  if (el.dataset.vista) { vista = el.dataset.vista; guardar("vista", vista); pintar() }
  else if (el.dataset.pct) { filtroPct = Number(el.dataset.pct); guardar("filtroPct", filtroPct); mostrarOfertas = 40; pintar() }
  else if (el.dataset.orden) { ordenOfertas = el.dataset.orden; guardar("ordenOfertas", ordenOfertas); mostrarOfertas = 40; pintar() }
  else if (el.dataset.genero) { filtroGenero = el.dataset.genero; guardar("filtroGenero", filtroGenero); mostrarOfertas = 40; pintar() }
  else if (el.dataset.tienda) { filtroTienda = el.dataset.tienda; guardar("filtroTienda", filtroTienda); mostrarOfertas = 40; pintar() }
  else if (el.id === "masOfertas") { mostrarOfertas += 40; pintar() }
  else if (el.dataset.buscar) { if (el.dataset.pais && el.dataset.pais !== $("#pais").value) { $("#pais").value = el.dataset.pais; actualizarAjustes() } buscar(el.dataset.buscar, false, el.dataset.clave) }
  else if (el.dataset.borrar) { const b = biblioteca(); b.splice(Number(el.dataset.borrar), 1); guardarBiblioteca(b); pintar() }
  else if (el.dataset.guardar) alternarGuardado(el.dataset.guardar);
  else if (el.id === "volver") { if (history.state && history.state.q) history.back(); else irAlInicio() }
  else if (el.id === "actualizarTodo") actualizarTodo();
});
$("#pais").addEventListener("change", () => { guardar("pais", $("#pais").value); actualizarAjustes(); const q = $("#q").value.trim(); if (q && ultimo) buscar(q); else pintar() });
$("#imp").addEventListener("change", () => { guardar("imp2", $("#imp").value); actualizarAjustes(); pintar() });
$("#tipoDolar").addEventListener("change", () => { guardar("tipoDolar2", $("#tipoDolar").value); actualizarAjustes(); pintar() });
$("h1").addEventListener("click", () => { if (history.state && history.state.q) history.back(); else irAlInicio() });
window.addEventListener("popstate", e => { if (e.state && e.state.q) buscar(e.state.q, true); else irAlInicio() });
actualizarAjustes(); pintar(); cargarDolar().then(revisarSiHaceFalta);
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
        if ruta == "/api/descuentos":
            pais = (args.get("pais") or ["AR"])[0].upper()
            pais = pais if pais in PAISES else "AR"
            return self._json(mega_descuentos(pais))
        if ruta == "/api/dolar":
            try:
                return self._json(cotizacion_dolar())
            except Exception as e:
                return self._json({"error": describir_error(e)}, 502)
        if ruta == "/manifest.json":
            return self._enviar(200, json.dumps(MANIFEST), "application/manifest+json", "public, max-age=86400")
        ic = icono(ruta)
        if ic:
            return self._enviar(200, ic[0], ic[1], "public, max-age=604800")
        if ruta == "/healthz":
            return self._enviar(200, "ok", "text/plain")
        self._enviar(404, "No encontrado", "text/plain; charset=utf-8")

    def log_message(self, formato, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), formato % args))


def precalentar_descuentos():
    """Arma la lista de ofertas apenas arranca el servidor y la renueva cada hora,
    para que la pestaña Mega descuentos abra rápido. También arma el vocabulario
    del corrector de errores de tipeo."""
    try:
        armar_vocabulario()
    except Exception as e:
        print(f"[vocabulario] {type(e).__name__}: {e}", file=sys.stderr)
    while True:
        try:
            with _cache_lock:
                _cache.pop(("descuentos", "AR", 50), None)
            mega_descuentos("AR")
        except Exception as e:
            print(f"[precalentar] {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(55 * 60)


def main():
    threading.Thread(target=precalentar_descuentos, daemon=True).start()
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
