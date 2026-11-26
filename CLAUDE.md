# Notas para trabajar en este proyecto

App de comparación de precios de juegos (Steam / Xbox / Nintendo eShop, Argentina) para un usuario no programador que la usa desde el iPhone. Hablarle en **castellano rioplatense, simple**, sin jerga. El README.md es la guía para el usuario.

## Estructura
- **Todo está en `precios_juegos.py`** (~2.700 líneas): servidor `http.server` + HTML/CSS/JS embebido en la constante `HTML` (string raw). Solo biblioteca estándar (`requirements.txt` vacío).
- `static/`: íconos (icon.svg + PNG 32/180/192/512) servidos por `icono()`.
- `render.yaml`, `.python-version` (3.12): deploy en Render (plan Free, 0.1 CPU: todo es más lento que en el sandbox).
- `.github/workflows/revisar-tiendas.yml`: revisión diaria que llama a `/api/estado` (503 = falla → GitHub manda mail).

## Despliegue y ramas
- Render publica en cada push. Puede estar publicando desde `main` o desde `claude/game-price-comparison-script-bflwdl` (se le pidió al usuario cambiarlo a `main`): **subir siempre a las dos ramas** (`git push origin HEAD:main` y a la rama de trabajo).
- Rama `datos-alertas`: la escribe la app sola (`alertas.json`, `primera_vez.json`). No tocar ni hacer que Render publique desde ahí.
- Keep-alive: **cron-job.org** (configurado por el usuario, `*/10 0-3,11-23 * * *` UTC = 8 a 1 hora argentina). El cron de GitHub Actions no sirvió (corría ~1 vez por hora).
- Variables en Render: `TELEGRAM_TOKEN`, `GITHUB_TOKEN_ALERTAS` (fine-grained, Contents RW, solo este repo). Nunca pedir que las peguen en el chat.

## Fuentes de datos (no oficiales, pueden cambiar)
- **Steam:** `store.steampowered.com/api/storesearch` (búsqueda; **sacar " - " del término**, Steam lo toma como exclusión), `api.steampowered.com/IStoreBrowseService/GetItems` (tipo juego/DLC, precios, fin de oferta, tags; muchos ids en una llamada), `search/results?specials=1&filter=topsellers` (ofertas), `api/featuredcategories` + páginas `/sale/...` (`data-partnereventstore` → eventos), `search/suggest` (autocompletar), `tagdata/populartags/spanish`.
- **Xbox:** `displaycatalog.mp.microsoft.com` (productos/precios; ignorar availabilities con `Remediations` = Game Pass; `ProductKind == "Game"`), `productFamilies/autosuggest` (encuentra "Grand Theft Auto VI" solo con números romanos → `con_romanos()`), `storeedgefd.dsx.mp.microsoft.com/v9.0/search`, **xbox.com** `games/browse/DynamicChannel.<canal>?orderby=..&Genre=..` (`window.__PRELOADED_STATE__` → `core2.products.productSummaries`, 25 por página; precios, `eligibility` "None" = para todos, `endDateUtc`), página `promotions/sales/sales-and-specials` (detecta canales de eventos), `catalog.gamepass.com/sigls/v2` (catálogo Game Pass como respaldo).
- **Nintendo:** Algolia `u3b6gr4ua3-dsn.algolia.net` índice `store_game_en_us` (búsqueda; `dlcType` None = juego, "ROM Bundle" = edición, otro = DLC; `gameGenreLabels`, `popularityRank`, filtro `topLevelFilters:"Deals"`), `api.ec.nintendo.com/v1/price` (precio AR en ARS; `discount_price.start_datetime/end_datetime`).
- **Dólar:** `dolarapi.com/v1/dolares` (oficial, MEP).

## Lógica de búsqueda (lo más delicado)
- `buscar_todo()`: `expandir_consulta()` (alias GTA/RE/COD…, números en palabras) → busca en las 3 tiendas en paralelo (`BUSCADORES`, memoria de 30 min por tienda en `_seguro`) → si no hay buen resultado, `corregir()` (vocabulario de títulos; solo se queda con la corrección si mejora) → segunda vuelta: secuela siguiente ("gta" → GTA VI) y búsqueda por nombre exacto en las tiendas donde faltó un juego → `detectar_juegos()` agrupa por `clave()`.
- `clave(titulo)`: nombre base sin edición ni plataforma (`sin_edicion`, `_sacar_descriptores`, `PALABRAS_EDICION`). `normalizar()` quita ™ **antes** de NFKD y pasa romanos a números. Cambios acá afectan todo: probar siempre con la batería de abajo.
- `puntaje()` compara la consulta (y su `clave`) con la `clave` del resultado; extras (DLC) restan.
- Probado y funcionando: Hollow Knight, Silksong, Hades, Elden Ring, Hellraiser (Clive Barker's: Revival), Minecraft, Hogwarts Legacy, Baldur's Gate 3, Cyberpunck, elden rign, the witcher tres, gta / gta 6, resident evil, final fantasy (incluye Tactics), Metamorph re fantazio, zelda, Celeste.

## Ofertas y avisos
- `armar_ofertas()` arma en segundo plano (hilo `precalentar_descuentos`, cada ~55 min): Steam + Nintendo + Xbox (xbox.com + respaldo). `/api/descuentos` nunca bloquea: devuelve `{"cargando": true}` mientras arma; el front reintenta y muestra la última lista guardada en localStorage.
- `marcar_recientes()`: `desde` = inicio real (Nintendo) o primera vez vista (persistido en `primera_vez.json`).
- `revisar_alertas()` (cada hora): juegos de la lista en oferta + eventos nuevos (al momento) y "ofertas nuevas" en `HORARIOS_OFERTAS_NUEVAS` (10, 16, 22 AR). Guarda en GitHub solo si cambió algo. Vinculación de Telegram: `/api/alertas/vincular` → link `t.me/<bot>?start=<código>` → `/api/alertas/confirmar` lee `getUpdates`.

## Front (JS dentro de `HTML`)
- Un solo listener de clicks en `#res` con una **lista blanca de selectores** (`closest("...")`): al agregar botones nuevos, sumar su id/atributo ahí o no responden.
- Clases CSS: ojo con nombres repetidos (`.oferta` ya es la fila de Mega descuentos).
- Estado del usuario en `localStorage` (Mis juegos, filtros, llave de avisos); envolver en try/catch (`leer`/`guardar`).

## Cómo probar
- Local: `PORT=8765 python3 precios_juegos.py`. Avisos sin servicios reales: `ALERTAS_ARCHIVO=/ruta/alertas.json TELEGRAM_PRUEBA=1 TELEGRAM_TOKEN=x` (imprime los mensajes y simula Telegram).
- Antes de subir cambios de búsqueda: correr la batería de búsquedas de arriba y una muestra de ofertas de `/api/descuentos` buscándolas por título (todas deben encontrar su juego).
- Capturas con Playwright (iPhone 13) para revisar la interfaz en claro y oscuro.
- En producción: `curl https://precio-juegos.onrender.com/api/estado`.
