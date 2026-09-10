import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { environment } from '../../../environments/environment';
import { AccountState } from '../services/account-state.service';

/** Cookie legible que el backend emite junto a la sesion. */
const CSRF_COOKIE = 'whatsapp_backup_csrf';

/**
 * Toda llamada a la API pasa por aqui.
 *
 * Dos cosas que no son opcionales:
 *
 * - `withCredentials`: la sesion viaja en una cookie, y sin esto el navegador
 *   no la envia entre origenes (`:4200` -> `:5000`). Todo respondería 401.
 * - `X-CSRF-Token` en las escrituras: se copia de una cookie legible. Otro
 *   sitio puede provocar la peticion, pero no puede LEER la cookie para
 *   rellenar la cabecera.
 * - `X-WhatsApp-Account`: de qué cuenta de WhatsApp habla esta petición.
 *
 * LA CABECERA DE CUENTA VA EN TODAS
 * ---------------------------------
 * Un usuario puede tener varios WhatsApp vinculados y son contextos
 * separados. Ponerla aquí —y no ruta por ruta— es lo que impide que una
 * petición nueva se olvide de decirlo y acabe leyendo la cuenta equivocada.
 *
 * El servidor NO se fía de ella: comprueba que esa cuenta sea de quien
 * pregunta y, si no lo es, usa la suya. La cabecera dice a qué se quiere
 * entrar, no a qué se puede.
 */
@Injectable({ providedIn: 'root' })
export class ApiClientService {
  private readonly http = inject(HttpClient);
  private readonly cuenta = inject(AccountState);
  readonly baseUrl = environment.apiBaseUrl;
  get<T>(
    path: string,
    params?: Record<string, string | number | boolean | undefined>,
  ): Observable<T> {
    let p = new HttpParams();
    for (const [key, value] of Object.entries(params ?? {}))
      if (value !== undefined) p = p.set(key, String(value));
    return this.http.get<T>(`${this.baseUrl}${path}`, {
      params: p,
      withCredentials: true,
      headers: this.cuentaHeaders(),
    });
  }
  post<T>(path: string, body: unknown = {}): Observable<T> {
    return this.http.post<T>(`${this.baseUrl}${path}`, body, {
      withCredentials: true,
      headers: this.escrituraHeaders(),
    });
  }

  put<T>(path: string, body: unknown = {}): Observable<T> {
    return this.http.put<T>(`${this.baseUrl}${path}`, body, {
      withCredentials: true,
      headers: this.escrituraHeaders(),
    });
  }

  delete<T>(path: string): Observable<T> {
    return this.http.delete<T>(`${this.baseUrl}${path}`, {
      withCredentials: true,
      headers: this.escrituraHeaders(),
    });
  }

  patch<T>(path: string, body: unknown = {}): Observable<T> {
    return this.http.patch<T>(`${this.baseUrl}${path}`, body, {
      withCredentials: true,
      headers: this.escrituraHeaders(),
    });
  }

  private csrfHeaders(): Record<string, string> {
    const token = leerCookie(CSRF_COOKIE);
    return token ? { 'X-CSRF-Token': token } : {};
  }

  /** De qué cuenta de WhatsApp habla esta petición, si ya se sabe. */
  private cuentaHeaders(): Record<string, string> {
    const id = this.cuenta.activaId();
    // Sin identificador NO se manda la cabecera vacía: el servidor
    // distingue "no me lo has dicho" —usa la activa— de "me has dicho una
    // que no existe", y una cadena vacía sería lo segundo.
    return id ? { 'X-WhatsApp-Account': id } : {};
  }

  private escrituraHeaders(): Record<string, string> {
    return { ...this.csrfHeaders(), ...this.cuentaHeaders() };
  }
  /**
   * Una URL absoluta, para lo que no pasa por `HttpClient`.
   *
   * Las imágenes (`<img src>`) y el canal de eventos (`EventSource`) no dejan
   * poner cabeceras, así que ahí la cuenta viaja como parámetro. Es el mismo
   * dato por el otro camino que acepta el servidor.
   */
  url(path: string, { conCuenta = false } = {}) {
    const base = `${this.baseUrl}${path}`;
    const id = this.cuenta.activaId();
    if (!conCuenta || !id) return base;
    return `${base}${base.includes('?') ? '&' : '?'}account_id=${encodeURIComponent(id)}`;
  }
}

function leerCookie(nombre: string): string | undefined {
  for (const trozo of document.cookie.split(';')) {
    const [clave, ...resto] = trozo.trim().split('=');
    if (clave === nombre) return decodeURIComponent(resto.join('='));
  }
  return undefined;
}
