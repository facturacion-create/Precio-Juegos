# Precios de Juegos

Compara el precio de un juego en **Steam**, **Xbox** y **Nintendo eShop**, con un botón para ver todo en pesos argentinos según la cotización del dólar del día (tarjeta, oficial o MEP).

Solo usa Python, sin instalar nada extra.

## Usarlo en tu compu

```
python precios_juegos.py
```

Después abrí http://localhost:8000 en el navegador.

## Publicarlo gratis en Render (para usarlo desde el celular)

1. Entrá a https://render.com y tocá **Get Started**. Elegí **GitHub** para crear la cuenta con tu usuario de GitHub. No pide tarjeta.
2. En el panel tocá **+ New** → **Web Service**.
3. Si no aparece tu repositorio, tocá **Configure account** / **Connect GitHub** y dale permiso a Render para ver `precio-juegos`.
4. Elegí el repositorio `precio-juegos` y completá:
   - **Name**: `precios-juegos` (esto define tu link)
   - **Branch**: la rama donde está el código (`main` si ya se juntaron los cambios)
   - **Language / Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python precios_juegos.py`
   - **Instance Type**: **Free**
5. Tocá **Deploy Web Service** y esperá unos 2 minutos, hasta que diga **Live**.
6. Arriba aparece tu link, del estilo `https://precios-juegos.onrender.com`.

Cada vez que se sube un cambio a esa rama en GitHub, Render lo vuelve a publicar solo.

### Agregarlo a la pantalla de inicio

- **iPhone (Safari)**: abrí el link → botón Compartir (el cuadrado con la flecha) → **Agregar a inicio**.
- **Android (Chrome)**: abrí el link → menú ⋮ → **Agregar a la pantalla principal** (o **Instalar app**).

### Algo a tener en cuenta

En el plan gratis, si nadie usa la página por 15 minutos, Render la "duerme". La primera vez que la abras después de eso puede tardar **30 a 60 segundos** en cargar. Después anda rápido.

## Notas sobre los precios

- **Steam** en Argentina cobra en dólares (USD). Con "Ver todo en pesos" se convierte con el dólar que elijas. El **dólar tarjeta** ya incluye los impuestos.
- **Xbox** muestra los precios en pesos que publica Microsoft para Argentina. Al pagar con tarjeta se pueden sumar impuestos.
- **Nintendo**: Argentina no tiene eShop oficial, así que se muestra el precio de la **eShop de EE.UU.** (en dólares) y, aparte, lo que figura para Argentina (suele estar desactualizado).
- Las tiendas no tienen una API oficial para esto. Si alguna cambia, esa tarjeta va a mostrar un error y las demás siguen funcionando.
