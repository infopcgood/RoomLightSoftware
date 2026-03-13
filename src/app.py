"""RoomLightSoftware — main application window."""

import tkinter as tk
from tkinter import ttk


class RoomLightApp(tk.Tk):
    """Main application window for controlling RoomLight strips."""

    def __init__(self):
        super().__init__()
        self.title("RoomLight Control")
        self.resizable(False, False)
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        """Create all widgets."""
        padding = {"padx": 12, "pady": 8}

        # ── Title ──────────────────────────────────────────────────────
        title_label = ttk.Label(
            self,
            text="RoomLight Control",
            font=("Helvetica", 18, "bold"),
        )
        title_label.grid(row=0, column=0, columnspan=2, pady=(16, 8))

        # ── Power toggle ───────────────────────────────────────────────
        self._power_var = tk.BooleanVar(value=False)
        power_frame = ttk.LabelFrame(self, text="Power")
        power_frame.grid(row=1, column=0, columnspan=2, **padding, sticky="ew")

        self._power_btn = ttk.Button(
            power_frame,
            text="Turn ON",
            command=self._toggle_power,
            width=14,
        )
        self._power_btn.pack(padx=8, pady=6)

        # ── Brightness ─────────────────────────────────────────────────
        brightness_frame = ttk.LabelFrame(self, text="Brightness")
        brightness_frame.grid(row=2, column=0, columnspan=2, **padding, sticky="ew")

        self._brightness_var = tk.IntVar(value=100)
        brightness_scale = ttk.Scale(
            brightness_frame,
            from_=0,
            to=100,
            orient="horizontal",
            variable=self._brightness_var,
            length=220,
            command=self._on_brightness_change,
        )
        brightness_scale.pack(padx=8, pady=4)

        self._brightness_label = ttk.Label(
            brightness_frame,
            text="100 %",
        )
        self._brightness_label.pack(pady=(0, 6))

        # ── Colour presets ─────────────────────────────────────────────
        colour_frame = ttk.LabelFrame(self, text="Colour Preset")
        colour_frame.grid(row=3, column=0, columnspan=2, **padding, sticky="ew")

        self._colour_var = tk.StringVar(value="White")
        colours = ["White", "Warm White", "Red", "Green", "Blue", "Purple"]
        colour_combo = ttk.Combobox(
            colour_frame,
            textvariable=self._colour_var,
            values=colours,
            state="readonly",
            width=18,
        )
        colour_combo.pack(padx=8, pady=6)
        colour_combo.bind("<<ComboboxSelected>>", self._on_colour_change)

        # ── Status bar ─────────────────────────────────────────────────
        self._status_var = tk.StringVar(value="Ready.")
        status_bar = ttk.Label(
            self,
            textvariable=self._status_var,
            relief="sunken",
            anchor="w",
        )
        status_bar.grid(
            row=4, column=0, columnspan=2, sticky="ew", padx=4, pady=(4, 8)
        )

        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _toggle_power(self):
        self._power_var.set(not self._power_var.get())
        if self._power_var.get():
            self._power_btn.configure(text="Turn OFF")
            self._status_var.set("Lights turned ON.")
        else:
            self._power_btn.configure(text="Turn ON")
            self._status_var.set("Lights turned OFF.")

    def _on_brightness_change(self, value):
        level = int(float(value))
        self._brightness_label.configure(text=f"{level} %")
        self._status_var.set(f"Brightness set to {level} %.")

    def _on_colour_change(self, _event=None):
        colour = self._colour_var.get()
        self._status_var.set(f"Colour preset set to '{colour}'.")
