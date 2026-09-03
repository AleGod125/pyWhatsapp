"""Visor de conversaciones: sidebar de chats + conversacion paginada.

Esta capa SOLO lee de PostgreSQL. No habla con WhatsApp ni dispara peticiones
de historial: recorrer una conversacion hacia atras es paginacion VISUAL sobre
lo que ya esta guardado. Traer datos nuevos es trabajo del extractor y va por
su lado.

Paginacion (secciones 1 a 4)
---------------------------
Al abrir un chat se pintan los ultimos 200 mensajes. Al acercarse el scroll al
principio se cargan otros 200 SOLOS, sin pulsar nada, conservando la posicion
visual. Cuando lo pintado iguala a lo almacenado se ensena "Inicio del
historial almacenado" y se dejan de lanzar consultas. El boton sigue existiendo
como respaldo, pero el uso normal no lo necesita.

Multimedia (secciones 8 a 12)
-----------------------------
Se distinguen dos situaciones que antes se veian igual: el archivo esta
descargado y se puede abrir, o WhatsApp ya no lo ofrece. Un mensaje NUNCA
desaparece porque su adjunto se haya perdido.

Memoria (secciones 38 y 39)
---------------------------
Las miniaturas se generan una vez y se cachean en disco. Al cambiar de chat se
sueltan las referencias de ``PhotoImage`` del anterior; si no, Tk las retiene
y la memoria solo sube.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.core.logging_setup import get_logger
from app.core.previews import bubble_placeholder, preview_for
from app.services.repository import ChatSummary
from app.gui.ui_theme import (
    ACCENT,
    BG,
    BORDER,
    BUBBLE_BORDER,
    BUBBLE_MINE,
    BUBBLE_THEIRS,
    CHAT_BG,
    DAY_BG,
    DIVIDER,
    FAINT,
    FG,
    HOVER,
    LINK,
    MUTED,
    SELECTED,
    SIDEBAR_BG,
    STATUS_BG,
    SYSTEM_BG,
    avatar_color,
    fonts,
    initials,
)

log = get_logger("GUI")

PAGE_SIZE = 200

# Tamanos maximos de las miniaturas en el hilo.
THUMBNAIL_MAX = (320, 320)
STICKER_MAX = (160, 160)

# Cache acotada de ``PhotoImage`` viva. El original ya no se decodifica en
# cada scroll: se lee la miniatura cacheada en disco.
THUMBNAIL_CACHE_SIZE = 120

# Umbral de prefetch: se carga la pagina anterior cuando el scroll entra en el
# primer 18% del contenido.
PREFETCH_THRESHOLD = 0.18

# Etiquetas del boton de paginacion (respaldo del scroll automatico).
LOAD_MORE_LABEL = "Cargar mensajes anteriores"
LOAD_BUSY_LABEL = "Cargando desde el PC..."
LOAD_DONE_LABEL = "Inicio del historial almacenado"

AUTO_PREFETCH = True

# Segundos minimos entre dos cargas automaticas. Impide la cascada
# scroll -> carga -> cambia el layout -> scroll -> carga...
PREFETCH_COOLDOWN = 0.45

# Cuantas miniaturas se materializan por tick del pintor perezoso.
THUMBS_PER_TICK = 4
THUMB_TICK_MS = 40
# Tick lento cuando no hay ninguna imagen cerca del viewport: el pintor sigue
# vivo por si el usuario se acerca, pero sin dar vueltas en vacio.
THUMB_IDLE_MS = 250
# Margen, en pixeles, alrededor del viewport que se considera "cerca".
VIEWPORT_MARGIN = 900

# Ancho maximo de una burbuja, en pixeles. Un mensaje corto ocupa lo que mide.
BUBBLE_MAX_WIDTH = 520

_MONTHS = (
    "ene", "feb", "mar", "abr", "may", "jun",
    "jul", "ago", "sep", "oct", "nov", "dic",
)

_WEEKDAYS = ("lun", "mar", "mie", "jue", "vie", "sab", "dom")


def _local(timestamp: int) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone()


def format_time(timestamp: int) -> str:
    return _local(timestamp).strftime("%H:%M")


def format_day(timestamp: int) -> str:
    moment = _local(timestamp)
    today = datetime.now().astimezone().date()
    delta = (today - moment.date()).days
    if delta == 0:
        return "Hoy"
    if delta == 1:
        return "Ayer"
    return f"{moment.day:02d} {_MONTHS[moment.month - 1]} {moment.year}"


def format_sidebar_time(timestamp: int | None) -> str:
    if not timestamp:
        return ""
    moment = _local(timestamp)
    today = datetime.now().astimezone().date()
    if moment.date() == today:
        return moment.strftime("%H:%M")
    if (today - moment.date()).days < 7:
        return _WEEKDAYS[moment.weekday()]
    return moment.strftime("%d/%m/%y")


def format_range(oldest: int | None, newest: int | None) -> str:
    """Rango historico para la cabecera. Vacio si no se sabe."""
    if not oldest or not newest:
        return ""
    inicio, fin = _local(oldest), _local(newest)
    if inicio.date() == fin.date():
        return format_day(oldest)
    return f"{format_day(oldest)} – {format_day(newest)}"


def format_size(size: int | None) -> str:
    """Tamano legible. Vacio cuando no se conoce, sin inventar un valor."""
    if not size:
        return ""
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    return f"{seconds // 60}:{seconds % 60:02d}"


def open_local_file(path: Path) -> bool:
    """Abre un archivo con la aplicacion del sistema. ``True`` si lo intento.

    En Windows se usa ``os.startfile``; en el resto el abridor habitual. Si el
    archivo no existe no se hace nada y se devuelve ``False``: puede estar
    pendiente de descarga, y eso no es un error que deba romper nada.
    """
    if not path.exists():
        log.warning("El archivo todavia no esta descargado: %s", path.name)
        return False
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception:  # noqa: BLE001 - no poder abrir no debe tumbar la GUI
        log.exception("No se pudo abrir %s", path.name)
        return False


def preview_text(text: str | None, message_type: str) -> str:
    """Vista previa de una fila del sidebar."""
    return preview_for(message_type, text)


# Estados de descarga que NO se van a resolver nunca.
TERMINAL_STATUSES = ("unavailable", "expired")

# Titulo y texto del aviso cuando el archivo ya no esta (seccion 12).
_UNAVAILABLE_TITLES = {
    "image": "🖼 Imagen no disponible",
    "video": "🎥 Video no disponible",
    "gif": "🎥 GIF no disponible",
    "audio": "🔊 Audio no disponible",
    "voice_note": "🎤 Nota de voz no disponible",
    "sticker": "Sticker no disponible",
    "document": "📄 Documento no disponible",
}

_MEDIA_TITLES = {
    "video": "▶ Video",
    "gif": "▶ GIF",
    "audio": "🔊 Audio",
    "voice_note": "🎤 Nota de voz",
    "document": "📄 Documento",
    "image": "📷 Imagen",
    "sticker": "Sticker",
}


class ScrollableFrame(tk.Frame):
    """Canvas con un frame interior y rueda del raton."""

    def __init__(self, master: tk.Misc, *, bg: str = BG) -> None:
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.scrollbar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = tk.Frame(self.canvas, bg=bg)

        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        for widget in (self.canvas, self.body):
            widget.bind("<Enter>", lambda _e: self._bind_wheel())
            widget.bind("<Leave>", lambda _e: self._unbind_wheel())

    def _on_body_configure(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        # El frame interior siempre ocupa el ancho del canvas para que el texto
        # se ajuste al redimensionar.
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event: tk.Event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def scroll_to_bottom(self) -> None:
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def content_height(self) -> int:
        return max(self.body.winfo_reqheight(), 1)

    def viewport(self) -> tuple[int, int]:
        """``(arriba, abajo)`` del contenido visible, en pixeles."""
        altura = self.content_height()
        try:
            first, last = self.canvas.yview()
        except (tk.TclError, ValueError):
            return 0, altura
        return int(first * altura), int(last * altura)

    def clear(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()


class ChatListPanel(tk.Frame):
    """Sidebar: buscador + lista de chats.

    Actualiza filas de una en una cuando puede (seccion 23): reconstruir la
    lista entera con cada mensaje que llega seria destruir y recrear miles de
    widgets para cambiar una linea de texto.
    """

    def __init__(
        self, master: tk.Misc, *, on_select: Callable[[ChatSummary], None]
    ) -> None:
        super().__init__(master, bg=SIDEBAR_BG, width=330)
        self.pack_propagate(False)

        self._on_select = on_select
        self._rows: dict[int, dict[str, Any]] = {}
        self._order: list[int] = []
        self._selected: int | None = None
        self._on_search: Callable[[str], None] | None = None
        self._f = fonts()

        search_box = tk.Frame(self, bg=SIDEBAR_BG)
        search_box.pack(fill="x", padx=12, pady=(12, 8))
        self.search_var = tk.StringVar()
        wrapper = tk.Frame(
            search_box, bg="#ffffff", highlightbackground=BORDER, highlightthickness=1
        )
        wrapper.pack(fill="x")
        tk.Label(
            wrapper, text="🔎", bg="#ffffff", fg=MUTED, font=self._f.meta
        ).pack(side="left", padx=(10, 4))
        self._entry = tk.Entry(
            wrapper,
            textvariable=self.search_var,
            font=self._f.meta,
            relief="flat",
            bg="#ffffff",
            fg=FG,
            insertbackground=FG,
        )
        self._entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 10))
        self.search_var.trace_add("write", self._on_search_changed)

        self._list = ScrollableFrame(self, bg=SIDEBAR_BG)
        self._list.pack(fill="both", expand=True)
        self._empty: tk.Widget | None = None

    def bind_search(self, callback: Callable[[str], None]) -> None:
        self._on_search = callback

    def _on_search_changed(self, *_args: Any) -> None:
        if self._on_search is not None:
            self._on_search(self.search_var.get())

    # -- Pintado -------------------------------------------------------------

    def set_chats(self, chats: list[ChatSummary]) -> None:
        """Reconstruye la lista completa. Se usa al abrir y al buscar."""
        self._list.clear()
        self._rows.clear()
        self._order = []
        self._empty = None

        if not chats:
            self._empty = tk.Label(
                self._list.body,
                text="Sin chats todavia",
                bg=SIDEBAR_BG,
                fg=MUTED,
                font=self._f.meta,
            )
            self._empty.pack(pady=24)
            return

        for chat in chats:
            self._rows[chat.id] = self._build_row(chat)
            self._order.append(chat.id)
        if self._selected in self._rows:
            self._paint(self._rows[self._selected], SELECTED)

    def update_chat(self, chat: ChatSummary) -> bool:
        """Refresca UNA fila sin tocar las demas (seccion 23).

        Devuelve ``False`` si el chat no esta en la lista, en cuyo caso el
        llamante tendra que recargarla entera: es un chat nuevo y hace falta
        crear su fila y reordenar.
        """
        fila = self._rows.get(chat.id)
        if fila is None:
            return False
        fila["chat"] = chat
        fila["name"].configure(text=self._row_title(chat))
        fila["time"].configure(text=format_sidebar_time(chat.last_message_timestamp))
        fila["preview"].configure(
            text=self._row_preview(chat),
            fg=MUTED if chat.message_count else FAINT,
        )
        fila["count"].configure(
            text=f"{chat.message_count}" if chat.message_count else ""
        )
        return True

    @staticmethod
    def _row_title(chat: ChatSummary) -> str:
        icono = "👥 " if chat.chat_type == "group" else ""
        return f"{icono}{chat.display_name}"

    @staticmethod
    def _row_preview(chat: ChatSummary) -> str:
        if not chat.message_count:
            return "sin mensajes guardados"
        # ``last_message`` ya viene formateado por app.previews: nunca dice
        # "[unknown]" para algo cuyo tipo conocemos.
        return (chat.last_message or "").replace("\n", " ")[:64] or "Mensaje"

    def _build_row(self, chat: ChatSummary) -> dict[str, Any]:
        row = tk.Frame(self._list.body, bg=SIDEBAR_BG, cursor="hand2")
        row.pack(fill="x")

        inner = tk.Frame(row, bg=SIDEBAR_BG)
        inner.pack(fill="x", padx=10, pady=7)

        # Avatar: inicial sobre un circulo de color estable por JID. Sin foto
        # real (no se descargan) pero suficiente para distinguir de un vistazo.
        avatar = tk.Label(
            inner,
            text=initials(chat.display_name),
            bg=avatar_color(chat.jid),
            fg="#ffffff",
            font=self._f.avatar,
            width=3,
            height=1,
        )
        avatar.pack(side="left", padx=(0, 10))

        texto = tk.Frame(inner, bg=SIDEBAR_BG)
        texto.pack(side="left", fill="x", expand=True)

        header = tk.Frame(texto, bg=SIDEBAR_BG)
        header.pack(fill="x")
        name = tk.Label(
            header,
            text=self._row_title(chat),
            bg=SIDEBAR_BG,
            fg=FG,
            font=self._f.name,
            anchor="w",
        )
        name.pack(side="left")
        time_label = tk.Label(
            header,
            text=format_sidebar_time(chat.last_message_timestamp),
            bg=SIDEBAR_BG,
            fg=FAINT,
            font=self._f.small,
        )
        time_label.pack(side="right")

        bottom = tk.Frame(texto, bg=SIDEBAR_BG)
        bottom.pack(fill="x")
        preview = tk.Label(
            bottom,
            text=self._row_preview(chat),
            bg=SIDEBAR_BG,
            fg=MUTED if chat.message_count else FAINT,
            font=self._f.meta,
            anchor="w",
        )
        preview.pack(side="left", fill="x", expand=True)
        # El contador de guardados baja de rango: es informacion nuestra, no
        # del chat, y no debe competir con el mensaje (seccion 25).
        count = tk.Label(
            bottom,
            text=f"{chat.message_count}" if chat.message_count else "",
            bg=SIDEBAR_BG,
            fg=FAINT,
            font=self._f.small,
        )
        count.pack(side="right", padx=(6, 0))

        separator = tk.Frame(row, bg=DIVIDER, height=1)
        separator.pack(fill="x", padx=10)

        widgets = [row, inner, texto, header, bottom, name, preview, count, time_label]
        for widget in widgets:
            widget.bind("<Button-1>", lambda _e, c=chat: self._select(c))
        avatar.bind("<Button-1>", lambda _e, c=chat: self._select(c))

        fila = {
            "chat": chat,
            "row": row,
            "avatar": avatar,
            "name": name,
            "preview": preview,
            "count": count,
            "time": time_label,
            "tintables": widgets,
        }
        row.bind("<Enter>", lambda _e, f=fila: self._hover(f, True))
        row.bind("<Leave>", lambda _e, f=fila: self._hover(f, False))
        return fila

    def _hover(self, fila: dict[str, Any], entrando: bool) -> None:
        if fila["chat"].id == self._selected:
            return
        self._paint(fila, HOVER if entrando else SIDEBAR_BG)

    def _select(self, chat: ChatSummary) -> None:
        anterior = self._rows.get(self._selected) if self._selected is not None else None
        if anterior is not None:
            self._paint(anterior, SIDEBAR_BG)
        self._selected = chat.id
        fila = self._rows.get(chat.id)
        if fila is not None:
            self._paint(fila, SELECTED)
        self._on_select(chat)

    @staticmethod
    def _paint(fila: dict[str, Any], color: str) -> None:
        """Tine la fila. El avatar conserva su color propio."""
        for widget in fila["tintables"]:
            try:
                widget.configure(bg=color)
            except tk.TclError:  # pragma: no cover - widget ya destruido
                continue


class StatusBar(tk.Frame):
    """Barra inferior discreta con el estado del trabajo de fondo.

    Ensena lo que importa (conectado, historial, multimedia, backfill) sin
    convertir la ventana en un visor de logs (seccion 29).
    """

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, bg=STATUS_BG, height=26)
        self.pack_propagate(False)
        f = fonts()

        self._dot = tk.Label(self, text="●", bg=STATUS_BG, fg=FAINT, font=f.small)
        self._dot.pack(side="left", padx=(12, 4))
        self._connection = tk.Label(
            self, text="Iniciando", bg=STATUS_BG, fg=FG, font=f.small
        )
        self._connection.pack(side="left")
        self._detail = tk.Label(self, text="", bg=STATUS_BG, fg=MUTED, font=f.small)
        self._detail.pack(side="left", padx=(14, 0))

    def update_status(self, status: Any) -> None:
        conectado = bool(getattr(status, "connected", False))
        self._dot.configure(fg=ACCENT if conectado else FAINT)
        self._connection.configure(text=getattr(status, "connection", ""))

        partes = [f"Historial: {getattr(status, 'history', '?')}"]
        pendientes = int(getattr(status, "media_pending", 0) or 0)
        partes.append(
            f"Multimedia: {pendientes} pendientes" if pendientes else "Multimedia: al dia"
        )
        partes.append(f"Backfill: {getattr(status, 'backfill', '?')}")
        if (
            getattr(status, "history_done", False)
            and getattr(status, "backfill_done", False)
            and not pendientes
        ):
            partes.append("Sincronizacion completa")
        self._detail.configure(text="   ·   ".join(partes))

    def set_text(self, text: str, *, color: str = MUTED) -> None:
        self._detail.configure(text=text, fg=color)


class ConversationPanel(tk.Frame):
    """Panel derecho: cabecera + mensajes con paginacion automatica."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, bg=CHAT_BG)
        self._f = fonts()

        # -- Cabecera (seccion 25): avatar, nombre, total y rango historico --
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x")
        cabecera = tk.Frame(header, bg=BG)
        cabecera.pack(side="left", padx=16, pady=9, fill="x", expand=True)

        self._avatar = tk.Label(
            cabecera, text="", bg=BG, fg="#ffffff", font=self._f.avatar, width=3
        )
        self._avatar.pack(side="left", padx=(0, 12))

        textos = tk.Frame(cabecera, bg=BG)
        textos.pack(side="left", fill="x", expand=True)
        self._title = tk.Label(
            textos, text="", bg=BG, fg=FG, font=self._f.title, anchor="w"
        )
        self._title.pack(anchor="w")
        self._meta = tk.Label(
            textos, text="", bg=BG, fg=MUTED, font=self._f.meta, anchor="w"
        )
        self._meta.pack(anchor="w")
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # Boton explicito de paginacion. SOLO consulta PostgreSQL: nunca
        # dispara ON_DEMAND. Es un respaldo del scroll automatico.
        self._load_bar = tk.Frame(self, bg=CHAT_BG)
        self._load_bar.pack(fill="x")
        self._load_button = tk.Label(
            self._load_bar,
            text=LOAD_MORE_LABEL,
            bg="#e9ecef",
            fg=LINK,
            font=self._f.meta,
            cursor="hand2",
            pady=6,
        )
        self._load_button.pack(fill="x", padx=16, pady=(6, 0))
        self._load_button.bind("<Button-1>", lambda _e: self.load_previous_page())

        self._scroll = ScrollableFrame(self, bg=CHAT_BG)
        self._scroll.pack(fill="both", expand=True)

        # Estado de la paginacion
        self._chat: ChatSummary | None = None
        self._oldest: tuple[int, int] | None = None  # (timestamp, id)
        self._loading = False
        self._exhausted = False
        self._last_day: str | None = None
        self._generation = 0  # invalida cargas de un chat anterior

        self._loader: Callable[[int, int, int, int], list[Any]] | None = None
        self._names: dict[str, str] = {}
        self._media: dict[int, Any] = {}
        self._stats: Any = None
        self._rendered = 0
        # IDs ya pintados: evita duplicar burbujas si una pagina se solapa.
        self._rendered_ids: set[int] = set()
        self._media_root: Path | None = None
        self._photos: dict[str, Any] = {}
        self._last_prefetch_at = 0.0
        # Posicion del scroll en la ultima carga automatica. Ver la guarda 5b.
        self._last_prefetch_view: float | None = None
        self._start_banner: tk.Widget | None = None

        # Pintado perezoso de miniaturas (seccion 38).
        self._pending_thumbs: list[dict[str, Any]] = []
        self._thumb_job: Any = None

        self._scroll.canvas.bind("<Configure>", self._maybe_prefetch, add="+")
        self._scroll.scrollbar.configure(command=self._on_scrollbar)
        self._scroll.canvas.configure(yscrollcommand=self._on_yscroll)

        self.show_placeholder()

    # -- API ----------------------------------------------------------------

    def bind_loader(self, loader: Callable[[int, int, int, int], list[Any]]) -> None:
        """``loader(chat_id, before_timestamp, before_id, limit) -> filas``."""
        self._loader = loader

    def show_placeholder(self) -> None:
        self._scroll.clear()
        tk.Label(
            self._scroll.body,
            text="Selecciona un chat",
            bg=CHAT_BG,
            fg=MUTED,
            font=self._f.meta,
        ).pack(pady=40)

    def set_media_root(self, root: Path) -> None:
        self._media_root = root

    def _update_meta(self) -> None:
        """Cabecera honesta: lo ALMACENADO, y aparte lo que hay en pantalla.

        Poner solo "256 mensajes" refiriendose a los widgets pintados induce a
        creer que no hay mas.
        """
        stats = self._stats
        if stats is None:
            self._meta.configure(text="")
            return
        partes = [f"{stats.total:,} mensajes almacenados".replace(",", ".")]
        rango = format_range(stats.oldest_timestamp, stats.newest_timestamp)
        if rango:
            partes.append(rango)
        if self._rendered < stats.total:
            partes.append(f"mostrando {self._rendered}")
        self._meta.configure(text="   ·   ".join(partes))

    def open_chat(
        self,
        chat: ChatSummary,
        messages: list[Any],
        names: dict[str, str],
        media: dict[int, Any] | None = None,
        stats: Any = None,
    ) -> None:
        """Pinta los mensajes mas recientes del chat."""
        # Soltar el chat anterior ANTES de pintar el nuevo: si no, las
        # ``PhotoImage`` del anterior siguen referenciadas y la memoria solo
        # crece al ir saltando de conversacion (seccion 39).
        self._release_chat()

        self._media = media or {}
        self._rendered_ids = set()
        self._last_prefetch_at = time.monotonic()
        self._last_prefetch_view = None
        # Cambiar de chat invalida cualquier carga en vuelo del anterior.
        self._generation += 1
        self._chat = chat
        self._names = names
        self._exhausted = False
        self._loading = False
        self._last_day = None

        self._title.configure(text=chat.display_name)
        self._avatar.configure(
            text=initials(chat.display_name), bg=avatar_color(chat.jid)
        )
        self._stats = stats
        self._rendered = len(messages)
        self._update_meta()

        self._scroll.clear()
        if not messages:
            self._empty_chat_notice()
            self._oldest = None
            self._sync_button()
            return

        for message in messages:
            self._append(message)
        self._oldest = (messages[0].timestamp, messages[0].id)
        self._scroll.scroll_to_bottom()
        self._sync_button()
        self._schedule_thumbs()

    def _release_chat(self) -> None:
        """Suelta imagenes y trabajo pendiente del chat anterior."""
        if self._thumb_job is not None:
            try:
                self.after_cancel(self._thumb_job)
            except (tk.TclError, ValueError):  # pragma: no cover
                pass
            self._thumb_job = None
        self._pending_thumbs.clear()
        self._photos.clear()
        self._start_banner = None

    def _empty_chat_notice(self) -> None:
        holder = tk.Frame(self._scroll.body, bg=CHAT_BG)
        holder.pack(pady=48, padx=40)
        tk.Label(
            holder,
            text="Todavia no hay mensajes de este chat",
            bg=CHAT_BG,
            fg=FG,
            font=self._f.title,
        ).pack()
        tk.Label(
            holder,
            text="\n".join(
                (
                    "WhatsApp solo envia un adelanto del historial al vincular un",
                    "dispositivo, y para este chat no ha entregado ningun mensaje.",
                    "",
                    "Sin un mensaje con identificador real no hay desde donde",
                    "pedirle mas al telefono: no hay ancla que enviarle.",
                    "",
                    "El chat existe y esta guardado; faltan sus mensajes.",
                )
            ),
            bg=CHAT_BG,
            fg=MUTED,
            font=self._f.meta,
            justify="center",
        ).pack(pady=(12, 0))

    def append_live(self, message: Any, media: Any = None) -> None:
        """Anade UN mensaje al final sin repintar la conversacion (seccion 34).

        Si el usuario esta leyendo mensajes antiguos no se le arrastra al
        final: solo se sigue el hilo cuando ya estaba abajo del todo.
        """
        if self._chat is None or message.id in self._rendered_ids:
            return
        try:
            _first, last = self._scroll.canvas.yview()
        except (tk.TclError, ValueError):
            last = 1.0
        estaba_abajo = last >= 0.999

        if media is not None:
            self._media[message.id] = media
        self._append(message)
        self._rendered += 1
        if self._stats is not None:
            self._stats = type(self._stats)(
                total=self._stats.total + 1,
                oldest_timestamp=self._stats.oldest_timestamp,
                newest_timestamp=message.timestamp,
            )
        self._update_meta()
        if estaba_abajo:
            self._scroll.scroll_to_bottom()
        self._schedule_thumbs()

    # -- Paginacion ---------------------------------------------------------

    def _on_scrollbar(self, *args: Any) -> None:
        self._scroll.canvas.yview(*args)
        self._maybe_prefetch()

    def _on_yscroll(self, first: str, last: str) -> None:
        self._scroll.scrollbar.set(first, last)
        self._maybe_prefetch()
        self._schedule_thumbs()

    def _maybe_prefetch(self, *_args: Any) -> None:
        """Carga automatica al acercarse al principio.

        Seis guardas, todas necesarias. La que causo el desastre anterior es
        la cuarta: cuando el contenido NO desborda el viewport, ``yview()``
        devuelve ``(0.0, 1.0)`` y "estoy cerca del principio" se cumple
        siempre, asi que cada carga provocaba otra y la conversacion entera
        (452 mensajes) entraba de golpe.

        Usa exactamente la misma funcion que el boton: una sola
        implementacion de "traer la pagina anterior".
        """
        if not AUTO_PREFETCH:
            return
        # 1) No hay chat, o ya no queda nada local que traer.
        if self._chat is None or self._oldest is None or self._loader is None:
            return
        if self._exhausted:
            return
        # 2) Ya hay una carga en curso: una pagina por disparo.
        if self._loading:
            return
        # 3) Anti-cascada: cooldown desde la ultima carga.
        now = time.monotonic()
        if now - self._last_prefetch_at < PREFETCH_COOLDOWN:
            return

        try:
            first, last = self._scroll.canvas.yview()
        except (tk.TclError, ValueError):
            return

        # 4) LA GUARDA CLAVE: si todo cabe en pantalla no hay scroll que
        #    interpretar y (0.0, 1.0) no significa "estoy arriba del todo".
        if last - first >= 1.0:
            return
        # 5) El usuario tiene que estar de verdad cerca del principio.
        if first > PREFETCH_THRESHOLD:
            return
        # 5b) Una pagina por MOVIMIENTO de scroll, no por evento (seccion 3).
        #     Tk emite varios eventos por el mismo gesto, y el cooldown solo
        #     es una ventana de tiempo: si pintar la pagina tarda mas que la
        #     ventana, el siguiente evento entra igual y se encadenan dos
        #     cargas. Exigir que la vista haya cambiado desde la ultima carga
        #     corta la cascada sin depender de cuanto tarde nada.
        if self._last_prefetch_view is not None and abs(
            first - self._last_prefetch_view
        ) < 1e-6:
            return
        # 6) Debe quedar algo en la base que traer (seccion 4).
        if self._stats is not None and self._rendered >= self._stats.total:
            self._exhausted = True
            self._mark_start_of_history()
            self._sync_button()
            return

        self._last_prefetch_at = now
        self._last_prefetch_view = first
        self.load_previous_page()
        # El cooldown se cuenta desde que la carga TERMINA, no desde que
        # empieza. Pintar 200 burbujas consume por si solo mas que la ventana,
        # asi que midiendolo desde el principio el siguiente evento entraba de
        # inmediato y se cargaban dos paginas de golpe.
        self._last_prefetch_at = time.monotonic()

    def load_previous_page(self) -> int:
        """Carga la pagina anterior DESDE POSTGRESQL. Devuelve cuantas trajo.

        Es la unica implementacion de "traer mensajes anteriores": la usan el
        boton y el scroll automatico. NUNCA contacta con WhatsApp.
        """
        if self._loading or self._exhausted:
            return 0
        if self._chat is None or self._oldest is None or self._loader is None:
            return 0

        self._set_button(LOAD_BUSY_LABEL, enabled=False)
        try:
            return self._load_older()
        finally:
            self._sync_button()
            self._schedule_thumbs()

    def _set_button(self, text: str, *, enabled: bool) -> None:
        self._load_button.configure(
            text=text,
            fg=LINK if enabled else MUTED,
            cursor="hand2" if enabled else "",
        )
        self._load_button.update_idletasks()

    def _sync_button(self) -> None:
        """Refleja si quedan mensajes anteriores EN LA BASE."""
        if self._chat is None:
            self._load_bar.pack_forget()
            return
        stats = self._stats
        quedan = (
            not self._exhausted
            and stats is not None
            and self._rendered < stats.total
        )
        if quedan:
            self._load_bar.pack(fill="x", before=self._scroll)
            self._set_button(LOAD_MORE_LABEL, enabled=True)
        elif self._exhausted or (stats is not None and self._rendered >= stats.total):
            # Estado terminal explicito: la barra se queda, deshabilitada,
            # diciendo que ya no hay mas. Ademas el mismo aviso aparece arriba
            # del hilo, donde el usuario acaba de llegar (seccion 4).
            self._load_bar.pack(fill="x", before=self._scroll)
            self._set_button(LOAD_DONE_LABEL, enabled=False)
        else:
            self._load_bar.pack_forget()

    def _load_older(self) -> int:
        assert self._chat is not None and self._oldest is not None
        assert self._loader is not None
        self._loading = True
        generation = self._generation
        timestamp, message_id = self._oldest

        older = self._loader(self._chat.id, timestamp, message_id, PAGE_SIZE)

        # Si el usuario cambio de chat mientras se consultaba, se descarta.
        if generation != self._generation:
            self._loading = False
            return 0
        # Filtro anti-duplicados, adicional al de PostgreSQL.
        older = [m for m in older if m.id not in self._rendered_ids]

        if not older:
            self._exhausted = True
            self._loading = False
            self._mark_start_of_history()
            return 0

        self._load_media_for(older)
        self._prepend(older)
        self._oldest = (older[0].timestamp, older[0].id)
        self._rendered += len(older)
        self._update_meta()
        self._loading = False
        if self._stats is not None and self._rendered >= self._stats.total:
            self._exhausted = True
            self._mark_start_of_history()
        return len(older)

    def _load_media_for(self, messages: list[Any]) -> None:
        """Trae los adjuntos de la pagina recien cargada.

        Solo de esos mensajes: nunca ``SELECT * FROM media_files``.
        """
        if self._media_loader is None:
            return
        try:
            nuevos = self._media_loader([m.id for m in messages])
        except Exception:  # noqa: BLE001 - sin adjuntos el texto sigue saliendo
            log.debug("No se pudieron cargar los adjuntos de la pagina")
            return
        self._media.update(nuevos)

    _media_loader: Callable[[list[int]], dict[int, Any]] | None = None

    def bind_media_loader(self, loader: Callable[[list[int]], dict[int, Any]]) -> None:
        self._media_loader = loader

    def _mark_start_of_history(self) -> None:
        """Aviso al llegar al principio de lo GUARDADO.

        Se dice "historial almacenado" a proposito: no sabemos si WhatsApp nos
        entrego toda la conversacion.
        """
        if self._start_banner is not None:
            return
        existing = self._scroll.body.winfo_children()
        if not existing:
            return
        banner = tk.Frame(self._scroll.body, bg=CHAT_BG)
        banner.pack(fill="x", pady=10)
        banner.pack_configure(before=existing[0])
        tk.Label(
            banner,
            text=LOAD_DONE_LABEL,
            bg=DAY_BG,
            fg=MUTED,
            font=self._f.small_bold,
            padx=12,
            pady=4,
        ).pack()
        self._start_banner = banner

    def _prepend(self, messages: list[Any]) -> None:
        """Inserta mensajes anteriores conservando la posicion visual.

        Se mide el alto del contenido antes y despues: la diferencia es
        exactamente cuanto hay que desplazar para que el mensaje que el
        usuario estaba mirando siga donde estaba (seccion 2). Sin esto el
        viewport salta al principio absoluto en cada carga y leer hacia atras
        es imposible.
        """
        canvas = self._scroll.canvas
        canvas.update_idletasks()
        height_before = self._scroll.content_height()
        offset_before = canvas.yview()[0] * height_before

        # Ancla fija: el primer widget que ya existia. Todo el bloque nuevo se
        # inserta ANTES de el, en orden, asi que la secuencia cronologica se
        # mantiene sin depender de indices que se mueven al insertar.
        existing = self._scroll.body.winfo_children()
        anchor = existing[0] if existing else None
        # Posicion del ancla RESPECTO A LA PANTALLA antes de insertar. Es la
        # medida que hay que conservar: donde esta ese mensaje para el ojo del
        # usuario. Restaurar la posicion a partir del incremento de altura
        # parecia equivalente, pero depende de que ``winfo_reqheight`` ya
        # refleje los widgets nuevos, y no siempre lo hace; la geometria real
        # del ancla si es fiable despues de ``update_idletasks``.
        anchor_screen_y: int | None = None
        if anchor is not None:
            try:
                anchor_screen_y = anchor.winfo_y() - int(offset_before)
            except tk.TclError:  # pragma: no cover
                anchor_screen_y = None

        # Los separadores del bloque nuevo se recalculan desde cero.
        self._last_day = None
        for message in messages:
            self._append(message, anchor)

        # El primer mensaje que ya estaba visible conserva su separador, asi
        # que se restaura el dia del ultimo insertado para no duplicarlo.
        self._last_day = format_day(messages[-1].timestamp)

        canvas.update_idletasks()
        height_after = self._scroll.content_height()
        if height_after <= 0:
            return

        destino: float | None = None
        if anchor is not None and anchor_screen_y is not None:
            try:
                destino = (anchor.winfo_y() - anchor_screen_y) / height_after
            except tk.TclError:  # pragma: no cover
                destino = None
        if destino is None:
            # Respaldo: desplazar tanto como haya crecido el contenido.
            destino = (offset_before + height_after - height_before) / height_after

        canvas.yview_moveto(max(0.0, min(1.0, destino)))
        log.debug(
            "Prepend de %d mensajes (+%dpx)", len(messages), height_after - height_before
        )

    # -- Pintado ------------------------------------------------------------

    def _append(self, message: Any, anchor: Any = None) -> None:
        """Pinta un mensaje, y su separador de dia si cambia la fecha.

        ``anchor`` es el widget ANTE EL QUE insertar. Es un widget fijo, no un
        indice: la version anterior usaba la posicion N dentro de la lista de
        hijos, que crece con cada insercion, asi que los bloques anteriores
        acababan intercalados y las fechas salian desordenadas
        (11 ago, 10 ago, Inicio, 21 ago).
        """
        day = format_day(message.timestamp)
        if day != self._last_day:
            self._day_separator(day, anchor)
            self._last_day = day

        self._rendered_ids.add(message.id)
        if message.message_type == "system":
            self._system_card(message, anchor)
        else:
            self._bubble(message, anchor)

    def _day_separator(self, day: str, anchor: Any = None) -> None:
        holder = tk.Frame(self._scroll.body, bg=CHAT_BG)
        holder.pack(fill="x", pady=(10, 4))
        if anchor is not None:
            holder.pack_configure(before=anchor)
        tk.Label(
            holder, text=day, bg=DAY_BG, fg=MUTED, font=self._f.small_bold,
            padx=10, pady=3,
        ).pack()

    def _system_card(self, message: Any, anchor: Any = None) -> None:
        """Evento de sistema: tarjeta centrada y discreta (seccion 31).

        Una llamada perdida no es un mensaje del usuario y no debe verse como
        una burbuja verde suya.
        """
        from app.core.system_message import describe_system_message

        evento = describe_system_message(
            getattr(message, "raw_proto", None), getattr(message, "raw_metadata", None)
        )
        holder = tk.Frame(self._scroll.body, bg=CHAT_BG)
        holder.pack(fill="x", pady=3)
        if anchor is not None:
            holder.pack_configure(before=anchor)

        texto = f"{evento.display} · {format_time(message.timestamp)}"
        tk.Label(
            holder,
            text=texto,
            bg=SYSTEM_BG,
            fg=MUTED,
            font=self._f.small,
            padx=12,
            pady=4,
            wraplength=BUBBLE_MAX_WIDTH,
            justify="center",
        ).pack()

    def _bubble(self, message: Any, anchor: Any = None) -> None:
        mine = bool(message.from_me)
        holder = tk.Frame(self._scroll.body, bg=CHAT_BG)
        # Menos aire vertical: antes cada mensaje corto ocupaba un bloque
        # enorme y la conversacion parecia mas vacia de lo que esta
        # (seccion 26).
        holder.pack(fill="x", padx=16, pady=1)
        if anchor is not None:
            holder.pack_configure(before=anchor)

        fondo = BUBBLE_MINE if mine else BUBBLE_THEIRS
        bubble = tk.Frame(
            holder,
            bg=fondo,
            highlightbackground=BUBBLE_BORDER,
            highlightthickness=0 if mine else 1,
        )
        bubble.pack(anchor="e" if mine else "w")

        # En grupos se identifica al participante.
        if not mine and self._chat is not None and self._chat.chat_type == "group":
            sender = message.sender_jid or getattr(message, "sender_lid", None) or ""
            if sender:
                tk.Label(
                    bubble,
                    text=self._names.get(sender, sender.split("@")[0]),
                    bg=fondo,
                    fg=ACCENT,
                    font=self._f.small_bold,
                    anchor="w",
                ).pack(anchor="w", padx=9, pady=(5, 0))

        media = self._media.get(message.id)
        if media is not None:
            self._render_media(bubble, media)

        # El texto de un adjunto es su pie de foto: si no hay, no se pinta una
        # etiqueta redundante debajo de la propia imagen.
        if message.text:
            body = message.text
        elif media is None:
            body = bubble_placeholder(message.message_type)
        else:
            body = None

        if body:
            # El ancho se ajusta al contenido: un "ok" no ocupa media
            # pantalla, y un parrafo largo se envuelve a un ancho comodo.
            ancho = min(BUBBLE_MAX_WIDTH, max(60, self._f.body.measure(body) + 24))
            tk.Label(
                bubble,
                text=body,
                bg=fondo,
                fg=FG,
                font=self._f.body,
                justify="left",
                anchor="w",
                wraplength=ancho,
            ).pack(anchor="w", padx=9, pady=(5, 1))

        tk.Label(
            bubble,
            text=format_time(message.timestamp),
            bg=fondo,
            fg=FAINT,
            font=self._f.small,
        ).pack(anchor="e", padx=9, pady=(0, 4))

    # -- Multimedia ---------------------------------------------------------

    def _media_path(self, media: Any) -> Path | None:
        if not media.local_path or self._media_root is None:
            return None
        return self._media_root / media.local_path

    def _render_media(self, bubble: tk.Frame, media: Any) -> None:
        """Pinta el adjunto segun su tipo y su estado de descarga."""
        path = self._media_path(media)
        descargado = (
            media.download_status == "downloaded" and path is not None and path.exists()
        )

        if descargado and media.media_type in ("image", "sticker", "gif"):
            self._image_slot(bubble, media, path)  # type: ignore[arg-type]
            return
        self._media_card(bubble, media, path, descargado)

    def _image_slot(self, bubble: tk.Frame, media: Any, path: Path) -> None:
        """Reserva el hueco de una imagen y encola su miniatura.

        No se decodifica aqui: se apunta en la cola perezosa y se materializa
        cuando el widget esta cerca del viewport (seccion 38). Asi abrir un
        chat con 200 fotos no congela la interfaz.
        """
        caja = STICKER_MAX if media.media_type == "sticker" else THUMBNAIL_MAX
        ancho, alto = self._slot_size(media, caja)

        # El contenedor reserva el hueco EN PIXELES y no propaga el tamano de
        # su hijo. Sin esa reserva, cada miniatura que llega empuja el resto
        # del hilo y el scroll da saltos mientras se cargan.
        contenedor = tk.Frame(bubble, bg=bubble["bg"], width=ancho, height=alto)
        contenedor.pack_propagate(False)
        contenedor.pack(anchor="w", padx=9, pady=(7, 2))

        slot = tk.Label(contenedor, bg=bubble["bg"], cursor="hand2", text="")
        slot.pack(fill="both", expand=True)
        slot.bind("<Button-1>", lambda _e, p=path: open_local_file(p))

        self._pending_thumbs.append(
            {"widget": slot, "container": contenedor, "path": path, "box": caja}
        )

    @staticmethod
    def _slot_size(media: Any, box: tuple[int, int]) -> tuple[int, int]:
        """Tamano reservado, respetando la proporcion si se conoce."""
        ancho, alto = media.width or 0, media.height or 0
        if not ancho or not alto:
            return box[0] // 2, box[1] // 2
        escala = min(box[0] / ancho, box[1] / alto, 1.0)
        return max(48, int(ancho * escala)), max(48, int(alto * escala))

    def _schedule_thumbs(self, delay: int = THUMB_TICK_MS) -> None:
        if self._thumb_job is not None or not self._pending_thumbs:
            return
        self._thumb_job = self.after(delay, self._pump_thumbs)

    def _pump_thumbs(self) -> None:
        """Materializa unas pocas miniaturas por tick, las que se ven antes."""
        self._thumb_job = None
        if not self._pending_thumbs:
            return

        arriba, abajo = self._scroll.viewport()
        cerca_min, cerca_max = arriba - VIEWPORT_MARGIN, abajo + VIEWPORT_MARGIN

        hechos = 0
        restantes: list[dict[str, Any]] = []
        for entrada in self._pending_thumbs:
            widget = entrada["widget"]
            if not widget.winfo_exists():
                continue
            if hechos >= THUMBS_PER_TICK:
                restantes.append(entrada)
                continue
            y = self._offset_in_body(entrada["container"])
            # Lejos del viewport: se deja para cuando el scroll se acerque.
            # ``None`` significa que no se ha podido situar; entonces se pinta,
            # que es el fallo bueno: mas vale una miniatura de mas que una
            # imagen que no aparece nunca.
            if y is not None and not (cerca_min <= y <= cerca_max):
                restantes.append(entrada)
                continue
            if self._paint_thumb(entrada):
                hechos += 1

        self._pending_thumbs = restantes
        if not self._pending_thumbs:
            return
        # Siempre se vuelve a programar mientras quede algo. La version
        # anterior solo lo hacia si habia pintado algo, asi que en cuanto una
        # ronda dejaba todas las imagenes fuera del viewport el pintor se
        # paraba para siempre y las miniaturas no aparecian nunca. Cuando no
        # hay nada cerca se espera mas, para no girar en vacio.
        self._schedule_thumbs(THUMB_TICK_MS if hechos else THUMB_IDLE_MS)

    def _offset_in_body(self, widget: tk.Misc) -> int | None:
        """Posicion vertical del widget DENTRO del contenido desplazable.

        ``winfo_y()`` es relativa al padre inmediato, y el hueco de una imagen
        cuelga de su burbuja: devolvia siempre 7 px. Comparar eso con el
        viewport daba "esta lejisimos" para todas las imagenes y ninguna
        llegaba a pintarse. La coordenada de raiz si es absoluta, y restando la
        del contenedor se obtiene el desplazamiento real.
        """
        try:
            return widget.winfo_rooty() - self._scroll.body.winfo_rooty()
        except tk.TclError:  # pragma: no cover - widget destruido a medias
            return None

    def _paint_thumb(self, entrada: dict[str, Any]) -> bool:
        photo = self._thumbnail(entrada["path"], entrada["box"])
        if photo is None:
            return False
        widget = entrada["widget"]
        try:
            widget.configure(image=photo)
            widget.image = photo  # referencia viva: si no, Tk la recolecta
        except tk.TclError:  # pragma: no cover - widget destruido a medias
            return False
        return True

    def _thumbnail(self, path: Path, box: tuple[int, int]) -> Any:
        """Miniatura lista para Tk. ``None`` si la imagen no se puede leer.

        Dos niveles de cache: el archivo derivado en disco (sobrevive al
        reinicio) y el ``PhotoImage`` en memoria (evita releer al hacer
        scroll). El original NUNCA se decodifica a tamano completo.
        """
        if self._media_root is None:
            return None
        clave = f"{path}|{box[0]}x{box[1]}"
        vivo = self._photos.get(clave)
        if vivo is not None:
            return vivo

        from app.services.thumbnails import ensure_thumbnail

        miniatura = ensure_thumbnail(self._media_root, path, box)
        if miniatura is None:
            return None
        try:
            from PIL import Image, ImageTk

            with Image.open(miniatura) as imagen:
                photo = ImageTk.PhotoImage(imagen)
        except Exception:  # noqa: BLE001 - un archivo corrupto no rompe el chat
            log.debug("No se pudo cargar la miniatura de %s", path.name)
            return None

        # Cache acotada: se descarta la entrada mas antigua al desbordar.
        if len(self._photos) >= THUMBNAIL_CACHE_SIZE:
            self._photos.pop(next(iter(self._photos)))
        self._photos[clave] = photo
        return photo

    def _media_card(
        self, bubble: tk.Frame, media: Any, path: Path | None, downloaded: bool
    ) -> None:
        """Tarjeta para video, audio, documento o adjunto no disponible."""
        fondo = bubble["bg"]
        card = tk.Frame(bubble, bg=fondo)
        card.pack(anchor="w", padx=9, pady=(7, 2), fill="x")

        no_disponible = media.download_status in TERMINAL_STATUSES
        if no_disponible:
            titulo = _UNAVAILABLE_TITLES.get(
                media.media_type, f"{media.media_type} no disponible"
            )
        elif media.media_type == "document" and media.file_name:
            titulo = f"📄 {media.file_name}"
        else:
            titulo = _MEDIA_TITLES.get(media.media_type, f"[{media.media_type}]")

        tk.Label(
            card,
            text=titulo,
            bg=fondo,
            fg=FG,
            font=self._f.body_bold if downloaded else self._f.body,
            anchor="w",
            justify="left",
        ).pack(anchor="w")

        detalles = [
            format_duration(media.duration_seconds),
            format_size(media.file_size),
        ]
        linea = "  ·  ".join(d for d in detalles if d)
        if linea:
            tk.Label(
                card, text=linea, bg=fondo, fg=MUTED, font=self._f.small, anchor="w"
            ).pack(anchor="w")

        if no_disponible:
            # El mensaje NUNCA desaparece porque su adjunto ya no este: se
            # dice con claridad que paso (seccion 12).
            tk.Label(
                card,
                text="El archivo ya no esta disponible en WhatsApp.",
                bg=fondo,
                fg=MUTED,
                font=self._f.small,
                anchor="w",
                justify="left",
                wraplength=BUBBLE_MAX_WIDTH,
            ).pack(anchor="w", pady=(2, 0))
            return

        if downloaded and path is not None:
            accion = {
                "voice_note": "▶  Reproducir",
                "audio": "▶  Reproducir",
                "video": "▶  Reproducir",
                "gif": "▶  Reproducir",
            }.get(media.media_type, "Abrir")
            abrir = tk.Label(
                card, text=accion, bg=fondo, fg=LINK, font=self._f.small,
                cursor="hand2", anchor="w",
            )
            abrir.pack(anchor="w", pady=(3, 0))
            abrir.bind("<Button-1>", lambda _e, p=path: open_local_file(p))
            return

        estado = {
            "pending": "pendiente de descarga",
            "downloading": "descargando...",
            "failed": "no se pudo descargar; se reintentara",
        }.get(media.download_status, "")
        if estado:
            tk.Label(
                card, text=estado, bg=fondo, fg=MUTED, font=self._f.small, anchor="w"
            ).pack(anchor="w", pady=(2, 0))


