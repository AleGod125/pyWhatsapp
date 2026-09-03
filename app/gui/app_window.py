"""Interfaz Tkinter.

Vive SIEMPRE en el hilo principal. Los eventos del cliente de WhatsApp llegan
por una ``queue.Queue`` que se vacia con ``root.after``: ningun widget se toca
desde el hilo de asyncio.

Hay un unico ``root`` y un unico ``mainloop``. Cambiar de pantalla es cambiar
de frame, no abrir otra ventana.
"""

from __future__ import annotations

import queue
import sys
import tkinter as tk
from tkinter import font as tkfont
from typing import Any, Callable

from PIL import ImageTk

from app.core.logging_setup import get_logger
from app.core.qr_render import MIN_SIZE, PREFERRED_MAX_SIZE, render_qr

log = get_logger("GUI")

# Paleta sobria. El objetivo es que el QR resalte, no decorar.
BG = "#ffffff"
FG = "#111b21"
MUTED = "#667781"
ACCENT = "#25d366"
WARN = "#d97706"
ERROR = "#dc2626"
BORDER = "#e9edef"

POLL_INTERVAL_MS = 50


def enable_dpi_awareness() -> None:
    """Evita que Windows escale la ventana y difumine el QR.

    Solo actua en Windows. En Linux/macOS no hace nada, para no romper una
    ejecucion posterior en esas plataformas.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        # PROCESS_PER_MONITOR_DPI_AWARE = 2. Disponible desde Windows 8.1.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        log.debug("DPI awareness activado (per-monitor)")
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[name-defined]
            log.debug("DPI awareness activado (modo antiguo)")
        except Exception:  # noqa: BLE001
            log.debug("No se pudo activar DPI awareness; el QR podria verse escalado")


def center(window: tk.Misc, width: int, height: int) -> None:
    screen_w = window.winfo_screenwidth()
    screen_h = window.winfo_screenheight()
    x = max(0, (screen_w - width) // 2)
    y = max(0, (screen_h - height) // 3)
    window.geometry(f"{width}x{height}+{x}+{y}")


# Margen blanco extra alrededor del QR, ademas de la zona tranquila del propio
# codigo. Ayuda a que la camara aisle el codigo del resto de la pantalla.
QR_PADDING = 18

# Ancho reservado a la columna de instrucciones. Es fijo a proposito: con el
# escalado de Windows al 150% las fuentes crecen, y si esta columna pudiera
# expandirse le robaria espacio al QR hasta recortarlo (que es exactamente el
# fallo que hacia que el telefono no pudiera leerlo).
TEXT_COLUMN_WIDTH = 380


class PairingView(tk.Frame):
    """Pantalla de vinculacion: instrucciones a la izquierda, QR a la derecha.

    El QR manda sobre la geometria. La ventana se dimensiona a partir del
    tamano real de la imagen (:meth:`required_size`), nunca al reves: si el
    codigo se recorta aunque sea unos pixeles, el telefono no lo lee.
    """

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, bg=BG)

        title_font = tkfont.Font(family="Segoe UI", size=18, weight="bold")
        body_font = tkfont.Font(family="Segoe UI", size=10)
        step_font = tkfont.Font(family="Segoe UI", size=10)
        status_font = tkfont.Font(family="Segoe UI", size=10, weight="bold")

        # Referencia viva a la imagen del QR. Sin esto Tkinter deja que el
        # recolector se lleve el PhotoImage y el QR aparece en blanco.
        self.qr_photo: ImageTk.PhotoImage | None = None

        container = tk.Frame(self, bg=BG)
        container.pack(fill="both", expand=True, padx=32, pady=24)

        # grid en vez de pack: la columna del QR tiene weight=0 y nunca cede
        # espacio, mientras que la del texto absorbe el sobrante.
        container.columnconfigure(0, weight=1, minsize=TEXT_COLUMN_WIDTH)
        container.columnconfigure(1, weight=0)
        container.rowconfigure(0, weight=1)

        left = tk.Frame(container, bg=BG)
        left.grid(row=0, column=0, sticky="nw", padx=(0, 28))

        tk.Label(
            left,
            text="Vincula tu cuenta de WhatsApp",
            font=title_font,
            bg=BG,
            fg=FG,
            anchor="w",
            justify="left",
        ).pack(anchor="w", pady=(4, 12))

        tk.Label(
            left,
            text=(
                "Escanea este codigo desde tu telefono para conectar\n"
                "WhatsApp Backup como dispositivo vinculado."
            ),
            font=body_font,
            bg=BG,
            fg=MUTED,
            anchor="w",
            justify="left",
        ).pack(anchor="w", pady=(0, 22))

        steps = (
            "1.  Abre WhatsApp en tu telefono.",
            "2.  Ve a Configuracion o menu.",
            '3.  Selecciona "Dispositivos vinculados".',
            '4.  Pulsa "Vincular un dispositivo".',
            "5.  Escanea el QR.",
        )
        for step in steps:
            tk.Label(
                left, text=step, font=step_font, bg=BG, fg=FG, anchor="w", justify="left"
            ).pack(anchor="w", pady=3)

        # -- QR a la derecha --
        right = tk.Frame(container, bg=BG)
        right.grid(row=0, column=1, sticky="n")

        self._qr_frame = tk.Frame(
            right, bg="#ffffff", highlightbackground=BORDER, highlightthickness=1
        )
        self._qr_frame.pack()
        # El label no lleva width/height en unidades de texto: eso lo ataba al
        # tamano de la fuente. Se dimensiona por la imagen y nada mas.
        self._qr_label = tk.Label(
            self._qr_frame,
            bg="#ffffff",
            text="Generando codigo...",
            fg=MUTED,
            font=body_font,
        )
        self._qr_label.pack(padx=QR_PADDING, pady=QR_PADDING)
        # Reserva provisional para que la ventana no de un salto brusco cuando
        # llegue la primera imagen.
        self._qr_size = 456

        # -- Estado abajo --
        status_bar = tk.Frame(self, bg=BG)
        status_bar.pack(fill="x", side="bottom", padx=36, pady=(0, 22))
        self._status = tk.Label(
            status_bar,
            text="●  Esperando escaneo",
            font=status_font,
            bg=BG,
            fg=MUTED,
            anchor="w",
        )
        self._status.pack(anchor="w")

    # -- API --

    def qr_budget(self) -> int:
        """Pixeles de ancho que puede ocupar el QR en esta pantalla.

        Se calcula a partir del monitor, no de la ventana: la ventana se
        adapta despues al QR. Se descuenta la columna de texto, los margenes
        y un colchon para el marco del sistema.
        """
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        horizontal = screen_w - TEXT_COLUMN_WIDTH - 2 * QR_PADDING - 140
        vertical = screen_h - 2 * QR_PADDING - 200
        return max(MIN_SIZE, min(PREFERRED_MAX_SIZE, horizontal, vertical))

    def required_size(self) -> tuple[int, int]:
        """Tamano de ventana necesario para mostrarlo todo entero.

        Se lo pregunta a Tk (``winfo_reqwidth``) en lugar de calcularlo con
        una formula: con el escalado de Windows al 150% las fuentes crecen y
        cualquier constante escrita a mano se queda corta. Se toma ademas el
        maximo con el minimo que exige el QR, por si el texto encogiera.
        """
        self.update_idletasks()
        qr_block = self._qr_size + 2 * QR_PADDING + 2  # +2 por el highlight
        width = max(self.winfo_reqwidth(), TEXT_COLUMN_WIDTH + 28 + qr_block + 2 * 32)
        height = max(self.winfo_reqheight(), qr_block + 2 * 24 + 74)
        return width, height

    def show_qr(self, payload: str) -> None:
        """Pinta (o repinta) el QR. Nunca abre otra ventana.

        pywhats rota el ref cada pocos segundos; cada rotacion sustituye la
        imagen del mismo widget.
        """
        image = render_qr(payload, max_pixels=self.qr_budget())
        self.qr_photo = ImageTk.PhotoImage(image)
        self._qr_label.configure(image=self.qr_photo, text="")
        self._qr_size = image.width
        log.info("QR actualizado (%dx%d px en pantalla)", image.width, image.height)

    def set_status(self, text: str, *, color: str = MUTED) -> None:
        self._status.configure(text=f"●  {text}", fg=color)


class StatusView(tk.Frame):
    """Pantalla intermedia: conectando, sesion invalida, error.

    Existe para que la aplicacion nunca tenga que ensenar el visor de chats
    mientras no sabe si la sesion sigue viva.
    """

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, bg=BG)

        self._title_font = tkfont.Font(family="Segoe UI", size=17, weight="bold")
        self._body_font = tkfont.Font(family="Segoe UI", size=10)

        holder = tk.Frame(self, bg=BG)
        holder.place(relx=0.5, rely=0.42, anchor="center")
        self._holder = holder

        self._title = tk.Label(holder, text="", bg=BG, fg=FG, font=self._title_font)
        self._title.pack()
        self._body = tk.Label(
            holder, text="", bg=BG, fg=MUTED, font=self._body_font, justify="center"
        )
        self._body.pack(pady=(12, 0))
        self._action: tk.Label | None = None

    def show(
        self,
        title: str,
        body: str = "",
        *,
        color: str = FG,
        action: tuple[str, Callable[[], None]] | None = None,
    ) -> None:
        self._title.configure(text=title, fg=color)
        self._body.configure(text=body)

        if self._action is not None:
            self._action.destroy()
            self._action = None
        if action is not None:
            label, command = action
            button = tk.Label(
                self._holder, text=label, bg=ACCENT, fg="#ffffff",
                font=self._body_font, padx=18, pady=8, cursor="hand2",
            )
            button.pack(pady=(20, 0))
            button.bind("<Button-1>", lambda _e: command())
            self._action = button


class App:
    """Ventana unica de la aplicacion."""

    def __init__(
        self,
        events: queue.Queue[Any],
        *,
        on_close: Callable[[], None] | None = None,
        title: str = "WhatsApp Backup - Vincular dispositivo",
    ) -> None:
        enable_dpi_awareness()

        self.root = tk.Tk()
        self.root.title(title)
        self.root.configure(bg=BG)

        self._events = events
        self._on_close = on_close
        self._handlers: dict[str, Callable[[Any], None]] = {}
        self._closing = False
        self._sidebar_job: Any = None
        # Lo inyecta main.py; si es None la GUI no aplica restricciones.
        self.session_state: Any = None

        self.pairing = PairingView(self.root)
        self.status = StatusView(self.root)
        self.viewer: Any = None          # se crea solo si hace falta
        self._current: tk.Frame | None = None

        self.show_pairing()
        self.root.protocol("WM_DELETE_WINDOW", self._handle_close)

    # -- Cambio de vista ------------------------------------------------------

    def _swap(self, frame: tk.Frame) -> None:
        """Una sola ventana, una sola vista visible. Nunca un Toplevel nuevo."""
        if self._current is frame:
            return
        if self._current is not None:
            self._current.pack_forget()
        frame.pack(fill="both", expand=True)
        self._current = frame

    def show_pairing(self) -> None:
        self.root.title("WhatsApp Backup - Vincular dispositivo")
        self._swap(self.pairing)
        self.fit_to_content()

    def show_status(
        self,
        title: str,
        body: str = "",
        *,
        titulo_ventana: str = "WhatsApp Backup",
        color: str = FG,
        action: Any = None,
    ) -> None:
        """Pantalla de estado. Nunca muestra chats."""
        self.root.title(titulo_ventana)
        self.status.show(title, body, color=color, action=action)
        self._swap(self.status)
        if self.root.winfo_width() < 700:
            center(self.root, 760, 520)

    def attach_viewer(
        self, session_factory: Callable[[], Any], media_root: Any = None
    ) -> Any:
        """Crea el visor de chats (perezoso) y lo devuelve."""
        if self.viewer is None:
            from app.gui.chat_view import ChatViewer

            self.viewer = ChatViewer(self.root, session_factory, media_root)
        return self.viewer

    def show_viewer(self, *, connected: bool = False) -> None:
        """Muestra el visor. Solo si el estado de sesion lo autoriza."""
        if self.viewer is None:
            raise RuntimeError("llama antes a attach_viewer()")
        if self.session_state is not None and not self.session_state.viewer_allowed:
            # Barrera final: aunque un evento tardio pida abrir el visor, si la
            # sesion no esta confirmada no se abre (seccion 9).
            log.warning(
                "Visor bloqueado: estado=%s", self.session_state.state.value
            )
            return
        estado = "Conectado" if connected else "Sin conexion"
        self.root.title(f"WhatsApp Backup - {estado}")
        self._swap(self.viewer)

        # El visor necesita mucho mas espacio que la pantalla del QR.
        width = min(1280, self.root.winfo_screenwidth() - 80)
        height = min(820, self.root.winfo_screenheight() - 120)
        self.root.minsize(900, 560)
        center(self.root, width, height)
        self.viewer.refresh_chats()

    # -- Geometria ------------------------------------------------------------

    def fit_to_content(self) -> None:
        """Ajusta la ventana al tamano que el QR necesita y la centra.

        Se llama tras cada QR porque la imagen puede cambiar de tamano entre
        rotaciones si cambia la longitud del payload. ``minsize`` impide que
        el usuario encoja la ventana hasta recortar el codigo.
        """
        width, height = self.pairing.required_size()
        # Nunca mas grande que la pantalla disponible.
        width = min(width, self.root.winfo_screenwidth() - 80)
        height = min(height, self.root.winfo_screenheight() - 120)

        current = (self.root.winfo_width(), self.root.winfo_height())
        self.root.minsize(width, height)
        if current != (width, height):
            center(self.root, width, height)
            log.debug("Ventana ajustada a %dx%d", width, height)

    def bring_to_front(self) -> None:
        """Trae la ventana al frente al arrancar.

        Sin esto puede abrirse detras del editor y el QR caduca sin que nadie
        lo vea. El 'topmost' se quita enseguida para no dejarla pegada encima
        de todo lo demas.
        """
        try:
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(700, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
        except tk.TclError:  # pragma: no cover - depende del gestor de ventanas
            log.debug("No se pudo traer la ventana al frente")

    # -- Despacho de eventos --------------------------------------------------

    def on(self, event_name: str, handler: Callable[[Any], None]) -> None:
        """Registra un handler que corre YA en el hilo de Tkinter."""
        self._handlers[event_name] = handler

    def _pump(self) -> None:
        """Vacia la cola del cliente. Unico puente entre los dos hilos."""
        try:
            while True:
                event = self._events.get_nowait()
                handler = self._handlers.get(event.name)
                if handler is None:
                    continue
                try:
                    handler(event)
                except Exception:  # noqa: BLE001 - un handler roto no cierra la GUI
                    log.exception("El handler de %r fallo", event.name)
        except queue.Empty:
            pass
        if not self._closing:
            self.root.after(POLL_INTERVAL_MS, self._pump)

    def _handle_close(self) -> None:
        self._closing = True
        if self._on_close is not None:
            self._on_close()
        self.root.destroy()

    def schedule_sidebar_refresh(self, delay_ms: int = 400) -> None:
        """Refresca el sidebar UNA vez, agrupando avisos seguidos."""
        if self._sidebar_job is not None:
            self.root.after_cancel(self._sidebar_job)

        def run() -> None:
            self._sidebar_job = None
            if self.session_state is not None and not self.session_state.viewer_allowed:
                log.debug("Refresco de sidebar ignorado: la sesion no esta activa")
                return
            if self.viewer is not None:
                self.viewer.refresh_chats()

        self._sidebar_job = self.root.after(delay_ms, run)

    def show_qr(self, payload: str) -> None:
        """Pinta el QR y reajusta la ventana para que quepa entero."""
        self.pairing.show_qr(payload)
        self.fit_to_content()

    def run(self) -> None:
        """Arranca el unico mainloop del proceso."""
        self.root.after(POLL_INTERVAL_MS, self._pump)
        self.root.after(120, self.bring_to_front)
        self.root.mainloop()

    def close(self) -> None:
        if not self._closing:
            self._closing = True
            self.root.destroy()
