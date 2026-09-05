# Configurar Google OAuth y Drive

Estos pasos los tienes que hacer **tú** en Google Cloud. El código ya está
listo; solo faltan las credenciales.

---

## 1. Crear el proyecto

1. Entra en <https://console.cloud.google.com/>.
2. Arriba, selector de proyecto → **Nuevo proyecto**.
3. Nombre: `WhatsApp Backup` (o el que quieras). **Crear**.
4. Asegúrate de tenerlo seleccionado antes de seguir.

## 2. Habilitar la API de Drive

1. **APIs y servicios → Biblioteca**.
2. Busca **Google Drive API** → **Habilitar**.

Sin esto, el login funcionará y las llamadas a Drive fallarán con 403.

## 3. Pantalla de consentimiento

1. **APIs y servicios → Pantalla de consentimiento de OAuth**.
2. Tipo: **Externo** (salvo que uses Google Workspace y quieras **Interno**).
3. Rellena nombre de la app, correo de asistencia y correo de contacto.
4. En **Permisos**, añade:

   ```
   .../auth/userinfo.email
   .../auth/userinfo.profile
   openid
   .../auth/drive.file
   ```

5. En **Usuarios de prueba**, añade **tu propia cuenta de Gmail**.

> Mientras la app esté en **Prueba**, solo entran los usuarios de prueba y el
> refresh token caduca a los 7 días. Para uso personal es suficiente; si un
> día te dice "necesitas volver a conectar Google", es esto.

## 4. Crear el Client ID

1. **APIs y servicios → Credenciales → Crear credenciales → ID de cliente de
   OAuth**.
2. Tipo de aplicación: **Aplicación web**.
3. Nombre: `WhatsApp Backup backend`.
4. **Orígenes de JavaScript autorizados**:

   ```
   http://localhost:4200
   ```

5. **URI de redirección autorizados** — este es el que importa:

   ```
   http://localhost:5000/api/v1/auth/google/callback
   ```

   Tiene que coincidir **carácter por carácter** con `GOOGLE_REDIRECT_URI`
   del `.env`. Sin barra final.

   > **Usa `localhost`, nunca `127.0.0.1`.** Son distintos para Google *y*
   > para las cookies del navegador: una cookie `SameSite=Lax` puesta en
   > `127.0.0.1` no viaja en las peticiones que hace una página de
   > `localhost`. Ese desajuste es exactamente lo que hacía que, tras un
   > login con Google correcto, la aplicación volviera al formulario de
   > acceso.

6. **Crear**. Copia el **Client ID** y el **Client secret**.

## 5. Rellenar `.env`

```ini
GOOGLE_CLIENT_ID=1234567890-abcdefg.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-...
GOOGLE_REDIRECT_URI=http://localhost:5000/api/v1/auth/google/callback
```

Y genera la clave de cifrado:

```bash
py -c "from app.auth.crypto import generar_clave; print(generar_clave())"
```

```ini
APP_ENCRYPTION_KEY=<lo que imprimió>
```

`.env` está en `.gitignore`. **El client secret no puede acabar en git.**

## 6. Comprobar

```bash
py service.py
```

En el log: si falta algo, `/api/v1/auth/google/start` responde
`503 GOOGLE_NOT_CONFIGURED` con el motivo.

---

## Por qué `drive.file` y no `drive`

| Scope | Qué da |
|---|---|
| `drive.file` | Solo los archivos que **crea esta aplicación**. |
| `drive` | **Todo** el Drive del usuario: fotos, documentos, todo. |

Esta aplicación solo necesita administrar sus propias copias, así que pedir
`drive` sería pedir mucho más de lo que hace falta. Además:

- `drive.file` **no** es un scope sensible: no requiere la verificación de
  Google, que tarda semanas.
- Si estas credenciales se filtraran, el daño se limita a los archivos de la
  aplicación.

Si algún día hiciera falta leer archivos que el usuario subió por su cuenta,
sería otra decisión y con su propio motivo.

## Errores frecuentes

| Mensaje | Causa |
|---|---|
| `redirect_uri_mismatch` | El URI de Google Cloud no coincide *exactamente* con `GOOGLE_REDIRECT_URI`. Revisa `localhost` vs `127.0.0.1`, el puerto y la barra final. |
| `access_denied` | Tu cuenta no está en **Usuarios de prueba**. |
| Vuelve con `status=drive_denied` | Desmarcaste el permiso de Drive en la pantalla de Google. Vuelve a conectar y acéptalo. |
| `403` al usar Drive | Falta habilitar **Google Drive API** (paso 2). |
| "Vuelve a conectar Google" a los pocos días | App en modo **Prueba**: el refresh token caduca a los 7 días. Publícala o reconecta. |

## Qué se guarda, y cómo

| Dato | Dónde | Protección |
|---|---|---|
| `client_secret` | `.env` del backend | Nunca sale de ahí; jamás llega a Angular |
| `access_token` | `google_credentials` | Cifrado con `APP_ENCRYPTION_KEY` (Fernet) |
| `refresh_token` | `google_credentials` | Cifrado; **nunca** se devuelve al frontend |
| `state` / PKCE / `nonce` | Cookie de Flask firmada | De un solo uso |

El frontend solo llega a saber `google_connected` y `drive_authorized`.
Ningún token cruza esa frontera.
