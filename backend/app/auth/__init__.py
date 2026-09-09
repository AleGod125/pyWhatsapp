"""Cuentas, sesiones web y Google OAuth.

Nada de este paquete decide quien eres a partir de datos que manda el
navegador: la identidad sale siempre de la cookie de sesion, que el servidor
emite y valida contra ``user_sessions``.

    passwords.py       Argon2id
    crypto.py          cifrado de tokens (Fernet) y tokens de sesion
    service.py         registro, login, sesiones
    google.py          cliente OAuth 2.0 (sin dependencias extra)
    google_service.py  persistencia y ciclo de vida de los tokens
    web.py             cookie, CSRF y guardas de ruta para Flask
"""
