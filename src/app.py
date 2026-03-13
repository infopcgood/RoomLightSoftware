"""RoomLightSoftware — main application window."""

import ipaddress
import tkinter as tk
from tkinter import messagebox, ttk


class RoomLightApp(tk.Tk):
    """Main application window for controlling RoomLight strips."""

    def __init__(self):
        super().__init__()
        self.title("RoomLight Control")
        self.resizable(False, False)
        self._strips = []  # list of {"ip": str, "port": str}
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        """Create all widgets."""
        self.columnconfigure(0, weight=1)

        # ── Title ──────────────────────────────────────────────────────
        ttk.Label(
            self,
            text="RoomLight Control",
            font=("Helvetica", 18, "bold"),
        ).grid(row=0, column=0, pady=(16, 8), padx=16, sticky="w")

        # ── Card list container ────────────────────────────────────────
        self._list_frame = ttk.Frame(self)
        self._list_frame.grid(row=1, column=0, padx=16, sticky="ew")
        self._list_frame.columnconfigure(0, weight=1)

        # ── Add-strip button ───────────────────────────────────────────
        bottom_frame = ttk.Frame(self)
        bottom_frame.grid(row=2, column=0, padx=16, pady=(4, 16), sticky="ew")
        bottom_frame.columnconfigure(0, weight=1)

        add_btn = tk.Button(
            bottom_frame,
            text="+",
            width=3,
            height=1,
            font=("Helvetica", 14, "bold"),
            command=self._open_add_dialog,
        )
        add_btn.grid(row=0, column=0, sticky="e")

        self._refresh_list()

    def _refresh_list(self):
        """Rebuild the card list from the current strips data."""
        for widget in self._list_frame.winfo_children():
            widget.destroy()

        for index, strip in enumerate(self._strips):
            self._build_card(index, strip)

    def _build_card(self, index, strip):
        """Create a single card row for a LED strip."""
        card = ttk.Frame(self._list_frame, relief="solid", borderwidth=1)
        card.grid(row=index, column=0, sticky="ew", pady=4)
        card.columnconfigure(0, weight=1)

        ttk.Label(
            card,
            text=f"{strip['ip']}:{strip['port']}",
            font=("Helvetica", 12),
        ).grid(row=0, column=0, padx=10, pady=8, sticky="w")

        ttk.Button(
            card,
            text="-",
            width=3,
            command=lambda i=index: self._remove_strip(i),
        ).grid(row=0, column=1, padx=8, pady=8)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _remove_strip(self, index):
        """Remove a LED strip from the list."""
        del self._strips[index]
        self._refresh_list()

    def _open_add_dialog(self):
        """Show the add-strip popup dialog."""
        dialog = tk.Toplevel(self)
        dialog.title("Add LED Strip")
        dialog.resizable(False, False)
        dialog.grab_set()

        ttk.Label(
            dialog,
            text="Add LED Strip",
            font=("Helvetica", 14, "bold"),
        ).grid(row=0, column=0, columnspan=2, pady=(16, 12), padx=16)

        ttk.Label(dialog, text="IP address:").grid(
            row=1, column=0, padx=16, pady=4, sticky="e"
        )
        ip_var = tk.StringVar()
        ttk.Entry(dialog, textvariable=ip_var, width=22).grid(
            row=1, column=1, padx=(0, 16), pady=4
        )

        ttk.Label(dialog, text="Port:").grid(
            row=2, column=0, padx=16, pady=4, sticky="e"
        )
        port_var = tk.StringVar()
        ttk.Entry(dialog, textvariable=port_var, width=22).grid(
            row=2, column=1, padx=(0, 16), pady=4
        )

        def _on_ok():
            ip = ip_var.get().strip()
            port = port_var.get().strip()
            if not ip or not port:
                return
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                messagebox.showerror("Invalid IP", f"'{ip}' is not a valid IP address.", parent=dialog)
                return
            try:
                port_int = int(port)
                if not (1 <= port_int <= 65535):
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid port", "Port must be an integer between 1 and 65535.", parent=dialog)
                return
            self._add_strip(ip, port)
            dialog.destroy()

        ttk.Button(dialog, text="OK", command=_on_ok).grid(
            row=3, column=0, columnspan=2, pady=(12, 16)
        )

    def _add_strip(self, ip, port):
        """Placeholder function executed when a new strip is confirmed."""
        self._strips.append({"ip": ip, "port": port})
        self._refresh_list()
