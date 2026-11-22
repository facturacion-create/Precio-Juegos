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

- Para Argentina, todo se muestra **en pesos y con impuestos**, con el desglose: precio de la tienda + IVA + Ingresos Brutos.
- Desde abril de 2025 los videojuegos digitales **no pagan la percepción del 30%** (RG ARCA 5677). Con tarjeta argentina se paga **IVA 21% + Ingresos Brutos** de tu provincia (2% en CABA y Buenos Aires). Arriba podés cambiar el porcentaje o ver los precios sin impuestos.
- **Steam** y la **eShop de EE.UU.** cobran en dólares: se pasan a pesos con el **dólar oficial**, que es el que usa la tarjeta. Si pagás con tus propios dólares, elegí **MEP**: el IVA y el IIBB se pagan igual.
- **Tarjeta Global** (emitida afuera, como AstroPay, Lemon Global, etc.): Steam, Xbox y la eShop argentina **no la aceptan**. La **eShop de EE.UU.** sí, y con ella no se paga IVA ni IIBB. La app muestra esa opción aparte, aproximando la cotización de la billetera con el dólar cripto.
- Fuente de cómo se cobra cada tienda: [impuestito.org](https://impuestito.org/blog/impuestos-2026-en-steam-playstation-xbox-y-nintendo-argentina).
- **Xbox** y la **eShop de Argentina** cobran en pesos: se usa ese precio y se le suman los impuestos.
- Arriba hay un resumen con **todas las opciones ordenadas de más barata a más cara**. Abajo, cada tienda muestra también los otros resultados parecidos (otras ediciones, secuelas, etc.).
- Las tiendas no tienen una API oficial para esto. Si alguna cambia, esa tarjeta va a mostrar un error y las demás siguen funcionando.