class ChatViewer(tk.Frame):
    """Visor completo: sidebar + conversacion, contra PostgreSQL.

    Recibe una fabrica de sesiones en vez de una sesion abierta: cada consulta
    usa la suya y se cierra, de modo que la GUI no retiene conexiones del pool
    mientras el usuario mira un chat.
    """

    # Retardo del buscador. Consultar en cada pulsacion lanzaria una consulta
    # por letra (seccion 24).
    SEARCH_DEBOUNCE_MS = 250

    def __init__(
        self, master: tk.Misc, session_factory: Callable[[], Any],
        media_root: Path | None = None,
    ) -> None:
        super().__init__(master, bg=BG)
        self._session_factory = session_factory

        cuerpo = tk.Frame(self, bg=BG)
        cuerpo.pack(fill="both", expand=True)

        self.chats = ChatListPanel(cuerpo, on_select=self._open_chat)
        self.chats.pack(side="left", fill="y")
        self.chats.bind_search(self._on_search)

        tk.Frame(cuerpo, bg=BORDER, width=1).pack(side="left", fill="y")

        self.conversation = ConversationPanel(cuerpo)
        self.conversation.pack(side="left", fill="both", expand=True)
        self.conversation.bind_loader(self._load_older)
        self.conversation.bind_media_loader(self._load_media)
        self._current: ChatSummary | None = None
        if media_root is not None:
            self.conversation.set_media_root(media_root)

        self.status_bar = StatusBar(self)
        self.status_bar.pack(fill="x", side="bottom")

        self._search_job: Any = None
        self._search_term: str | None = None

    # -- Datos --------------------------------------------------------------

    def refresh_chats(self, search: str | None = None) -> int:
        """Recarga el sidebar entero. Devuelve cuantos chats hay."""
        from app.services import repository as repo

        session = self._session_factory()
        try:
            summaries = repo.list_chat_summaries(session, search=search)
        finally:
            session.close()
        self.chats.set_chats(summaries)
        log.info("Sidebar actualizado: %d chats", len(summaries))
        return len(summaries)

    def refresh_chat_row(self, chat_id: int | None) -> bool:
        """Actualiza SOLO la fila de un chat (seccion 23).

        Devuelve ``False`` si no se pudo (chat nuevo, o cambia el orden), y
        entonces el llamante recarga el sidebar completo.
        """
        if chat_id is None:
            return False
        from app.services import repository as repo

        session = self._session_factory()
        try:
            resumen = repo.chat_summary(session, chat_id)
        finally:
            session.close()
        if resumen is None:
            return False
        return self.chats.update_chat(resumen)

    def _on_search(self, term: str) -> None:
        """Busca con retardo: una consulta por pausa, no por tecla."""
        self._search_term = term.strip() or None
        if self._search_job is not None:
            try:
                self.after_cancel(self._search_job)
            except (tk.TclError, ValueError):  # pragma: no cover
                pass
        self._search_job = self.after(self.SEARCH_DEBOUNCE_MS, self._run_search)

    def _run_search(self) -> None:
        self._search_job = None
        self.refresh_chats(search=self._search_term)

    def _open_chat(self, chat: ChatSummary) -> None:
        from app.services import repository as repo

        self._current = chat
        session = self._session_factory()
        try:
            messages = repo.get_recent_messages(session, chat.id, limit=PAGE_SIZE)
            # Las cifras salen de PostgreSQL, no de cuantos widgets se pinten.
            stats = repo.get_chat_stats(session, chat.id)
            media = repo.media_for_messages(session, [m.id for m in messages])
            names = (
                repo.sender_names(
                    session,
                    [m.sender_jid for m in messages if m.sender_jid]
                    + [m.sender_lid for m in messages if getattr(m, "sender_lid", None)],
                )
                if chat.chat_type == "group"
                else {}
            )
        finally:
            session.close()
        self.conversation.open_chat(chat, messages, names, media, stats)
        log.info(
            "[CHAT] jid=%s | PostgreSQL=%d | pagina=%d | adjuntos=%d",
            chat.jid.split("@")[0][:6] + "***@" + chat.jid.partition("@")[2],
            stats.total,
            len(messages),
            len(media),
        )

    def append_new_message(self, chat_id: int | None) -> bool:
        """Anade al final el ultimo mensaje del chat abierto (seccion 34).

        Devuelve ``False`` si no aplica (otro chat, o ese mensaje ya estaba
        pintado), y entonces el llamante decide si repinta.

        Existe para no repintar la conversacion entera por cada mensaje que
        llega: un repintado devuelve el scroll al final, asi que a quien
        estuviera leyendo mensajes antiguos se le arrastraba abajo cada vez.
        """
        if chat_id is None or self._current is None or self._current.id != chat_id:
            return False

        from app.services import repository as repo

        session = self._session_factory()
        try:
            ultimos = repo.get_recent_messages(session, chat_id, limit=1)
            if not ultimos:
                return False
            mensaje = ultimos[0]
            adjuntos = repo.media_for_messages(session, [mensaje.id])
        finally:
            session.close()

        if mensaje.id in self.conversation._rendered_ids:
            return False
        self.conversation.append_live(mensaje, adjuntos.get(mensaje.id))
        return True

    def reload_if_open(self, chat_id: int | None) -> None:
        """Repinta solo si el chat afectado es el que se esta mirando.

        Repintar a ciegas cada vez que llega un mensaje tiraria al usuario al
        final de la conversacion aunque estuviera leyendo mensajes antiguos.
        """
        if chat_id is not None and self._current is not None and self._current.id == chat_id:
            self._open_chat(self._current)

    def reload_current_chat(self) -> None:
        """Repinta el chat abierto. Se usa al terminar descargas de multimedia."""
        if self._current is not None:
            self._open_chat(self._current)

    def update_status(self, status: Any) -> None:
        self.status_bar.update_status(status)

    def _load_older(
        self, chat_id: int, before_timestamp: int, before_id: int, limit: int
    ) -> list[Any]:
        """Pagina anterior. Consulta a PostgreSQL, nunca a WhatsApp."""
        from app.services import repository as repo

        session = self._session_factory()
        try:
            return repo.get_messages_before(
                session, chat_id, before_timestamp, before_id, limit
            )
        finally:
            session.close()

    def _load_media(self, message_ids: list[int]) -> dict[int, Any]:
        from app.services import repository as repo

        session = self._session_factory()
        try:
            return repo.media_for_messages(session, message_ids)
        finally:
            session.close()
