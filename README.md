# 🎮 Precios de Juegos

App web para el celular que compara el precio de un juego en **Steam**, **Xbox** y **Nintendo eShop** (Argentina), con el **precio final en pesos con impuestos**, ofertas, avisos por Telegram y más.

**Link:** https://precio-juegos.onrender.com (se agrega a la pantalla de inicio del celular como una app).

---

## Qué hace

### Buscar un juego
- Escribís el nombre y muestra **lo que pagarías en cada tienda**, del más barato al más caro, con impuestos incluidos.
- **Autocompleta** mientras escribís y **corrige errores** ("cyberpunck" → Cyberpunk, "elden rign" → Elden Ring). Entiende abreviaturas (GTA, RE4, COD, BOTW, RDR2, TLOU…) y números romanos (GTA 5 = GTA V).
- Si la búsqueda coincide con varios juegos (una saga: "resident evil", "final fantasy"), muestra **todos en una lista** con su precio más barato. Tocás uno y se abre el detalle.
- En el detalle de cada tienda: link a la tienda, desglose de impuestos y **otras ediciones** (Deluxe, Switch 2, etc.). Los DLC están aparte.
- **Verde = en oferta** (precio de antes tachado y porcentaje). La etiqueta **"Más barato"** marca la tienda más conveniente.

### Mis juegos ⭐
- Tocá **☆ Guardar** en un juego para seguir su precio. La lista se guarda en el celular.
- Al abrir la app revisa solos tus juegos y muestra arriba un **aviso verde** si alguno está en oferta (con **NUEVO** si la oferta no estaba antes).

### Mega descuentos 🔥
- Ofertas de **30% o más** de Steam, Xbox y Nintendo eShop Argentina, con portada, precio final y fecha de fin.
- **Órdenes:** 🆕 Recientes (agrupadas Hoy / Ayer / Esta semana / Antes) · ⭐ Más vendidos · 🔥 Mayor descuento · ⏰ Terminan pronto · 💲 Más baratos.
- **Filtros:** eventos (Black Friday, ofertas de editor, rebajas de Steam… se detectan solos), porcentaje, tienda y género.
- Tocá una oferta para compararla en todas las tiendas.

### Avisos por Telegram 🔔
Un bot de Telegram (**@MELI_OFF_BOT**) te escribe:

| Qué | Cuándo |
|---|---|
| Un juego de **Mis juegos** entra en oferta | Al momento (se revisa cada hora) |
| Arranca un **evento de ofertas** nuevo | Al momento |
| **Ofertas nuevas** (las que no estaban en el aviso anterior) | 10:00, 16:00 y 22:00 |

Nunca repite una oferta ya avisada. Se activa desde la app: **Mis juegos → 🔔 Avisos por Telegram → Activar → Iniciar** en Telegram.

### Ajustes ⚙️
Botón arriba a la derecha: impuestos según tu provincia, cómo pagás los dólares (tarjeta = dólar oficial, o tus dólares = MEP) y país.

---

## Cómo se calculan los precios
- **Con tarjeta argentina** los juegos digitales pagan **IVA 21% + Ingresos Brutos** de tu provincia (2% en CABA y Buenos Aires). No pagan la percepción de Ganancias desde abril de 2025 (RG ARCA 5677). Fuente: [impuestito.org](https://impuestito.org/blog/impuestos-2026-en-steam-playstation-xbox-y-nintendo-argentina).
- **Steam** cobra en dólares → se pasan a pesos con el **dólar oficial** (o MEP si elegís pagar con tus dólares). Cotización de [dolarapi.com](https://dolarapi.com).
- **Xbox** y **Nintendo eShop Argentina** cobran en pesos → se les suman los impuestos.
- Los precios salen de las tiendas y pueden cambiar sin aviso: **confirmá siempre en la tienda antes de comprar**.

---

## Dónde está cada cosa (todo gratis)

| Pieza | Para qué | Dónde se configura |
|---|---|---|
| **Render** (plan Free) | Hostea la app | render.com → servicio *Precio-Juegos* |
| **GitHub** | Código + datos de los avisos | Repositorio `facturacion-create/Precio-Juegos` |
| **cron-job.org** | Visita la app cada 10 min de 8 a 1 (hora argentina) para que Render no la duerma | cron-job.org → cronjob *Precios de Juegos* (`*/10 0-3,11-23 * * *` en UTC) |
| **Telegram** | Bot de avisos | @BotFather → @MELI_OFF_BOT |
| **GitHub Actions** | Revisión diaria (9:07) de que las tiendas funcionen; si algo falla, llega un mail | `.github/workflows/revisar-tiendas.yml` |

### Variables de entorno en Render (Environment)
| Variable | Qué es |
|---|---|
| `TELEGRAM_TOKEN` | Token del bot (de @BotFather), **completo** con los números y los dos puntos |
| `GITHUB_TOKEN_ALERTAS` | Token *fine-grained* de GitHub, solo para el repo *Precio-Juegos*, permiso **Contents: Read and write** |

Nunca van en el código (el repositorio es público).

### Ramas de GitHub
- **`main`**: el código de la app (Render publica desde acá; cada cambio se publica solo).
- **`datos-alertas`**: datos que la app guarda sola (`alertas.json`: tu lista, el chat de Telegram y lo ya avisado; `primera_vez.json`: cuándo apareció cada oferta). **No tocar.**

### Límites del plan gratis
- **Render:** 750 horas por mes. Con la app despierta de 8 a 1 usa ~535 horas. Si se pasara, Render la pausa hasta el mes siguiente (no cobra: no hay tarjeta cargada). Si creás otra app en la misma cuenta, las horas se comparten.
- De 1 a 8 la app duerme: si entrás en ese horario tarda ~50 segundos en abrir.

---

## Si algo falla
- **La app tarda ~50 segundos en abrir:** Render la durmió. Revisá en cron-job.org → *Historial* que las visitas digan "Éxito".
- **Llega un mail de GitHub "Revisar que las tiendas funcionen" falló:** alguna tienda cambió algo. El detalle está en GitHub → *Actions*. Pasáselo a quien mantenga la app.
- **"Los avisos todavía no están configurados":** falta o está mal escrita una variable en Render (ver tabla de arriba). Después de cambiarlas: *Save, rebuild and deploy*.
- **El token de GitHub vence** (se creó con vencimiento): crear uno nuevo igual y reemplazarlo en Render.
- **El bot no responde:** revisar que `TELEGRAM_TOKEN` esté completo. Se puede probar abriendo `https://api.telegram.org/bot<TOKEN>/getMe` en el navegador (tiene que decir `"ok":true`).

---

## Para correrla en una compu
```
python precios_juegos.py
```
y abrir http://localhost:8000. Solo usa la biblioteca estándar de Python 3 (no hay que instalar nada).
