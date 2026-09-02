"""whatsapp_backup - backup local de WhatsApp via dispositivo companion.

Capas (deliberadamente separadas, ver README):

* protocolo  -- pywhats + app/compat: obtener datos de WhatsApp.
* persistencia -- PostgreSQL (app/database.py, app/models.py): preservarlos.
* presentacion -- app/gui.py, app/terminal_ui.py: mostrarlos.
"""

__version__ = "0.1.0"
