"""Visor de conversaciones: sidebar de chats + conversacion paginada.

Esta capa SOLO lee de PostgreSQL. No habla con WhatsApp ni dispara peticiones
de historial: recorrer una conversacion hacia atras es paginacion VISUAL sobre
lo que ya esta guardado. Traer datos nuevos es trabajo del extractor, que es
otra cosa y va por su lado (seccion 39).

Virtualizacion (seccion 38): al abrir un chat se pintan los ultimos ~200
mensajes. Al acercarse el scroll al principio se cargan otros ~200
automaticamente, sin boton, conservando la posicion visual del viewport.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import font as tkfont
from typing import Any, Callable

from app.logging_setup import get_logger
from app.repository import ChatSummary

log = get_logger("GUI")

# Paleta
BG = "#ffffff"
SIDEBAR_BG = "#f7f8fa"
FG = "#111b21"
MUTED = "#667781"
BORDER = "#e9edef"
BUBBLE_MINE = "#d9fdd3"
BUBBLE_THEIRS = "#f5f6f6"
SELECTED = "#e7f3ff"
ACCENT = "#25d366"

PAGE_SIZE = 200

# Tamanos maximos de la seccion 45 del brief.
THUMBNAIL_MAX = (320, 320)
STICKER_MAX = (160, 160)

# Cache acotada de thumbnails: decodificar la misma imagen en cada scroll
# seria caro, pero retenerlas todas se comeria la memoria en un chat largo.
THUMBNAIL_CACHE_SIZE = 120
# Umbral de prefetch: se carga la pagina anterior cuando el scroll entra en el
# primer 18% del contenido.
PREFETCH_THRESHOLD = 0.18

# Etiquetas del boton de paginacion.
LOAD_MORE_LABEL = "Cargar mensajes anteriores"
LOAD_BUSY_LABEL = "Cargando desde el PC..."
LOAD_DONE_LABEL = "Inicio del historial almacenado"

# Scroll automatico. Desactivado a proposito: el boton es el mecanismo fiable
# por ahora. Ver ConversationPanel._maybe_prefetch.
AUTO_PREFETCH = False

_MONTHS = (
    "ene", "feb", "mar", "abr", "may", "jun",
    "jul", "ago", "sep", "oct", "nov", "dic",
)

# Etiqueta para los tipos sin texto propio.
_TYPE_LABELS = {
    "image": "[imagen]",
    "video": "[video]",
    "gif": "[gif]",
    "audio": "[audio]",
    "voice_note": "[nota de voz]",
    "sticker": "[sticker]",
    "document": "[documento]",
    "location": "[ubicacion]",
    "contact": "[contacto]",
    "poll": "[encuesta]",
    "reaction": "[reaccion]",
    "protocol": "[mensaje de protocolo]",
    "system": "[mensaje de sistema]",
    "senderkey": "[clave de grupo]",
    "unknown": "[sin interpretar]",
}


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
        return ["lun", "mar", "mie", "jue", "vie", "sab", "dom"][moment.weekday()]
    return moment.strftime("%d/%m/%y")


def format_size(size: int | None) -> str:
    """Tamano legible. ``None`` cuando no se conoce, sin inventar un valor."""
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
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def open_local_file(path: Path) -> None:
    """Abre un archivo con la aplicacion del sistema.

    En Windows se usa ``os.startfile``; en el resto el abridor habitual. Si el
    archivo no existe no se hace nada: puede estar pendiente de descarga.
    """
    if not path.exists():
        log.warning("El archivo todavia no esta descargado: %s", path.name)
        return
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:  # noqa: BLE001 - no poder abrir no debe tumbar la GUI
        log.exception("No se pudo abrir %s", path.name)


def preview_text(text: str | None, message_type: str) -> str:
    if text:
        return text.replace("\n", " ")[:60]
    return _TYPE_LABELS.get(message_type, f"[{message_type}]")


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

    def clear(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()


class ChatListPanel(tk.Frame):
    """Sidebar: buscador + lista de chats."""

    def __init__(
        self, master: tk.Misc, *, on_select: Callable[[ChatSummary], None]
    ) -> None:
        super().__init__(master, bg=SIDEBAR_BG, width=320)
        self.pack_propagate(False)

        self._on_select = on_select
        self._rows: dict[int, tk.Frame] = {}
        self._selected: int | None = None
        self._on_search: Callable[[str], None] | None = None

        self._name_font = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self._preview_font = tkfont.Font(family="Segoe UI", size=9)
        self._time_font = tkfont.Font(family="Segoe UI", size=8)

        search_box = tk.Frame(self, bg=SIDEBAR_BG)
        search_box.pack(fill="x", padx=12, pady=(12, 8))
        self.search_var = tk.StringVar()
        wrapper = tk.Frame(
            search_box, bg="#ffffff", highlightbackground=BORDER, highlightthickness=1
        )
        wrapper.pack(fill="x")
        tk.Label(
            wrapper, text="Buscar", bg="#ffffff", fg=MUTED, font=self._preview_font
        ).pack(side="left", padx=(10, 4))
        self._entry = tk.Entry(
            wrapper,
            textvariable=self.search_var,
            font=self._preview_font,
            relief="flat",
            bg="#ffffff",
            fg=FG,
            insertbackground=FG,
        )
        self._entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 10))
        self.search_var.trace_add("write", self._on_search_changed)

        self._list = ScrollableFrame(self, bg=SIDEBAR_BG)
        self._list.pack(fill="both", expand=True)

    def bind_search(self, callback: Callable[[str], None]) -> None:
        self._on_search = callback

    def _on_search_changed(self, *_args: Any) -> None:
        if self._on_search is not None:
            self._on_search(self.search_var.get())

    def set_chats(self, chats: list[ChatSummary]) -> None:
        self._list.clear()
        self._rows.clear()

        if not chats:
            tk.Label(
                self._list.body,
                text="Sin chats todavia",
                bg=SIDEBAR_BG,
                fg=MUTED,
                font=self._preview_font,
            ).pack(pady=24)
            return

        for chat in chats:
            self._rows[chat.id] = self._build_row(chat)

    def _build_row(self, chat: ChatSummary) -> tk.Frame:
        row = tk.Frame(self._list.body, bg=SIDEBAR_BG, cursor="hand2")
        row.pack(fill="x")

        inner = tk.Frame(row, bg=SIDEBAR_BG)
        inner.pack(fill="x", padx=12, pady=8)

        header = tk.Frame(inner, bg=SIDEBAR_BG)
        header.pack(fill="x")

        icon = "#" if chat.chat_type == "group" else ""
        name = tk.Label(
            header,
            text=f"{icon} {chat.display_name}".strip(),
            bg=SIDEBAR_BG,
            fg=FG,
            font=self._name_font,
            anchor="w",
        )
        name.pack(side="left")
        time_label = tk.Label(
            header,
            text=format_sidebar_time(chat.last_message_timestamp),
            bg=SIDEBAR_BG,
            fg=MUTED,
            font=self._time_font,
        )
        time_label.pack(side="right")

        # Se distingue "sin mensajes guardados" de "aun no sincronizado": son
        # cosas distintas y confundirlas hace perder el tiempo al usuario.
        if chat.message_count:
            text = preview_text(chat.last_message, "text")
        else:
            text = "sin mensajes guardados"
        preview = tk.Label(
            inner, text=text, bg=SIDEBAR_BG,
            fg=MUTED if chat.message_count else "#b0b8bd",
            font=self._preview_font, anchor="w",
        )
        preview.pack(fill="x")

        count = tk.Label(
            inner, text=f"{chat.message_count} guardados" if chat.message_count else "",
            bg=SIDEBAR_BG, fg="#9aa5ab", font=self._time_font, anchor="w",
        )
        count.pack(fill="x")

        separator = tk.Frame(row, bg=BORDER, height=1)
        separator.pack(fill="x", padx=12)

        for widget in (row, inner, header, name, preview, count, time_label):
            widget.bind("<Button-1>", lambda _e, c=chat: self._select(c))
        return row

    def _select(self, chat: ChatSummary) -> None:
        if self._selected is not None and self._selected in self._rows:
            self._paint(self._rows[self._selected], SIDEBAR_BG)
        self._selected = chat.id
        if chat.id in self._rows:
            self._paint(self._rows[chat.id], SELECTED)
        self._on_select(chat)

    @staticmethod
    def _paint(row: tk.Frame, color: str) -> None:
        row.configure(bg=color)
        for child in row.winfo_children():
            if isinstance(child, tk.Frame) and child.winfo_height() > 2:
                child.configure(bg=color)
                for leaf in child.winfo_children():
                    leaf.configure(bg=color)
                    if isinstance(leaf, tk.Frame):
                        for deep in leaf.winfo_children():
                            deep.configure(bg=color)


class ConversationPanel(tk.Frame):
    """Panel derecho: cabecera + mensajes con paginacion automatica."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, bg=BG)

        self._title_font = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        self._meta_font = tkfont.Font(family="Segoe UI", size=9)
        self._text_font = tkfont.Font(family="Segoe UI", size=10)
        self._time_font = tkfont.Font(family="Segoe UI", size=8)
        self._day_font = tkfont.Font(family="Segoe UI", size=8, weight="bold")

        # Sin altura fija: con el escalado de Windows al 150% dos lineas de
        # texto no caben en 58px y el contador de mensajes salia cortado por
        # la mitad. Que la cabecera se dimensione por su contenido.
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x")
        inner = tk.Frame(header, bg=BG)
        inner.pack(side="left", padx=18, pady=10)
        self._title = tk.Label(inner, text="", bg=BG, fg=FG, font=self._title_font, anchor="w")
        self._title.pack(anchor="w")
        self._meta = tk.Label(inner, text="", bg=BG, fg=MUTED, font=self._meta_font, anchor="w")
        self._meta.pack(anchor="w")
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # Boton explicito de paginacion. SOLO consulta PostgreSQL: nunca
        # dispara ON_DEMAND. Traer datos nuevos de WhatsApp es trabajo del
        # backfill y va por su lado (seccion 21 del encargo).
        self._load_bar = tk.Frame(self, bg=BG)
        self._load_bar.pack(fill="x")
        self._load_button = tk.Label(
            self._load_bar,
            text=LOAD_MORE_LABEL,
            bg="#eef1f3",
            fg="#1d7bd6",
            font=self._meta_font,
            cursor="hand2",
            pady=7,
        )
        self._load_button.pack(fill="x", padx=18, pady=(8, 0))
        self._load_button.bind("<Button-1>", lambda _e: self.load_previous_page())

        self._scroll = ScrollableFrame(self, bg=BG)
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
        # IDs ya pintados: evita duplicar burbujas si una pagina se solapa
        # con otra (seccion 11 del brief).
        self._rendered_ids: set[int] = set()
        self._media_root: Path | None = None
        self._thumbnails: dict[tuple[str, tuple[int, int]], Any] = {}

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
            bg=BG,
            fg=MUTED,
            font=self._meta_font,
        ).pack(pady=40)

    def _update_meta(self) -> None:
        """Cabecera honesta: lo ALMACENADO, y aparte lo que hay en pantalla.

        Poner solo "256 mensajes" haciendo referencia a los widgets pintados
        induce a creer que no hay mas (seccion 17 del brief).
        """
        stats = self._stats
        if stats is None:
            self._meta.configure(text="")
            return
        partes = [f"{stats.total:,} mensajes almacenados".replace(",", ".")]
        if stats.oldest_timestamp:
            partes.append(f"desde {format_day(stats.oldest_timestamp)}")
        if self._rendered < stats.total:
            partes.append(f"mostrando {self._rendered}")
        self._meta.configure(text="  ·  ".join(partes))

    def set_media_root(self, root: Path) -> None:
        self._media_root = root

    def open_chat(
        self,
        chat: ChatSummary,
        messages: list[Any],
        names: dict[str, str],
        media: dict[int, Any] | None = None,
        stats: Any = None,
    ) -> None:
        """Pinta los mensajes mas recientes del chat."""
        self._media = media or {}
        self._rendered_ids = set()
        # Cambiar de chat invalida cualquier carga en vuelo del anterior.
        self._generation += 1
        self._chat = chat
        self._names = names
        self._exhausted = False
        self._loading = False
        self._last_day = None

        self._title.configure(text=chat.display_name)
        self._stats = stats
        self._rendered = len(messages)
        self._update_meta()

        self._scroll.clear()
        if not messages:
            holder = tk.Frame(self._scroll.body, bg=BG)
            holder.pack(pady=48, padx=40)
            tk.Label(
                holder, text="Todavia no hay mensajes de este chat",
                bg=BG, fg=FG, font=self._title_font,
            ).pack()
            tk.Label(
                holder,
                text="\n".join(
                    (
                        "WhatsApp solo envia un adelanto del historial al vincular",
                        "un dispositivo: en la sincronizacion inicial llegaron",
                        "mensajes de unas pocas conversaciones, no de todas.",
                        "",
                        "El resto hay que pedirlo al telefono mediante",
                        "HISTORY_SYNC_ON_DEMAND, que es la siguiente fase del",
                        "proyecto y todavia no esta implementada.",
                        "",
                        "El chat existe y esta guardado: faltan sus mensajes.",
                    )
                ),
                bg=BG, fg=MUTED, font=self._meta_font, justify="center",
            ).pack(pady=(12, 0))
            self._oldest = None
            return

        for message in messages:
            self._append(message)
        self._oldest = (messages[0].timestamp, messages[0].id)
        self._scroll.scroll_to_bottom()
        self._sync_button()

    # -- Paginacion ---------------------------------------------------------

    def _on_scrollbar(self, *args: Any) -> None:
        self._scroll.canvas.yview(*args)
        self._maybe_prefetch()

    def _on_yscroll(self, first: str, last: str) -> None:
        self._scroll.scrollbar.set(first, last)
        self._maybe_prefetch()

    def _maybe_prefetch(self, *_args: Any) -> None:
        """Carga automatica al acercarse al principio.

        DESACTIVADA por defecto (``AUTO_PREFETCH``). Motivo medido: cuando el
        contenido no desborda el viewport, ``yview()`` devuelve ``(0.0, 1.0)``
        y la condicion "estoy cerca del principio" se cumple SIEMPRE, asi que
        el prefetch se disparaba en cadena y cargaba la conversacion entera de
        golpe (452 mensajes en lugar de 200). Ese era el scroll inestable.

        La comprobacion de desbordamiento de abajo lo arregla, pero el boton
        es el mecanismo fiable mientras tanto (seccion 24 del encargo).
        Cuando se reactive usara esta MISMA funcion, sin logica duplicada.
        """
        if not AUTO_PREFETCH:
            return
        if self._loading or self._exhausted or self._chat is None or self._oldest is None:
            return
        if self._loader is None:
            return
        try:
            first, last = self._scroll.canvas.yview()
        except (tk.TclError, ValueError):
            return
        # Si todo cabe en pantalla no hay scroll que interpretar.
        if last - first >= 1.0:
            return
        if first > PREFETCH_THRESHOLD:
            return
        # Misma funcion que el boton: una sola implementacion.
        self.load_previous_page()

    def load_previous_page(self) -> int:
        """Carga la pagina anterior DESDE POSTGRESQL. Devuelve cuantas trajo.

        Es la unica implementacion de "traer mensajes anteriores": la usa el
        boton y la reutilizara el scroll automatico cuando se reactive. No
        hay logica duplicada, y NUNCA contacta con WhatsApp.
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

    def _set_button(self, text: str, *, enabled: bool) -> None:
        self._load_button.configure(
            text=text,
            fg="#1d7bd6" if enabled else MUTED,
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
            self._load_bar.pack(fill="x", before=self._scroll)
            self._set_button(LOAD_DONE_LABEL, enabled=False)
        else:
            self._load_bar.pack_forget()

    def _load_older(self) -> int:
        assert self._chat is not None and self._oldest is not None and self._loader is not None
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

        self._prepend(older)
        self._oldest = (older[0].timestamp, older[0].id)
        self._rendered += len(older)
        self._update_meta()
        self._loading = False
        return len(older)

    def _mark_start_of_history(self) -> None:
        """Aviso al llegar al principio de lo GUARDADO.

        Se dice "historial almacenado" a proposito: no sabemos si WhatsApp nos
        entrego toda la conversacion (seccion 18).
        """
        existing = self._scroll.body.winfo_children()
        if not existing:
            return
        banner = tk.Frame(self._scroll.body, bg=BG)
        banner.pack(fill="x", pady=10)
        banner.pack_configure(before=existing[0])
        tk.Label(
            banner, text="Inicio del historial almacenado",
            bg=BG, fg=MUTED, font=self._day_font,
        ).pack()

    def _prepend(self, messages: list[Any]) -> None:
        """Inserta mensajes anteriores conservando la posicion visual.

        Se mide el alto del contenido antes y despues: la diferencia es
        exactamente lo que hay que desplazar para que el mensaje que el
        usuario estaba mirando siga donde estaba.
        """
        canvas = self._scroll.canvas
        canvas.update_idletasks()
        height_before = self._scroll.body.winfo_reqheight()
        offset_before = canvas.yview()[0] * max(height_before, 1)

        # Ancla fija: el primer widget que ya existia. Todo el bloque nuevo se
        # inserta ANTES de el, en orden, asi que la secuencia cronologica se
        # mantiene sin depender de indices que se mueven.
        existing = self._scroll.body.winfo_children()
        anchor = existing[0] if existing else None

        # Los separadores del bloque nuevo se recalculan desde cero.
        self._last_day = None
        for message in messages:
            self._append(message, anchor)

        # El primer mensaje que ya estaba visible conserva su separador, asi
        # que se restaura el dia del ultimo insertado para no duplicarlo.
        self._last_day = format_day(messages[-1].timestamp)

        canvas.update_idletasks()
        height_after = self._scroll.body.winfo_reqheight()
        added = height_after - height_before
        if height_after > 0:
            canvas.yview_moveto((offset_before + added) / height_after)
        log.debug("Prepend de %d mensajes (+%dpx)", len(messages), added)

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
        self._bubble(message, anchor)

    def _day_separator(self, day: str, anchor: Any = None) -> None:
        holder = tk.Frame(self._scroll.body, bg=BG)
        holder.pack(fill="x", pady=8)
        if anchor is not None:
            holder.pack_configure(before=anchor)
        tk.Label(
            holder, text=day, bg="#eef1f3", fg=MUTED, font=self._day_font, padx=10, pady=3
        ).pack()

    def _bubble(self, message: Any, anchor: Any = None) -> None:
        self._rendered_ids.add(message.id)
        mine = bool(message.from_me)
        holder = tk.Frame(self._scroll.body, bg=BG)
        holder.pack(fill="x", padx=18, pady=2)
        if anchor is not None:
            holder.pack_configure(before=anchor)

        bubble = tk.Frame(holder, bg=BUBBLE_MINE if mine else BUBBLE_THEIRS)
        bubble.pack(anchor="e" if mine else "w")

        # En grupos se identifica al participante.
        if not mine and self._chat is not None and self._chat.chat_type == "group":
            sender = message.sender_jid or ""
            if sender:
                tk.Label(
                    bubble,
                    text=self._names.get(sender, sender.split("@")[0]),
                    bg=bubble["bg"],
                    fg=ACCENT,
                    font=self._time_font,
                    anchor="w",
                ).pack(anchor="w", padx=10, pady=(6, 0))

        media = self._media.get(message.id)
        if media is not None:
            self._render_media(bubble, media)

        # El texto de un adjunto es su pie de foto: si no hay, no se pinta una
        # etiqueta redundante debajo de la propia imagen.
        if message.text:
            body = message.text
        elif media is None:
            body = _TYPE_LABELS.get(message.message_type, f"[{message.message_type}]")
        else:
            body = None

        if body:
            tk.Label(
                bubble,
                text=body,
                bg=bubble["bg"],
                fg=FG,
                font=self._text_font,
                justify="left",
                anchor="w",
                wraplength=520,
            ).pack(anchor="w", padx=10, pady=(6, 2))

        tk.Label(
            bubble,
            text=format_time(message.timestamp),
            bg=bubble["bg"],
            fg=MUTED,
            font=self._time_font,
        ).pack(anchor="e", padx=10, pady=(0, 6))

    def _render_media(self, bubble: tk.Frame, media: Any) -> None:
        """Pinta el adjunto segun su tipo y su estado de descarga."""
        background = bubble["bg"]
        path = (
            self._media_root / media.local_path
            if media.local_path and self._media_root
            else None
        )
        downloaded = media.download_status == "downloaded" and path and path.exists()

        if downloaded and media.media_type in ("image", "sticker", "gif"):
            photo = self._thumbnail(
                path, STICKER_MAX if media.media_type == "sticker" else THUMBNAIL_MAX
            )
            if photo is not None:
                label = tk.Label(bubble, image=photo, bg=background, cursor="hand2")
                label.image = photo  # referencia viva: si no, Tk la recolecta
                label.pack(anchor="w", padx=10, pady=(8, 2))
                label.bind("<Button-1>", lambda _e, p=path: open_local_file(p))
                return

        # Tarjeta para lo que no se muestra en linea (o aun no esta en disco).
        self._media_card(bubble, media, path, downloaded)

    def _media_card(
        self, bubble: tk.Frame, media: Any, path: Path | None, downloaded: bool
    ) -> None:
        background = bubble["bg"]
        icons = {
            "video": "[Video]", "gif": "[GIF]", "audio": "[Audio]",
            "voice_note": "[Nota de voz]", "document": "[Documento]",
            "image": "[Imagen]", "sticker": "[Sticker]",
        }
        card = tk.Frame(bubble, bg=background)
        card.pack(anchor="w", padx=10, pady=(8, 2), fill="x")

        title = media.file_name or icons.get(media.media_type, f"[{media.media_type}]")
        tk.Label(
            card, text=title, bg=background, fg=FG, font=self._text_font, anchor="w",
        ).pack(anchor="w")

        detalles = [
            format_duration(media.duration_seconds),
            format_size(media.file_size),
        ]
        # El estado se dice tal cual es: "no disponible" no es lo mismo que
        # "fallo", y ninguno de los dos invalida el mensaje.
        estado = {
            "pending": "pendiente de descarga",
            "downloading": "descargando...",
            "failed": "no se pudo descargar",
            "unavailable": "ya no esta en el servidor",
            "expired": "caducado en el servidor",
        }.get(media.download_status if not downloaded else "", "")
        if estado:
            detalles.append(estado)

        linea = "  ".join(d for d in detalles if d)
        if linea:
            tk.Label(
                card, text=linea, bg=background, fg=MUTED, font=self._time_font, anchor="w",
            ).pack(anchor="w")

        if downloaded and path is not None:
            abrir = tk.Label(
                card, text="Abrir", bg=background, fg="#1d7bd6",
                font=self._time_font, cursor="hand2", anchor="w",
            )
            abrir.pack(anchor="w")
            abrir.bind("<Button-1>", lambda _e, p=path: open_local_file(p))

    def _thumbnail(self, path: Path, box: tuple[int, int]) -> Any:
        """Miniatura cacheada. Devuelve ``None`` si la imagen no se puede leer."""
        key = (str(path), box)
        cached = self._thumbnails.get(key)
        if cached is not None:
            return cached
        try:
            from PIL import Image, ImageTk

            with Image.open(path) as image:
                image = image.convert("RGBA") if image.mode == "P" else image.convert("RGB")
                image.thumbnail(box)
                photo = ImageTk.PhotoImage(image)
        except Exception:  # noqa: BLE001 - un archivo corrupto no rompe el chat
            log.debug("No se pudo generar la miniatura de %s", path.name)
            return None

        # Cache acotada: se descarta la entrada mas antigua al desbordar.
        if len(self._thumbnails) >= THUMBNAIL_CACHE_SIZE:
            self._thumbnails.pop(next(iter(self._thumbnails)))
        self._thumbnails[key] = photo
        return photo


class ChatViewer(tk.Frame):
    """Visor completo: sidebar + conversacion, contra PostgreSQL.

    Recibe una fabrica de sesiones en vez de una sesion abierta: cada consulta
    usa la suya y se cierra, de modo que la GUI no retiene conexiones del pool
    mientras el usuario mira un chat.
    """

    def __init__(
        self, master: tk.Misc, session_factory: Callable[[], Any],
        media_root: Path | None = None,
    ) -> None:
        super().__init__(master, bg=BG)
        self._session_factory = session_factory

        self.chats = ChatListPanel(self, on_select=self._open_chat)
        self.chats.pack(side="left", fill="y")
        self.chats.bind_search(self._on_search)

        tk.Frame(self, bg=BORDER, width=1).pack(side="left", fill="y")

        self.conversation = ConversationPanel(self)
        self.conversation.pack(side="left", fill="both", expand=True)
        self.conversation.bind_loader(self._load_older)
        self._current: ChatSummary | None = None
        if media_root is not None:
            self.conversation.set_media_root(media_root)

        self._status = tk.Label(
            self, text="", bg=BG, fg=MUTED,
            font=tkfont.Font(family="Segoe UI", size=8),
        )

    # -- Datos --------------------------------------------------------------

    def refresh_chats(self, search: str | None = None) -> int:
        """Recarga el sidebar. Devuelve cuantos chats hay."""
        from app import repository as repo

        session = self._session_factory()
        try:
            summaries = repo.list_chat_summaries(session, search=search)
        finally:
            session.close()
        self.chats.set_chats(summaries)
        log.info("Sidebar actualizado: %d chats", len(summaries))
        return len(summaries)

    def _on_search(self, term: str) -> None:
        self.refresh_chats(search=term or None)

    def _open_chat(self, chat: ChatSummary) -> None:
        from app import repository as repo

        self._current = chat
        session = self._session_factory()
        try:
            messages = repo.get_recent_messages(session, chat.id, limit=PAGE_SIZE)
            # Las cifras salen de PostgreSQL, no de cuantos widgets se pinten.
            stats = repo.get_chat_stats(session, chat.id)
            media = repo.media_for_messages(session, [m.id for m in messages])
            names = (
                repo.sender_names(session, [m.sender_jid for m in messages if m.sender_jid])
                if chat.chat_type == "group"
                else {}
            )
        finally:
            session.close()
        self.conversation.open_chat(chat, messages, names, media, stats)
        log.info(
            "[CHAT] jid=%s | PostgreSQL=%d | consulta devuelve=%d | renderizados=%d | adjuntos=%d",
            chat.jid.split("@")[0][:6] + "***@" + chat.jid.partition("@")[2],
            stats.total,
            len(messages),
            len(messages),
            len(media),
        )

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

    def _load_older(
        self, chat_id: int, before_timestamp: int, before_id: int, limit: int
    ) -> list[Any]:
        """Pagina anterior. Consulta a PostgreSQL, nunca a WhatsApp."""
        from app import repository as repo

        session = self._session_factory()
        try:
            return repo.get_messages_before(
                session, chat_id, before_timestamp, before_id, limit
            )
        finally:
            session.close()
