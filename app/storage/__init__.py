"""Almacenamiento del contenido pesado del backup.

    encryption.py   AES-256-GCM: DEK por usuario, KEK del servidor
    interface.py    BackupStorage: el contrato, sin Google dentro
    segments.py     agrupar mensajes en segmentos JSONL comprimidos
    jobs.py         cola durable en PostgreSQL (patron outbox)
    worker.py       el que sube, reintenta y marca listo
    media.py        multimedia: hash, subida, cache local, rangos
    drive/          la implementacion contra Google Drive

POR QUE HAY UNA INTERFAZ
------------------------
Drive es la implementacion de hoy. Si manana hace falta S3, OneDrive o un
disco local, lo que cambia es una clase; el pipeline de sincronizacion no se
entera. Por eso ``routes.py`` no llama nunca a la API de Google.
"""
