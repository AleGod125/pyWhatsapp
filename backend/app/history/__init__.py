"""Recuperacion de historial: anclas y su origen.

    seed_collector.py   observa, valida y anota anclas de todas las fuentes
    blob_scanner.py     extrae anclas de los blobs ya guardados, UNA vez

El motor que pide el historial —``BackfillService``— no vive aqui y no se
toca: funciona. Esto solo le entrega las referencias que necesita.
"""
