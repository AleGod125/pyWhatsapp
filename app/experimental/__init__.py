"""Codigo que NO forma parte del producto. Apagado por defecto.

Nada de aqui se importa desde el arranque, el runtime, la sincronizacion, el
backfill ni la API principal. Se carga solo cuando una ruta protegida por un
flag lo pide, y hay una prueba que comprueba que sigue siendo asi.

QUE VIVE AQUI Y POR QUE
-----------------------
``web_seed_provider`` y ``history_recovery``
    Vinculan un SEGUNDO dispositivo a la cuenta del usuario (Baileys, Node)
    para intentar conseguir un ancla de historial en las conversaciones que
    llegaron sin ninguna. El producto no lo necesita y pedir dos codigos QR
    convierte una copia local en algo que parece pedir permisos de mas.

    Se conserva porque la infraestructura es correcta y reutilizable, no
    porque prometa mucho: las fuentes nativas se agotaron con medidas
    (bootstrap, blobs, PostgreSQL, alias PN/LID, app-state incremental y
    snapshot completo: cero claves) y la sesion auxiliar tampoco recibio
    historial al reconectar. ``no_seed`` fue el resultado habitual.

    Se enciende con ``WEB_BOOTSTRAP_ENABLED=true``.

``diagnostics_api``
    Medicion puntual sobre app-state. Observacion, no producto.

La ruta normal equivalente —revisar historiales pendientes SIN vincular nada—
es :mod:`app.services.pending_recheck`.
"""
