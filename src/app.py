"""RoomLightSoftware — main application window."""

import colorsys
import ctypes
import ipaddress
import json
import socket
import threading
import tkinter as tk
import time
from pathlib import Path
from tkinter import colorchooser, messagebox, ttk

try:
    import numpy as np
    import soundcard as sc
    from soundcard import mediafoundation as sc_mediafoundation
except ImportError:
    np = None
    sc = None
    sc_mediafoundation = None


def _patch_soundcard_fromstring_binary_mode():
    """Patch soundcard's binary fromstring call for NumPy/Python compatibility."""
    if np is None or sc_mediafoundation is None:
        return

    if getattr(sc_mediafoundation, "_roomlight_fromstring_patched", False):
        return

    original_fromstring = sc_mediafoundation.numpy.fromstring

    def _fromstring_compat(buffer_data, dtype=float, count=-1, sep=""):
        if sep == "":
            if count == -1:
                return sc_mediafoundation.numpy.frombuffer(buffer_data, dtype=dtype)
            return sc_mediafoundation.numpy.frombuffer(buffer_data, dtype=dtype, count=count)
        return original_fromstring(buffer_data, dtype=dtype, count=count, sep=sep)

    sc_mediafoundation.numpy.fromstring = _fromstring_compat
    sc_mediafoundation._roomlight_fromstring_patched = True


_patch_soundcard_fromstring_binary_mode()


class RoomLightApp(tk.Tk):
    """Main application window for controlling RoomLight strips."""

    def __init__(self):
        super().__init__()
        self.title("RoomLight Control")
        self.resizable(False, False)
        self._strips = []  # list of {"name": str, "ip": str, "port": str, ...}
        self._settings_path = Path(__file__).resolve().parents[1] / "roomlight_settings.json"
        self._strobe_frame_ms = 20
        self._live_volume_frame_ms = 8  # 125 FPS target
        self._live_volume_samplerate = 48000
        self._live_volume_block_frames = 256  # ~188 captures/sec at 48 kHz
        self._live_volume_sensitivity_power = 0.65
        self._red_segment_weight = 1.4
        self._red_edge_ease_power = 1.35
        self._timer_resolution_ms = 1
        self._winmm = None
        self._high_res_timer_enabled = False
        self._enable_high_resolution_timer()
        self._udp_max_payload_bytes = 8192
        self._fft_block_frames = 256    # ~5 ms per capture at 48 kHz
        self._fft_frame_ms = max(
            self._live_volume_frame_ms,
            int(round(1000 * self._fft_block_frames / self._live_volume_samplerate)),
        )
        self._fft_window_size = 4096    # ~11.7 Hz frequency resolution at 48 kHz
        self._load_settings()
        self.protocol("WM_DELETE_WINDOW", self._close_app)
        self._build_ui()
        self.after_idle(self._auto_restart_saved_modes)

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

        self.update_idletasks()
        self.geometry(f"{max(680, self.winfo_reqwidth())}x{self.winfo_reqheight()}")

    def _build_card(self, index, strip):
        """Create a single card row for a LED strip."""
        card = ttk.Frame(self._list_frame, relief="solid", borderwidth=1, cursor="hand2")
        card.grid(row=index, column=0, sticky="ew", pady=4)
        card.columnconfigure(0, weight=1)

        click_handler = lambda _event, i=index: self._open_mode_dialog(i)

        name_label = ttk.Label(
            card,
            text=strip["name"],
            font=("Helvetica", 12, "bold"),
            cursor="hand2",
        )
        name_label.grid(row=0, column=0, padx=12, pady=(10, 2), sticky="w")

        details_label = ttk.Label(
            card,
            text=(
                f"IP: {strip['ip']}    "
                f"Port: {strip['port']}    "
                f"Data pin: {strip['data_pin']}"
            ),
            font=("Helvetica", 10),
            cursor="hand2",
        )
        details_label.grid(row=1, column=0, padx=12, pady=(0, 10), sticky="w")

        ttk.Button(
            card,
            text="-",
            width=3,
            command=lambda i=index: self._remove_strip(i),
        ).grid(row=0, column=1, rowspan=2, padx=10, pady=8, sticky="ns")

        card.bind("<Button-1>", click_handler)
        name_label.bind("<Button-1>", click_handler)
        details_label.bind("<Button-1>", click_handler)

    def _build_strip_record(
        self,
        *,
        name,
        ip,
        port,
        num_leds,
        data_pin,
        mode="full_color",
        color=(255, 80, 0),
        strobe_points=None,
        strobe_interval_ms=3000,
        strobe_speed_leds=12.0,
        live_volume_gain=2.5,
        live_volume_led_count=None,
        fft_led_count=None,
        fft_color_low=(0, 0, 255),
        fft_color_high=(255, 0, 0),
        fft_sustain_ms=0,
        fft_gain=2.5,
    ):
        """Build a strip record with persistent fields and runtime defaults."""
        if strobe_points is None:
            strobe_points = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]

        if live_volume_led_count is None:
            live_volume_led_count = num_leds

        if fft_led_count is None:
            fft_led_count = num_leds

        return {
            "name": name,
            "ip": ip,
            "port": str(port),
            "num_leds": num_leds,
            "data_pin": data_pin,
            "mode": mode,
            "color": tuple(color),
            "strobe_points": [tuple(point) for point in strobe_points],
            "strobe_interval_ms": strobe_interval_ms,
            "strobe_speed_leds": strobe_speed_leds,
            "live_volume_gain": live_volume_gain,
            "live_volume_led_count": live_volume_led_count,
            "fft_led_count": fft_led_count,
            "fft_color_low": tuple(fft_color_low),
            "fft_color_high": tuple(fft_color_high),
            "fft_sustain_ms": fft_sustain_ms,
            "fft_gain": fft_gain,
            "live_volume_level": 0.0,
            "live_volume_smoothed": 0.0,
            "live_volume_error": None,
            "fft_levels": [],
            "fft_smoothed": [],
            "fft_held_levels": [],
            "fft_held_until": [],
            "fft_error": None,
            "volume_stop_event": None,
            "volume_worker": None,
            "volume_stream": None,
            "animation_job": None,
        }

    def _sanitize_strip_data(self, strip_data):
        """Validate and normalize persisted strip data."""
        if not isinstance(strip_data, dict):
            return None

        name = str(strip_data.get("name", "")).strip()
        ip = str(strip_data.get("ip", "")).strip()
        if not name or not ip:
            return None

        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return None

        try:
            port_int = int(strip_data.get("port", 0))
        except (TypeError, ValueError):
            return None
        if not 1 <= port_int <= 65535:
            return None

        try:
            num_leds = int(strip_data.get("num_leds", 0))
        except (TypeError, ValueError):
            return None
        if not 1 <= num_leds <= 65535:
            return None

        try:
            data_pin = int(strip_data.get("data_pin", 0))
        except (TypeError, ValueError):
            data_pin = 0

        mode = str(strip_data.get("mode", "full_color"))
        if mode not in {"full_color", "strobe", "live_volume", "fft_spectrum"}:
            mode = "full_color"

        default_color = (255, 80, 0)
        color_value = strip_data.get("color", default_color)
        if (
            isinstance(color_value, (list, tuple))
            and len(color_value) == 3
            and all(isinstance(channel, (int, float)) for channel in color_value)
        ):
            color = tuple(min(255, max(0, int(round(channel)))) for channel in color_value)
        else:
            color = default_color

        strobe_points_value = strip_data.get("strobe_points", [])
        strobe_points = []
        if isinstance(strobe_points_value, list):
            for point in strobe_points_value:
                if (
                    isinstance(point, (list, tuple))
                    and len(point) == 3
                    and all(isinstance(channel, (int, float)) for channel in point)
                ):
                    strobe_points.append(
                        tuple(min(255, max(0, int(round(channel)))) for channel in point)
                    )
        if len(strobe_points) < 2:
            strobe_points = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]

        try:
            strobe_interval_ms = int(strip_data.get("strobe_interval_ms", 3000))
        except (TypeError, ValueError):
            strobe_interval_ms = 3000
        strobe_interval_ms = min(60000, max(250, strobe_interval_ms))

        try:
            strobe_speed_leds = float(strip_data.get("strobe_speed_leds", 12.0))
        except (TypeError, ValueError):
            strobe_speed_leds = 12.0
        strobe_speed_leds = min(1000.0, max(0.0, strobe_speed_leds))

        try:
            live_volume_gain = float(strip_data.get("live_volume_gain", 2.5))
        except (TypeError, ValueError):
            live_volume_gain = 2.5
        live_volume_gain = min(20.0, max(0.1, live_volume_gain))

        try:
            live_volume_led_count = int(strip_data.get("live_volume_led_count", num_leds))
        except (TypeError, ValueError):
            live_volume_led_count = num_leds
        live_volume_led_count = min(num_leds, max(1, live_volume_led_count))

        try:
            fft_led_count = int(strip_data.get("fft_led_count", num_leds))
        except (TypeError, ValueError):
            fft_led_count = num_leds
        fft_led_count = min(num_leds, max(1, fft_led_count))

        _default_fft_low = (0, 0, 255)
        _fft_low_raw = strip_data.get("fft_color_low", _default_fft_low)
        if (
            isinstance(_fft_low_raw, (list, tuple))
            and len(_fft_low_raw) == 3
            and all(isinstance(ch, (int, float)) for ch in _fft_low_raw)
        ):
            fft_color_low = tuple(min(255, max(0, int(round(ch)))) for ch in _fft_low_raw)
        else:
            fft_color_low = _default_fft_low

        _default_fft_high = (255, 0, 0)
        _fft_high_raw = strip_data.get("fft_color_high", _default_fft_high)
        if (
            isinstance(_fft_high_raw, (list, tuple))
            and len(_fft_high_raw) == 3
            and all(isinstance(ch, (int, float)) for ch in _fft_high_raw)
        ):
            fft_color_high = tuple(min(255, max(0, int(round(ch)))) for ch in _fft_high_raw)
        else:
            fft_color_high = _default_fft_high

        try:
            fft_sustain_ms = int(strip_data.get("fft_sustain_ms", 0))
        except (TypeError, ValueError):
            fft_sustain_ms = 0
        fft_sustain_ms = min(5000, max(0, fft_sustain_ms))

        try:
            fft_gain = float(strip_data.get("fft_gain", 2.5))
        except (TypeError, ValueError):
            fft_gain = 2.5
        fft_gain = min(50.0, max(0.1, fft_gain))

        return self._build_strip_record(
            name=name,
            ip=ip,
            port=str(port_int),
            num_leds=num_leds,
            data_pin=data_pin,
            mode=mode,
            color=color,
            strobe_points=strobe_points,
            strobe_interval_ms=strobe_interval_ms,
            strobe_speed_leds=strobe_speed_leds,
            live_volume_gain=live_volume_gain,
            live_volume_led_count=live_volume_led_count,
            fft_led_count=fft_led_count,
            fft_color_low=fft_color_low,
            fft_color_high=fft_color_high,
            fft_sustain_ms=fft_sustain_ms,
            fft_gain=fft_gain,
        )

    def _serialize_strip(self, strip):
        """Serialize a strip to JSON-safe persistent fields."""
        return {
            "name": strip["name"],
            "ip": strip["ip"],
            "port": str(strip["port"]),
            "num_leds": int(strip["num_leds"]),
            "data_pin": int(strip["data_pin"]),
            "mode": strip.get("mode", "full_color"),
            "color": [int(channel) for channel in strip.get("color", (255, 80, 0))],
            "strobe_points": [
                [int(channel) for channel in point]
                for point in strip.get("strobe_points", [(255, 0, 0), (0, 255, 0), (0, 0, 255)])
            ],
            "strobe_interval_ms": int(strip.get("strobe_interval_ms", 3000)),
            "strobe_speed_leds": float(strip.get("strobe_speed_leds", 12.0)),
            "live_volume_gain": float(strip.get("live_volume_gain", 2.5)),
            "live_volume_led_count": int(strip.get("live_volume_led_count", strip["num_leds"])),
            "fft_led_count": int(strip.get("fft_led_count", strip["num_leds"])),
            "fft_color_low": [int(ch) for ch in strip.get("fft_color_low", (0, 0, 255))],
            "fft_color_high": [int(ch) for ch in strip.get("fft_color_high", (255, 0, 0))],
            "fft_sustain_ms": int(strip.get("fft_sustain_ms", 0)),
            "fft_gain": float(strip.get("fft_gain", 2.5)),
        }

    def _save_settings(self):
        """Persist all strips and their configurable options to disk."""
        payload = {
            "version": 1,
            "strips": [self._serialize_strip(strip) for strip in self._strips],
        }

        try:
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._settings_path.with_suffix(self._settings_path.suffix + ".tmp")
            with temp_path.open("w", encoding="utf-8") as settings_file:
                json.dump(payload, settings_file, indent=2)
            temp_path.replace(self._settings_path)
        except (OSError, TypeError, ValueError):
            # Keep the app running even if persistence fails.
            return

    def _load_settings(self):
        """Load persisted strips and configurable options from disk."""
        if not self._settings_path.exists():
            return

        try:
            with self._settings_path.open("r", encoding="utf-8") as settings_file:
                payload = json.load(settings_file)
        except (OSError, ValueError):
            return

        if not isinstance(payload, dict):
            return

        strips_data = payload.get("strips", [])
        if not isinstance(strips_data, list):
            return

        loaded_strips = []
        for strip_data in strips_data:
            strip = self._sanitize_strip_data(strip_data)
            if strip is not None:
                loaded_strips.append(strip)
        self._strips = loaded_strips

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _remove_strip(self, index):
        """Remove a LED strip from the list."""
        self._stop_strip_animation(self._strips[index])
        del self._strips[index]
        self._save_settings()
        self._refresh_list()

    def _rgb_to_hex(self, red, green, blue):
        """Convert RGB components to a Tk color string."""
        return f"#{red:02x}{green:02x}{blue:02x}"

    def _enhance_color(self, red, green, blue):
        """Boost saturation slightly so colors stay vivid on strips with white bleed."""
        hue, saturation, value = colorsys.rgb_to_hsv(
            red / 255,
            green / 255,
            blue / 255,
        )
        if value == 0 or saturation < 0.05:
            return red, green, blue

        boosted_saturation = saturation + (1.0 - saturation) * 0.35
        boosted_value = min(1.0, value * 1.05)
        output_red, output_green, output_blue = colorsys.hsv_to_rgb(
            hue,
            boosted_saturation,
            boosted_value,
        )
        return (
            round(output_red * 255),
            round(output_green * 255),
            round(output_blue * 255),
        )

    def _srgb_to_linear(self, channel):
        """Convert an 8-bit sRGB channel to linear RGB."""
        value = channel / 255
        if value <= 0.04045:
            return value / 12.92
        return ((value + 0.055) / 1.055) ** 2.4

    def _linear_to_srgb(self, channel):
        """Convert a linear RGB channel to 8-bit sRGB."""
        channel = min(1.0, max(0.0, channel))
        if channel <= 0.0031308:
            value = 12.92 * channel
        else:
            value = 1.055 * (channel ** (1 / 2.4)) - 0.055
        return round(value * 255)

    def _rgb_to_oklab(self, red, green, blue):
        """Convert an RGB color to OKLab."""
        linear_red = self._srgb_to_linear(red)
        linear_green = self._srgb_to_linear(green)
        linear_blue = self._srgb_to_linear(blue)

        l = 0.4122214708 * linear_red + 0.5363325363 * linear_green + 0.0514459929 * linear_blue
        m = 0.2119034982 * linear_red + 0.6806995451 * linear_green + 0.1073969566 * linear_blue
        s = 0.0883024619 * linear_red + 0.2817188376 * linear_green + 0.6299787005 * linear_blue

        l_root = l ** (1 / 3)
        m_root = m ** (1 / 3)
        s_root = s ** (1 / 3)

        return (
            0.2104542553 * l_root + 0.7936177850 * m_root - 0.0040720468 * s_root,
            1.9779984951 * l_root - 2.4285922050 * m_root + 0.4505937099 * s_root,
            0.0259040371 * l_root + 0.7827717662 * m_root - 0.8086757660 * s_root,
        )

    def _oklab_to_rgb(self, lightness, green_red, blue_yellow):
        """Convert an OKLab color to RGB."""
        l_root = lightness + 0.3963377774 * green_red + 0.2158037573 * blue_yellow
        m_root = lightness - 0.1055613458 * green_red - 0.0638541728 * blue_yellow
        s_root = lightness - 0.0894841775 * green_red - 1.2914855480 * blue_yellow

        l = l_root ** 3
        m = m_root ** 3
        s = s_root ** 3

        linear_red = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
        linear_green = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
        linear_blue = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s

        return (
            self._linear_to_srgb(linear_red),
            self._linear_to_srgb(linear_green),
            self._linear_to_srgb(linear_blue),
        )

    def _send_full_color_update(self, strip, red, green, blue):
        """Set every LED in the strip to a single RGB color over UDP."""
        num_leds = strip["num_leds"]
        if num_leds <= 0:
            raise ValueError("The strip does not report any LEDs.")
        if num_leds > 65535:
            raise ValueError("The strip reports more LEDs than the update packet supports.")

        output_red, output_green, output_blue = self._enhance_color(red, green, blue)

        parsed_ip = ipaddress.ip_address(strip["ip"])
        family = socket.AF_INET6 if parsed_ip.version == 6 else socket.AF_INET
        target = (
            (strip["ip"], int(strip["port"]), 0, 0)
            if family == socket.AF_INET6
            else (strip["ip"], int(strip["port"]))
        )

        # Keep packets in strict ascending LED order and fit within UDP payload budget.
        max_updates_per_packet = max(1, (self._udp_max_payload_bytes - 1) // 5)
        with socket.socket(family, socket.SOCK_DGRAM) as udp_socket:
            for start_index in range(0, num_leds, max_updates_per_packet):
                end_index = min(start_index + max_updates_per_packet, num_leds)
                update_count = end_index - start_index
                packet = bytearray(1 + update_count * 5)
                packet[0] = ord("u")
                for offset_index, led_index in enumerate(range(start_index, end_index)):
                    offset = 1 + offset_index * 5
                    packet[offset : offset + 2] = led_index.to_bytes(2, byteorder="big")
                    packet[offset + 2 : offset + 5] = bytes(
                        (output_red, output_green, output_blue)
                    )
                udp_socket.sendto(packet, target)

        return output_red, output_green, output_blue

    def _send_per_led_update(self, strip, led_colors):
        """Send a per-LED RGB frame over UDP."""
        num_leds = strip["num_leds"]
        if num_leds <= 0:
            raise ValueError("The strip does not report any LEDs.")
        if num_leds > 65535:
            raise ValueError("The strip reports more LEDs than the update packet supports.")
        if len(led_colors) != num_leds:
            raise ValueError("Per-LED frame does not match strip LED count.")

        parsed_ip = ipaddress.ip_address(strip["ip"])
        family = socket.AF_INET6 if parsed_ip.version == 6 else socket.AF_INET
        target = (
            (strip["ip"], int(strip["port"]), 0, 0)
            if family == socket.AF_INET6
            else (strip["ip"], int(strip["port"]))
        )

        # Keep packets in strict ascending LED order and fit within UDP payload budget.
        max_updates_per_packet = max(1, (self._udp_max_payload_bytes - 1) // 5)
        with socket.socket(family, socket.SOCK_DGRAM) as udp_socket:
            for start_index in range(0, num_leds, max_updates_per_packet):
                end_index = min(start_index + max_updates_per_packet, num_leds)
                update_count = end_index - start_index
                packet = bytearray(1 + update_count * 5)
                packet[0] = ord("u")
                for offset_index, led_index in enumerate(range(start_index, end_index)):
                    red, green, blue = self._enhance_color(*led_colors[led_index])
                    offset = 1 + offset_index * 5
                    packet[offset : offset + 2] = led_index.to_bytes(2, byteorder="big")
                    packet[offset + 2 : offset + 5] = bytes((red, green, blue))
                udp_socket.sendto(packet, target)

    def _volume_zone_counts(self, num_leds):
        """Return LED counts for green/yellow/red zones (70/20/10 split)."""
        green_count = int(round(num_leds * 0.7))
        yellow_count = int(round(num_leds * 0.2))
        red_count = num_leds - green_count - yellow_count

        if red_count < 0:
            yellow_count = max(0, yellow_count + red_count)
            red_count = num_leds - green_count - yellow_count
        if red_count < 0:
            green_count = max(0, green_count + red_count)
            red_count = num_leds - green_count - yellow_count

        return green_count, yellow_count, red_count

    def _build_live_volume_led_colors(self, total_leds, active_leds, level):
        """Build per-LED colors for the live volume meter."""
        if total_leds <= 0:
            return []

        active_leds = min(max(1, int(active_leds)), total_leds)
        green_count, yellow_count, _ = self._volume_zone_counts(active_leds)
        lit_leds = int(round(min(1.0, max(0.0, level)) * active_leds))
        yellow_boundary = green_count + yellow_count

        led_colors = []
        for led_index in range(total_leds):
            if led_index >= active_leds or led_index >= lit_leds:
                led_colors.append((0, 0, 0))
                continue

            if led_index < green_count:
                led_colors.append((0, 255, 0))
            elif led_index < yellow_boundary:
                led_colors.append((255, 220, 0))
            else:
                led_colors.append((255, 0, 0))

        return led_colors

    def _stop_strip_animation(self, strip):
        """Cancel any scheduled animation callback for a strip."""
        animation_job = strip.get("animation_job")
        if animation_job is not None:
            try:
                self.after_cancel(animation_job)
            except tk.TclError:
                pass

        volume_stop_event = strip.get("volume_stop_event")
        if volume_stop_event is not None:
            volume_stop_event.set()

        volume_worker = strip.get("volume_worker")
        if volume_worker is not None and volume_worker.is_alive():
            volume_worker.join(timeout=0.25)

        # Backward compatibility for older sessions that still had a stream object.
        volume_stream = strip.get("volume_stream")
        if volume_stream is not None:
            try:
                volume_stream.stop()
                volume_stream.close()
            except Exception:
                pass

        strip["animation_job"] = None
        strip["volume_stop_event"] = None
        strip["volume_worker"] = None
        strip["volume_stream"] = None
        strip["live_volume_error"] = None
        strip["fft_error"] = None
        strip["fft_levels"] = []
        strip["fft_smoothed"] = []
        strip["fft_held_levels"] = []
        strip["fft_held_until"] = []
        strip.pop("animation_started_at", None)

    def _restart_strip_from_saved_mode(self, strip):
        """Apply a strip's persisted mode and settings at startup."""
        mode = str(strip.get("mode", "full_color"))

        if mode == "full_color":
            red, green, blue = strip.get("color", (255, 80, 0))
            self._stop_strip_animation(strip)
            self._send_full_color_update(strip, int(red), int(green), int(blue))
            return True

        if mode == "strobe":
            self._start_strobe_animation(strip)
            return strip.get("animation_job") is not None

        if mode == "live_volume":
            self._start_live_volume_animation(strip)
            return strip.get("animation_job") is not None

        if mode == "fft_spectrum":
            self._start_fft_animation(strip)
            return strip.get("animation_job") is not None

        self._stop_strip_animation(strip)
        return True

    def _auto_restart_saved_modes(self):
        """Automatically restart all strips with their persisted mode on launch."""
        if not self._strips:
            return

        failed_starts = []
        for strip in self._strips:
            try:
                started_ok = self._restart_strip_from_saved_mode(strip)
            except (OSError, ValueError) as exc:
                self._stop_strip_animation(strip)
                failed_starts.append(
                    f"{strip.get('name', 'Unnamed strip')} ({strip.get('mode', 'full_color')}): {exc}"
                )
                continue

            if not started_ok:
                failed_starts.append(
                    f"{strip.get('name', 'Unnamed strip')} ({strip.get('mode', 'full_color')}): "
                    "The mode did not start successfully."
                )

        if failed_starts:
            preview_lines = failed_starts[:5]
            if len(failed_starts) > 5:
                preview_lines.append(f"... and {len(failed_starts) - 5} more.")
            messagebox.showwarning(
                "Auto-restart incomplete",
                "Some strips could not be restarted automatically:\n\n" + "\n".join(preview_lines),
                parent=self,
            )

    def _close_app(self):
        """Stop active animations before closing the application."""
        for strip in self._strips:
            self._stop_strip_animation(strip)
        self._save_settings()
        self._disable_high_resolution_timer()
        self.destroy()

    def _enable_high_resolution_timer(self):
        """Request 1 ms Windows timer granularity for smoother high-FPS animations."""
        if not hasattr(ctypes, "WinDLL"):
            return

        try:
            winmm = ctypes.WinDLL("winmm")
            result = winmm.timeBeginPeriod(self._timer_resolution_ms)
        except Exception:
            return

        if result == 0:
            self._winmm = winmm
            self._high_res_timer_enabled = True

    def _disable_high_resolution_timer(self):
        """Release the Windows high-resolution timer request if it was enabled."""
        if not self._high_res_timer_enabled or self._winmm is None:
            return

        try:
            self._winmm.timeEndPeriod(self._timer_resolution_ms)
        except Exception:
            pass

        self._high_res_timer_enabled = False
        self._winmm = None

    def _interpolate_oklab_color(self, start_color, end_color, progress):
        """Blend two RGB colors in OKLab for smoother perceptual transitions."""
        start_lightness, start_green_red, start_blue_yellow = self._rgb_to_oklab(*start_color)
        end_lightness, end_green_red, end_blue_yellow = self._rgb_to_oklab(*end_color)

        lightness = start_lightness + (end_lightness - start_lightness) * progress
        green_red = start_green_red + (end_green_red - start_green_red) * progress
        blue_yellow = start_blue_yellow + (end_blue_yellow - start_blue_yellow) * progress
        return self._oklab_to_rgb(lightness, green_red, blue_yellow)

    def _is_red_region(self, rgb_color):
        """Return True if an RGB color lies near the red hue region."""
        hue, saturation, value = colorsys.rgb_to_hsv(
            rgb_color[0] / 255,
            rgb_color[1] / 255,
            rgb_color[2] / 255,
        )
        hue_distance_to_red = min(abs(hue), abs(1.0 - hue))
        return hue_distance_to_red <= 0.09 and saturation >= 0.25 and value >= 0.1

    def _sample_loop_color_oklab(self, points, phase):
        """Sample a periodic color loop with explicit easing around red hues."""
        if not points:
            raise ValueError("No strobe color points are configured.")
        if len(points) == 1:
            return tuple(points[0])

        phase = phase % 1.0
        segment_count = len(points)

        segment_weights = []
        for segment_index in range(segment_count):
            start_is_red = self._is_red_region(points[segment_index])
            end_is_red = self._is_red_region(points[(segment_index + 1) % segment_count])
            weight = self._red_segment_weight if (start_is_red or end_is_red) else 1.0
            segment_weights.append(weight)

        total_weight = sum(segment_weights)
        weighted_phase = phase * total_weight

        segment_index = segment_count - 1
        segment_progress = 0.0
        accumulated_weight = 0.0
        for current_index, weight in enumerate(segment_weights):
            next_accumulated = accumulated_weight + weight
            if weighted_phase <= next_accumulated or current_index == segment_count - 1:
                segment_index = current_index
                segment_progress = (
                    (weighted_phase - accumulated_weight) / weight if weight > 0 else 0.0
                )
                break
            accumulated_weight = next_accumulated

        segment_progress = min(1.0, max(0.0, segment_progress))
        next_index = (segment_index + 1) % segment_count

        # Smoothstep easing removes abrupt velocity changes at point boundaries.
        eased_progress = segment_progress * segment_progress * (3 - 2 * segment_progress)

        start_is_red = self._is_red_region(points[segment_index])
        end_is_red = self._is_red_region(points[next_index])
        if start_is_red and not end_is_red:
            eased_progress = eased_progress ** self._red_edge_ease_power
        elif end_is_red and not start_is_red:
            eased_progress = 1.0 - ((1.0 - eased_progress) ** self._red_edge_ease_power)

        return self._interpolate_oklab_color(
            points[segment_index],
            points[next_index],
            eased_progress,
        )

    def _run_strobe_animation(self, strip):
        """Send the next frame for a strip running in strobe mode."""
        if strip.get("mode") != "strobe":
            self._stop_strip_animation(strip)
            return

        points = [tuple(point) for point in strip.get("strobe_points", [])]
        if len(points) < 2:
            self._stop_strip_animation(strip)
            return

        interval_ms = max(250, int(strip.get("strobe_interval_ms", 3000)))
        num_leds = strip["num_leds"]
        speed_leds = max(0.0, float(strip.get("strobe_speed_leds", 12.0)))
        base_phase = ((time.monotonic() - strip["animation_started_at"]) * 1000 / interval_ms) % 1.0

        try:
            if speed_leds == 0:
                red, green, blue = self._sample_loop_color_oklab(points, base_phase)
                self._send_full_color_update(strip, red, green, blue)
            else:
                led_colors = []
                for led_index in range(num_leds):
                    led_phase = base_phase - (led_index / speed_leds)
                    led_colors.append(self._sample_loop_color_oklab(points, led_phase))
                self._send_per_led_update(strip, led_colors)
        except (OSError, ValueError) as exc:
            self._stop_strip_animation(strip)
            messagebox.showerror(
                "Strobe failed",
                f"Stopped strobe for {strip['name']}.\n{exc}",
                parent=self,
            )
            return

        strip["animation_job"] = self.after(
            self._strobe_frame_ms,
            lambda current_strip=strip: self._run_strobe_animation(current_strip),
        )

    def _start_strobe_animation(self, strip):
        """Start looping a strip through its configured strobe color points."""
        points = [tuple(point) for point in strip.get("strobe_points", [])]
        if len(points) < 2:
            raise ValueError("Add at least two color points to run strobe mode.")

        interval_ms = int(strip.get("strobe_interval_ms", 3000))
        if not 250 <= interval_ms <= 60000:
            raise ValueError("Loop duration must be between 250 and 60000 milliseconds.")

        self._stop_strip_animation(strip)
        strip["animation_started_at"] = time.monotonic()
        self._run_strobe_animation(strip)

    def _get_default_speaker_loopback(self):
        """Return the default speaker loopback capture source."""
        if sc is None:
            raise ValueError(
                "Live volume mode requires 'soundcard'. Install it with: pip install soundcard"
            )

        try:
            speaker = sc.default_speaker()
        except Exception as exc:
            raise OSError(f"Failed to access the default speaker. {exc}") from exc

        if speaker is None:
            raise OSError("No default speaker is available.")

        speaker_identifiers = [
            getattr(speaker, "name", None),
            getattr(speaker, "id", None),
        ]
        for identifier in speaker_identifiers:
            if not identifier:
                continue
            try:
                loopback = sc.get_microphone(identifier, include_loopback=True)
                if loopback is not None:
                    return loopback
            except Exception:
                continue

        raise OSError("No loopback capture source was found for the default speaker.")

    def _speaker_volume_worker(self, strip, stop_event):
        """Capture default speaker loopback peak asynchronously and store normalized level."""
        try:
            loopback = self._get_default_speaker_loopback()
            with loopback.recorder(samplerate=self._live_volume_samplerate) as recorder:
                while not stop_event.is_set():
                    audio_block = recorder.record(numframes=self._live_volume_block_frames)
                    try:
                        peak_value = float(abs(audio_block).max())
                    except Exception:
                        peak_value = 0.0

                    gain = float(strip.get("live_volume_gain", 1.0))
                    strip["live_volume_level"] = min(1.0, max(0.0, peak_value * gain))
        except Exception as exc:
            strip["live_volume_level"] = 0.0
            strip["live_volume_error"] = str(exc)
            stop_event.set()

    def _run_live_volume_animation(self, strip):
        """Send the next frame for a strip running in live volume mode."""
        if strip.get("mode") != "live_volume":
            self._stop_strip_animation(strip)
            return

        live_volume_error = strip.get("live_volume_error")
        if live_volume_error:
            self._stop_strip_animation(strip)
            messagebox.showerror(
                "Live volume failed",
                f"Stopped live volume mode for {strip['name']}.\n{live_volume_error}",
                parent=self,
            )
            return

        volume_worker = strip.get("volume_worker")
        volume_stop_event = strip.get("volume_stop_event")
        if (
            volume_worker is None
            or volume_stop_event is None
            or volume_stop_event.is_set()
            or not volume_worker.is_alive()
        ):
            self._stop_strip_animation(strip)
            return

        target_level = min(1.0, max(0.0, float(strip.get("live_volume_level", 0.0))))
        # Exponent < 1 boosts low-to-mid peaks for a more sensitive meter.
        target_level = target_level ** self._live_volume_sensitivity_power
        smoothed_level = float(strip.get("live_volume_smoothed", 0.0))
        if target_level >= smoothed_level:
            smoothed_level = smoothed_level * 0.05 + target_level * 0.95
        else:
            smoothed_level = smoothed_level * 0.25 + target_level * 0.75
        strip["live_volume_smoothed"] = smoothed_level

        try:
            total_leds = strip["num_leds"]
            active_leds = int(strip.get("live_volume_led_count", total_leds))
            active_leds = min(max(1, active_leds), total_leds)
            led_colors = self._build_live_volume_led_colors(total_leds, active_leds, smoothed_level)
            self._send_per_led_update(strip, led_colors)
        except (OSError, ValueError) as exc:
            self._stop_strip_animation(strip)
            messagebox.showerror(
                "Live volume failed",
                f"Stopped live volume mode for {strip['name']}.\n{exc}",
                parent=self,
            )
            return

        strip["animation_job"] = self.after(
            self._live_volume_frame_ms,
            lambda current_strip=strip: self._run_live_volume_animation(current_strip),
        )

    def _start_live_volume_animation(self, strip):
        """Start speaker-signal-peak live volume mode for a strip."""
        if sc is None:
            raise ValueError(
                "Live volume mode requires 'soundcard'. Install it with: pip install soundcard"
            )

        gain = float(strip.get("live_volume_gain", 1.0))
        if not 0.1 <= gain <= 20.0:
            raise ValueError("Live volume gain must be between 0.1 and 20.0.")

        # Validate that loopback capture is available before starting the worker thread.
        self._get_default_speaker_loopback()

        self._stop_strip_animation(strip)
        strip["live_volume_level"] = 0.0
        strip["live_volume_smoothed"] = 0.0
        strip["live_volume_error"] = None

        stop_event = threading.Event()
        volume_worker = threading.Thread(
            target=self._speaker_volume_worker,
            args=(strip, stop_event),
            daemon=True,
            name=f"speaker-loopback-{strip['ip']}",
        )

        strip["volume_stop_event"] = stop_event
        strip["volume_worker"] = volume_worker
        strip["volume_stream"] = None
        volume_worker.start()
        self._run_live_volume_animation(strip)

    # ------------------------------------------------------------------
    # FFT spectrum mode
    # ------------------------------------------------------------------

    def _map_fft_to_leds(self, spectrum, fft_led_count, gain):
        """Map an RFFT magnitude spectrum to per-LED levels on a log scale.

        Covers 10 Hz – 10 kHz log-evenly across `fft_led_count` LEDs.
        Each LED's value is clamped to [0, 1] after the gain multiplier.
        """
        freq_min = 10.0
        freq_max = 10000.0
        full_fft_size = (len(spectrum) - 1) * 2
        freq_resolution = self._live_volume_samplerate / full_fft_size

        levels = []
        for i in range(fft_led_count):
            t_low = i / fft_led_count
            t_high = (i + 1) / fft_led_count
            f_low = freq_min * (freq_max / freq_min) ** t_low
            f_high = freq_min * (freq_max / freq_min) ** t_high

            bin_low = max(0, int(f_low / freq_resolution))
            bin_high = min(len(spectrum), int(f_high / freq_resolution) + 1)
            if bin_low >= len(spectrum):
                levels.append(0.0)
                continue
            if bin_low >= bin_high:
                bin_high = bin_low + 1

            level = float(np.mean(np.abs(spectrum[bin_low:bin_high]))) * gain
            levels.append(min(1.0, max(0.0, level)))

        return levels

    def _gaussian_blur_levels(self, levels, target_count):
        """Apply a small 1D Gaussian-like blur across neighboring FFT bands."""
        target_count = max(0, int(target_count))
        if target_count == 0:
            return []

        normalized = [0.0] * target_count
        for index in range(target_count):
            if index < len(levels):
                normalized[index] = min(1.0, max(0.0, float(levels[index])))

        if target_count == 1:
            return normalized

        # Pascal row [1, 4, 6, 4, 1] approximates a Gaussian kernel (sigma ~1).
        kernel = (1.0, 4.0, 6.0, 4.0, 1.0)
        radius = 2
        blurred = [0.0] * target_count

        for index in range(target_count):
            weighted_sum = 0.0
            total_weight = 0.0
            for kernel_offset, weight in enumerate(kernel):
                sample_index = index + kernel_offset - radius
                if 0 <= sample_index < target_count:
                    weighted_sum += normalized[sample_index] * weight
                    total_weight += weight

            blurred[index] = weighted_sum / total_weight if total_weight > 0 else normalized[index]

        return blurred

    def _speaker_fft_worker(self, strip, stop_event):
        """Capture speaker loopback, compute FFT, and store per-LED spectrum levels."""
        try:
            loopback = self._get_default_speaker_loopback()
            ring_buffer = np.zeros(self._fft_window_size, dtype=np.float32)
            window = np.hanning(self._fft_window_size).astype(np.float32)
            fft_led_count = int(strip.get("fft_led_count", strip["num_leds"]))

            with loopback.recorder(samplerate=self._live_volume_samplerate) as recorder:
                while not stop_event.is_set():
                    block = recorder.record(numframes=self._fft_block_frames)
                    mono = (
                        block.mean(axis=1).astype(np.float32)
                        if block.ndim > 1
                        else block.astype(np.float32)
                    )

                    ring_buffer = np.roll(ring_buffer, -self._fft_block_frames)
                    ring_buffer[-self._fft_block_frames:] = mono[: self._fft_block_frames]

                    spectrum = np.fft.rfft(ring_buffer * window) / self._fft_window_size
                    gain = float(strip.get("fft_gain", 2.5))
                    strip["fft_levels"] = self._map_fft_to_leds(spectrum, fft_led_count, gain)
        except Exception as exc:
            strip["fft_levels"] = [0.0] * int(strip.get("fft_led_count", strip["num_leds"]))
            strip["fft_error"] = str(exc)
            stop_event.set()

    def _run_fft_animation(self, strip):
        """Send the next frame for a strip running in FFT spectrum mode."""
        if strip.get("mode") != "fft_spectrum":
            self._stop_strip_animation(strip)
            return

        fft_error = strip.get("fft_error")
        if fft_error:
            self._stop_strip_animation(strip)
            messagebox.showerror(
                "Spectrum analyzer failed",
                f"Stopped spectrum analyzer for {strip['name']}.\n{fft_error}",
                parent=self,
            )
            return

        volume_worker = strip.get("volume_worker")
        volume_stop_event = strip.get("volume_stop_event")
        if (
            volume_worker is None
            or volume_stop_event is None
            or volume_stop_event.is_set()
            or not volume_worker.is_alive()
        ):
            self._stop_strip_animation(strip)
            return

        total_leds = strip["num_leds"]
        fft_led_count = min(max(1, int(strip.get("fft_led_count", total_leds))), total_leds)
        sustain_sec = max(0, int(strip.get("fft_sustain_ms", 0))) / 1000.0
        color_low = tuple(strip.get("fft_color_low", (0, 0, 255)))
        color_high = tuple(strip.get("fft_color_high", (255, 0, 0)))

        raw_levels = strip.get("fft_levels") or []
        blurred_levels = self._gaussian_blur_levels(raw_levels, fft_led_count)

        fft_smoothed = strip.get("fft_smoothed")
        if not isinstance(fft_smoothed, list) or len(fft_smoothed) != fft_led_count:
            fft_smoothed = [0.0] * fft_led_count
            strip["fft_smoothed"] = fft_smoothed

        fft_held_levels = strip.get("fft_held_levels")
        if not isinstance(fft_held_levels, list) or len(fft_held_levels) != fft_led_count:
            fft_held_levels = [0.0] * fft_led_count
            strip["fft_held_levels"] = fft_held_levels

        fft_held_until = strip.get("fft_held_until")
        if not isinstance(fft_held_until, list) or len(fft_held_until) != fft_led_count:
            fft_held_until = [0.0] * fft_led_count
            strip["fft_held_until"] = fft_held_until

        now = time.monotonic()
        led_colors = []

        for i in range(total_leds):
            if i >= fft_led_count:
                led_colors.append((0, 0, 0))
                continue

            raw = blurred_levels[i]

            prev = fft_smoothed[i]
            smoothed = prev * 0.05 + raw * 0.95 if raw >= prev else prev * 0.3 + raw * 0.7
            fft_smoothed[i] = smoothed

            if smoothed >= fft_held_levels[i]:
                # New peak — latch and restart hold timer.
                fft_held_levels[i] = smoothed
                fft_held_until[i] = now + sustain_sec
            elif now >= fft_held_until[i]:
                # Hold expired — exponentially release toward the live level (~24 ms half-life).
                fft_held_levels[i] = fft_held_levels[i] * 0.75 + smoothed * 0.25

            display_level = fft_held_levels[i]

            base_r, base_g, base_b = self._interpolate_oklab_color(color_low, color_high, display_level)

            led_colors.append((
                int(round(base_r * display_level)),
                int(round(base_g * display_level)),
                int(round(base_b * display_level)),
            ))

        try:
            self._send_per_led_update(strip, led_colors)
        except (OSError, ValueError) as exc:
            self._stop_strip_animation(strip)
            messagebox.showerror(
                "Spectrum analyzer failed",
                f"Stopped spectrum analyzer for {strip['name']}.\n{exc}",
                parent=self,
            )
            return

        strip["animation_job"] = self.after(
            self._fft_frame_ms,
            lambda current_strip=strip: self._run_fft_animation(current_strip),
        )

    def _start_fft_animation(self, strip):
        """Start FFT spectrum analyzer mode for a strip."""
        if sc is None or np is None:
            raise ValueError(
                "Spectrum analyzer mode requires 'soundcard' and 'numpy'.\n"
                "Install them with: pip install soundcard numpy"
            )

        fft_led_count = int(strip.get("fft_led_count", strip["num_leds"]))
        if not 1 <= fft_led_count <= strip["num_leds"]:
            raise ValueError(f"FFT LED count must be between 1 and {strip['num_leds']}.")

        gain = float(strip.get("fft_gain", 2.5))
        if not 0.1 <= gain <= 50.0:
            raise ValueError("FFT gain must be between 0.1 and 50.0.")

        self._get_default_speaker_loopback()

        self._stop_strip_animation(strip)
        strip["fft_levels"] = [0.0] * fft_led_count
        strip["fft_smoothed"] = [0.0] * fft_led_count
        strip["fft_held_levels"] = [0.0] * fft_led_count
        strip["fft_held_until"] = [0.0] * fft_led_count
        strip["fft_error"] = None

        stop_event = threading.Event()
        fft_worker = threading.Thread(
            target=self._speaker_fft_worker,
            args=(strip, stop_event),
            daemon=True,
            name=f"speaker-fft-{strip['ip']}",
        )
        strip["volume_stop_event"] = stop_event
        strip["volume_worker"] = fft_worker
        strip["volume_stream"] = None
        fft_worker.start()
        self._run_fft_animation(strip)

    def _open_mode_dialog(self, index):
        """Show the LED mode picker for a strip."""
        strip = self._strips[index]

        dialog = tk.Toplevel(self)
        dialog.title(f"{strip['name']} LED Modes")
        dialog.resizable(False, False)
        dialog.grab_set()
        dialog.columnconfigure(0, weight=1)

        ttk.Label(
            dialog,
            text=strip["name"],
            font=("Helvetica", 15, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(16, 4))

        ttk.Label(
            dialog,
            text=(
                f"IP: {strip['ip']}    "
                f"Port: {strip['port']}    "
                f"Data pin: {strip['data_pin']}"
            ),
            font=("Helvetica", 10),
        ).grid(row=1, column=0, sticky="w", padx=16, pady=(0, 12))

        ttk.Label(
            dialog,
            text="LED Mode",
            font=("Helvetica", 11, "bold"),
        ).grid(row=2, column=0, sticky="w", padx=16)

        mode_var = tk.StringVar(value=strip.get("mode", "full_color"))
        saved_color = strip.get("color", (255, 80, 0))
        red_var = tk.IntVar(value=saved_color[0])
        green_var = tk.IntVar(value=saved_color[1])
        blue_var = tk.IntVar(value=saved_color[2])
        strobe_points = [
            tuple(point)
            for point in strip.get(
                "strobe_points",
                [(255, 0, 0), (0, 255, 0), (0, 0, 255)],
            )
        ]
        strobe_interval_var = tk.StringVar(value=str(strip.get("strobe_interval_ms", 3000)))
        strobe_speed_var = tk.StringVar(value=str(strip.get("strobe_speed_leds", 12.0)))
        live_volume_gain_var = tk.StringVar(value=str(strip.get("live_volume_gain", 1.0)))
        live_volume_led_count_value = int(strip.get("live_volume_led_count", strip["num_leds"]))
        live_volume_led_count_value = min(max(1, live_volume_led_count_value), strip["num_leds"])
        live_volume_led_count_var = tk.StringVar(value=str(live_volume_led_count_value))
        fft_led_count_value = int(strip.get("fft_led_count", strip["num_leds"]))
        fft_led_count_value = min(max(1, fft_led_count_value), strip["num_leds"])
        fft_led_count_var = tk.StringVar(value=str(fft_led_count_value))
        fft_gain_var = tk.StringVar(value=str(strip.get("fft_gain", 2.5)))
        fft_sustain_var = tk.StringVar(value=str(strip.get("fft_sustain_ms", 0)))
        fft_color_low = list(strip.get("fft_color_low", (0, 0, 255)))
        fft_color_high = list(strip.get("fft_color_high", (255, 0, 0)))
        live_volume_worker = strip.get("volume_worker")
        if strip.get("mode") == "strobe" and strip.get("animation_job") is not None:
            status_var = tk.StringVar(
                value=(
                    f"Strobe is running with {len(strobe_points)} color points over "
                    f"{int(strobe_interval_var.get()) / 1000:.2f}s at "
                    f"{float(strobe_speed_var.get()):.2f} LEDs per one loop."
                )
            )
        elif strip.get("mode") == "live_volume" and live_volume_worker is not None and live_volume_worker.is_alive():
            status_var = tk.StringVar(
                value=(
                    f"Live volume is running with gain {float(live_volume_gain_var.get()):.1f}. "
                    f"Using {live_volume_led_count_value}/{strip['num_leds']} LEDs. "
                    "Zones: 70% green, 20% yellow, 10% red."
                )
            )
        elif strip.get("mode") == "fft_spectrum" and live_volume_worker is not None and live_volume_worker.is_alive():
            status_var = tk.StringVar(
                value=(
                    f"Spectrum analyzer is running \u2014 "
                    f"{fft_led_count_value}/{strip['num_leds']} LEDs, "
                    f"gain {float(strip.get('fft_gain', 2.5)):.1f}, "
                    f"sustain {int(strip.get('fft_sustain_ms', 0))}ms."
                )
            )
        else:
            status_var = tk.StringVar(value="Select a mode and configure its settings.")

        def _ask_color(initial_rgb, title):
            _, hex_color = colorchooser.askcolor(
                color=self._rgb_to_hex(*initial_rgb),
                parent=dialog,
                title=title,
            )
            if not hex_color:
                return None

            return (
                int(hex_color[1:3], 16),
                int(hex_color[3:5], 16),
                int(hex_color[5:7], 16),
            )

        def _parse_strobe_interval():
            try:
                interval_ms = int(strobe_interval_var.get())
            except (TypeError, ValueError, tk.TclError) as exc:
                raise ValueError(
                    "Loop duration must be an integer between 250 and 60000 milliseconds."
                ) from exc

            if not 250 <= interval_ms <= 60000:
                raise ValueError("Loop duration must be between 250 and 60000 milliseconds.")
            return interval_ms

        def _parse_strobe_speed():
            try:
                speed_leds = float(strobe_speed_var.get())
            except (TypeError, ValueError, tk.TclError) as exc:
                raise ValueError("Speed must be a number between 0 and 1000 LEDs per one loop.") from exc

            if not 0 <= speed_leds <= 1000:
                raise ValueError("Speed must be between 0 and 1000 LEDs per one loop.")
            return speed_leds

        def _parse_live_volume_gain():
            try:
                gain = float(live_volume_gain_var.get())
            except (TypeError, ValueError, tk.TclError) as exc:
                raise ValueError("Gain must be a number between 0.1 and 20.0.") from exc

            if not 0.1 <= gain <= 20.0:
                raise ValueError("Gain must be between 0.1 and 20.0.")
            return gain

        def _parse_live_volume_led_count():
            try:
                led_count = int(live_volume_led_count_var.get())
            except (TypeError, ValueError, tk.TclError) as exc:
                raise ValueError(
                    f"Live volume LED count must be an integer between 1 and {strip['num_leds']}."
                ) from exc

            if not 1 <= led_count <= strip["num_leds"]:
                raise ValueError(
                    f"Live volume LED count must be between 1 and {strip['num_leds']}."
                )
            return led_count

        def _parse_fft_led_count():
            try:
                count = int(fft_led_count_var.get())
            except (TypeError, ValueError, tk.TclError) as exc:
                raise ValueError(
                    f"FFT LED count must be an integer between 1 and {strip['num_leds']}."
                ) from exc
            if not 1 <= count <= strip["num_leds"]:
                raise ValueError(
                    f"FFT LED count must be between 1 and {strip['num_leds']}."
                )
            return count

        def _parse_fft_gain():
            try:
                gain = float(fft_gain_var.get())
            except (TypeError, ValueError, tk.TclError) as exc:
                raise ValueError("FFT gain must be a number between 0.1 and 50.0.") from exc
            if not 0.1 <= gain <= 50.0:
                raise ValueError("FFT gain must be between 0.1 and 50.0.")
            return gain

        def _parse_fft_sustain():
            try:
                sustain_ms = int(fft_sustain_var.get())
            except (TypeError, ValueError, tk.TclError) as exc:
                raise ValueError("FFT sustain must be an integer between 0 and 5000 ms.") from exc
            if not 0 <= sustain_ms <= 5000:
                raise ValueError("FFT sustain must be between 0 and 5000 ms.")
            return sustain_ms

        mode_options = [
            ("full_color", "Full Color", "Set every LED on the strip to one chosen color."),
            ("strobe", "Strobe", "Loop evenly spaced color points with smooth OKLab transitions."),
            (
                "live_volume",
                "Live Volume Level",
                "Tracks current default speaker signal peak with 70% green, 20% yellow, 10% red.",
            ),
            (
                "fft_spectrum",
                "Spectrum Analyzer",
                "Analyzes speaker loopback 10 Hz \u2013 10 kHz (log scale) with a 2-color OKLab blend.",
            ),
        ]

        modes_frame = ttk.Frame(dialog)
        modes_frame.grid(row=3, column=0, sticky="ew", padx=16, pady=(8, 12))
        modes_frame.columnconfigure(0, weight=1)

        for row_index, (value, title, subtitle) in enumerate(mode_options):
            option_frame = ttk.Frame(modes_frame, relief="solid", borderwidth=1)
            option_frame.grid(row=row_index, column=0, sticky="ew", pady=4)
            option_frame.columnconfigure(0, weight=1)

            ttk.Radiobutton(
                option_frame,
                text=title,
                value=value,
                variable=mode_var,
                command=lambda: _render_mode_controls(),
            ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 2))

            ttk.Label(
                option_frame,
                text=subtitle,
                font=("Helvetica", 9),
            ).grid(row=1, column=0, sticky="w", padx=38, pady=(0, 10))

        controls_frame = ttk.Frame(dialog, relief="solid", borderwidth=1)
        controls_frame.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 12))
        controls_frame.columnconfigure(0, weight=1)

        def _render_mode_controls():
            for widget in controls_frame.winfo_children():
                widget.destroy()

            selected_mode = mode_var.get()
            if selected_mode == "full_color":
                ttk.Label(
                    controls_frame,
                    text="Full Color Controls",
                    font=("Helvetica", 11, "bold"),
                ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))

                ttk.Label(
                    controls_frame,
                    text="Pick a color and apply it to every LED on the strip.",
                    font=("Helvetica", 9),
                ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 10))

                picker_frame = ttk.Frame(controls_frame)
                picker_frame.grid(row=2, column=0, sticky="ew", padx=12)
                picker_frame.columnconfigure(1, weight=1)

                preview_swatch = tk.Frame(
                    picker_frame,
                    width=72,
                    height=72,
                    relief="solid",
                    borderwidth=1,
                )
                preview_swatch.grid(row=0, column=0, rowspan=4, padx=(0, 12), pady=(0, 8))
                preview_swatch.grid_propagate(False)

                selected_label = ttk.Label(
                    picker_frame,
                    font=("Helvetica", 9),
                )
                selected_label.grid(row=0, column=1, sticky="w")

                output_label = ttk.Label(
                    picker_frame,
                    font=("Helvetica", 11, "bold"),
                )
                output_label.grid(row=1, column=1, sticky="w")

                rgb_label = ttk.Label(
                    picker_frame,
                    font=("Helvetica", 9),
                )
                rgb_label.grid(row=2, column=1, sticky="w", pady=(2, 8))

                def _update_preview():
                    selected_hex = self._rgb_to_hex(red_var.get(), green_var.get(), blue_var.get())
                    output_red, output_green, output_blue = self._enhance_color(
                        red_var.get(),
                        green_var.get(),
                        blue_var.get(),
                    )
                    output_hex = self._rgb_to_hex(output_red, output_green, output_blue)
                    preview_swatch.configure(bg=output_hex)
                    selected_label.configure(text=f"Selected {selected_hex.upper()}")
                    output_label.configure(text=f"Output {output_hex.upper()}")
                    rgb_label.configure(
                        text=(
                            f"Sent R {output_red}    "
                            f"G {output_green}    "
                            f"B {output_blue}"
                        )
                    )

                def _choose_color():
                    selected_rgb = _ask_color(
                        (red_var.get(), green_var.get(), blue_var.get()),
                        f"Choose a color for {strip['name']}",
                    )
                    if selected_rgb is None:
                        return

                    red_var.set(selected_rgb[0])
                    green_var.set(selected_rgb[1])
                    blue_var.set(selected_rgb[2])
                    _update_preview()

                ttk.Button(
                    picker_frame,
                    text="Choose Color...",
                    command=_choose_color,
                ).grid(row=3, column=1, sticky="w")

                presets_frame = ttk.Frame(controls_frame)
                presets_frame.grid(row=3, column=0, sticky="w", padx=12, pady=(4, 10))

                ttk.Label(
                    presets_frame,
                    text="Presets:",
                    font=("Helvetica", 9, "bold"),
                ).grid(row=0, column=0, padx=(0, 6))

                preset_colors = [
                    ("Amber", (255, 80, 0)),
                    ("Red", (255, 0, 0)),
                    ("Green", (0, 255, 0)),
                    ("Blue", (0, 0, 255)),
                    ("Purple", (180, 0, 255)),
                    ("White", (255, 255, 255)),
                ]
                for preset_index, (label, rgb) in enumerate(preset_colors, start=1):
                    tk.Button(
                        presets_frame,
                        text=label,
                        width=7,
                        bg=self._rgb_to_hex(*rgb),
                        fg="black",
                        relief="raised",
                        command=lambda rgb_value=rgb: _set_color(*rgb_value),
                    ).grid(row=0, column=preset_index, padx=2)

                sliders_frame = ttk.Frame(controls_frame)
                sliders_frame.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 12))
                sliders_frame.columnconfigure(1, weight=1)

                def _set_color(red, green, blue):
                    red_var.set(red)
                    green_var.set(green)
                    blue_var.set(blue)
                    _update_preview()

                def _add_slider(row_index, label, variable):
                    ttk.Label(sliders_frame, text=label, font=("Helvetica", 9, "bold")).grid(
                        row=row_index, column=0, sticky="w", padx=(0, 10), pady=4
                    )
                    tk.Scale(
                        sliders_frame,
                        from_=0,
                        to=255,
                        orient="horizontal",
                        variable=variable,
                        showvalue=True,
                        length=320,
                        command=lambda _value: _update_preview(),
                    ).grid(row=row_index, column=1, sticky="ew", pady=4)

                _add_slider(0, "Red", red_var)
                _add_slider(1, "Green", green_var)
                _add_slider(2, "Blue", blue_var)
                _update_preview()
                return

            if selected_mode == "strobe":
                ttk.Label(
                    controls_frame,
                    text="Strobe Controls",
                    font=("Helvetica", 11, "bold"),
                ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))

                ttk.Label(
                    controls_frame,
                    text=(
                        "Add color points in loop order. The software spaces them evenly "
                        "across the loop duration and blends them in OKLab."
                    ),
                    font=("Helvetica", 9),
                ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 10))

                timing_frame = ttk.Frame(controls_frame)
                timing_frame.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))

                ttk.Label(
                    timing_frame,
                    text="Loop duration (ms)",
                    font=("Helvetica", 9, "bold"),
                ).grid(row=0, column=0, sticky="w", padx=(0, 10))

                interval_spinbox = tk.Spinbox(
                    timing_frame,
                    from_=250,
                    to=60000,
                    increment=250,
                    textvariable=strobe_interval_var,
                    width=10,
                )
                interval_spinbox.grid(row=0, column=1, sticky="w")

                ttk.Label(
                    timing_frame,
                    text="Speed (LEDs per one loop)",
                    font=("Helvetica", 9, "bold"),
                ).grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(6, 0))

                speed_spinbox = tk.Spinbox(
                    timing_frame,
                    from_=0,
                    to=1000,
                    increment=0.5,
                    textvariable=strobe_speed_var,
                    width=10,
                    format="%.1f",
                )
                speed_spinbox.grid(row=1, column=1, sticky="w", pady=(6, 0))

                points_frame = ttk.Frame(controls_frame)
                points_frame.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 10))
                points_frame.columnconfigure(0, weight=1)

                points_listbox = tk.Listbox(
                    points_frame,
                    height=6,
                    width=40,
                    exportselection=False,
                    activestyle="none",
                )
                points_listbox.grid(row=0, column=0, rowspan=4, sticky="nsew", padx=(0, 12))

                point_preview_frame = ttk.Frame(points_frame)
                point_preview_frame.grid(row=0, column=1, sticky="nw")

                point_swatch = tk.Frame(
                    point_preview_frame,
                    width=72,
                    height=72,
                    relief="solid",
                    borderwidth=1,
                )
                point_swatch.grid(row=0, column=0, pady=(0, 8), sticky="w")
                point_swatch.grid_propagate(False)

                point_hex_label = ttk.Label(
                    point_preview_frame,
                    font=("Helvetica", 11, "bold"),
                )
                point_hex_label.grid(row=1, column=0, sticky="w")

                point_rgb_label = ttk.Label(
                    point_preview_frame,
                    font=("Helvetica", 9),
                )
                point_rgb_label.grid(row=2, column=0, sticky="w", pady=(2, 8))

                summary_var = tk.StringVar()

                def _selected_point_index():
                    selection = points_listbox.curselection()
                    return selection[0] if selection else None

                def _update_strobe_summary():
                    try:
                        interval_ms = _parse_strobe_interval()
                    except ValueError:
                        summary_var.set("Loop duration must be between 250 and 60000 ms.")
                        return

                    try:
                        speed_leds = _parse_strobe_speed()
                    except ValueError:
                        summary_var.set("Speed must be between 0 and 1000 LEDs per one loop.")
                        return

                    if len(strobe_points) < 2:
                        summary_var.set("Add at least two color points to run strobe mode.")
                        return

                    segment_ms = interval_ms / len(strobe_points)
                    if speed_leds == 0:
                        summary_var.set(
                            f"{len(strobe_points)} points, {segment_ms:.0f} ms between points, uniform color across strip."
                        )
                    else:
                        summary_var.set(
                            f"{len(strobe_points)} points, {segment_ms:.0f} ms between points, moves {speed_leds:.2f} LEDs per one loop."
                        )

                def _update_point_preview(_event=None):
                    selected_index = _selected_point_index()
                    if selected_index is None or selected_index >= len(strobe_points):
                        point_swatch.configure(bg="#202020")
                        point_hex_label.configure(text="No color point selected")
                        point_rgb_label.configure(text="Select or add a point to preview it.")
                        return

                    red, green, blue = strobe_points[selected_index]
                    point_swatch.configure(bg=self._rgb_to_hex(red, green, blue))
                    point_hex_label.configure(text=self._rgb_to_hex(red, green, blue).upper())
                    point_rgb_label.configure(text=f"R {red}    G {green}    B {blue}")

                def _refresh_strobe_points(preferred_index=None):
                    points_listbox.delete(0, tk.END)
                    for point_index, rgb in enumerate(strobe_points, start=1):
                        points_listbox.insert(
                            tk.END,
                            (
                                f"{point_index}. {self._rgb_to_hex(*rgb).upper()}    "
                                f"R {rgb[0]} G {rgb[1]} B {rgb[2]}"
                            ),
                        )

                    points_listbox.selection_clear(0, tk.END)
                    if strobe_points:
                        if preferred_index is None:
                            preferred_index = 0
                        preferred_index = min(preferred_index, len(strobe_points) - 1)
                        points_listbox.selection_set(preferred_index)
                        points_listbox.activate(preferred_index)

                    _update_strobe_summary()
                    _update_point_preview()

                def _add_strobe_point():
                    selected_rgb = _ask_color((255, 0, 0), f"Add a strobe color point for {strip['name']}")
                    if selected_rgb is None:
                        return
                    strobe_points.append(selected_rgb)
                    _refresh_strobe_points(len(strobe_points) - 1)

                def _edit_strobe_point():
                    selected_index = _selected_point_index()
                    if selected_index is None:
                        messagebox.showerror(
                            "No color point selected",
                            "Select a strobe color point to edit.",
                            parent=dialog,
                        )
                        return

                    selected_rgb = _ask_color(
                        strobe_points[selected_index],
                        f"Edit strobe point {selected_index + 1} for {strip['name']}",
                    )
                    if selected_rgb is None:
                        return
                    strobe_points[selected_index] = selected_rgb
                    _refresh_strobe_points(selected_index)

                def _remove_strobe_point():
                    selected_index = _selected_point_index()
                    if selected_index is None:
                        messagebox.showerror(
                            "No color point selected",
                            "Select a strobe color point to remove.",
                            parent=dialog,
                        )
                        return

                    del strobe_points[selected_index]
                    next_index = selected_index if selected_index < len(strobe_points) else len(strobe_points) - 1
                    _refresh_strobe_points(next_index if strobe_points else None)

                ttk.Button(
                    point_preview_frame,
                    text="Add Color...",
                    command=_add_strobe_point,
                ).grid(row=3, column=0, sticky="ew", pady=(0, 4))

                ttk.Button(
                    point_preview_frame,
                    text="Edit Selected...",
                    command=_edit_strobe_point,
                ).grid(row=4, column=0, sticky="ew", pady=(0, 4))

                ttk.Button(
                    point_preview_frame,
                    text="Remove Selected",
                    command=_remove_strobe_point,
                ).grid(row=5, column=0, sticky="ew")

                ttk.Label(
                    controls_frame,
                    textvariable=summary_var,
                    font=("Helvetica", 9),
                ).grid(row=4, column=0, sticky="w", padx=12, pady=(0, 12))

                points_listbox.bind("<<ListboxSelect>>", _update_point_preview)
                interval_spinbox.bind("<KeyRelease>", lambda _event: _update_strobe_summary())
                interval_spinbox.bind("<FocusOut>", lambda _event: _update_strobe_summary())
                speed_spinbox.bind("<KeyRelease>", lambda _event: _update_strobe_summary())
                speed_spinbox.bind("<FocusOut>", lambda _event: _update_strobe_summary())
                _refresh_strobe_points()
                return

            if selected_mode == "live_volume":
                ttk.Label(
                    controls_frame,
                    text="Live Volume Controls",
                    font=("Helvetica", 11, "bold"),
                ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))

                ttk.Label(
                    controls_frame,
                    text=(
                        "Uses loopback capture of your current default speaker signal at about 125 FPS."
                    ),
                    font=("Helvetica", 9),
                ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 10))

                zones_frame = ttk.Frame(controls_frame)
                zones_frame.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))
                zones_frame.columnconfigure(1, weight=1)

                ttk.Label(
                    zones_frame,
                    text="Live meter LEDs",
                    font=("Helvetica", 9, "bold"),
                ).grid(row=0, column=0, sticky="w", padx=(0, 10))

                led_count_spinbox = tk.Spinbox(
                    zones_frame,
                    from_=1,
                    to=strip["num_leds"],
                    increment=1,
                    textvariable=live_volume_led_count_var,
                    width=8,
                )
                led_count_spinbox.grid(row=0, column=1, sticky="w")

                zone_summary_var = tk.StringVar()

                def _update_live_zone_summary(_event=None):
                    try:
                        selected_count = _parse_live_volume_led_count()
                    except ValueError:
                        zone_summary_var.set(
                            f"LED count must be between 1 and {strip['num_leds']}."
                        )
                        return

                    green_count, yellow_count, red_count = self._volume_zone_counts(selected_count)
                    zone_summary_var.set(
                        f"Zone split for {selected_count} LEDs: "
                        f"{green_count} green, {yellow_count} yellow, {red_count} red"
                    )

                ttk.Label(
                    zones_frame,
                    textvariable=zone_summary_var,
                    font=("Helvetica", 9),
                ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

                led_count_spinbox.bind("<KeyRelease>", _update_live_zone_summary)
                led_count_spinbox.bind("<FocusOut>", _update_live_zone_summary)
                _update_live_zone_summary()

                gain_frame = ttk.Frame(controls_frame)
                gain_frame.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))
                gain_frame.columnconfigure(1, weight=1)

                ttk.Label(
                    gain_frame,
                    text="Speaker gain",
                    font=("Helvetica", 9, "bold"),
                ).grid(row=0, column=0, sticky="w", padx=(0, 10))

                gain_scale = tk.Scale(
                    gain_frame,
                    from_=0.1,
                    to=20.0,
                    resolution=0.1,
                    orient="horizontal",
                    length=340,
                    variable=live_volume_gain_var,
                    showvalue=True,
                )
                gain_scale.grid(row=0, column=1, sticky="ew")

                ttk.Label(
                    controls_frame,
                    text=(
                        "Gain multiplies speaker signal peak before mapping to LEDs (1.0 = direct loopback peak)."
                    ),
                    font=("Helvetica", 9),
                ).grid(row=4, column=0, sticky="w", padx=12, pady=(0, 12))
                ttk.Label(
                    controls_frame,
                    text=(
                        "Sensitivity boost is enabled for low peaks so quiet content still moves the meter."
                    ),
                    font=("Helvetica", 9),
                ).grid(row=5, column=0, sticky="w", padx=12, pady=(0, 12))
                return

            if selected_mode == "fft_spectrum":
                ttk.Label(
                    controls_frame,
                    text="Spectrum Analyzer Controls",
                    font=("Helvetica", 11, "bold"),
                ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))

                ttk.Label(
                    controls_frame,
                    text=(
                        "Loopback audio is analyzed from 10 Hz to 10 kHz on a logarithmic scale. "
                        "Each LED represents a frequency band; brightness shows amplitude."
                    ),
                    font=("Helvetica", 9),
                ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 10))

                spectrum_frame = ttk.Frame(controls_frame)
                spectrum_frame.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))
                spectrum_frame.columnconfigure(1, weight=1)

                ttk.Label(
                    spectrum_frame,
                    text="Spectrum LEDs",
                    font=("Helvetica", 9, "bold"),
                ).grid(row=0, column=0, sticky="w", padx=(0, 10))

                fft_led_count_spinbox = tk.Spinbox(
                    spectrum_frame,
                    from_=1,
                    to=strip["num_leds"],
                    increment=1,
                    textvariable=fft_led_count_var,
                    width=8,
                )
                fft_led_count_spinbox.grid(row=0, column=1, sticky="w")

                ttk.Label(
                    spectrum_frame,
                    text="Gain",
                    font=("Helvetica", 9, "bold"),
                ).grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(8, 0))

                fft_gain_scale = tk.Scale(
                    spectrum_frame,
                    from_=0.1,
                    to=50.0,
                    resolution=0.1,
                    orient="horizontal",
                    length=300,
                    variable=fft_gain_var,
                    showvalue=True,
                )
                fft_gain_scale.grid(row=1, column=1, sticky="ew", pady=(8, 0))

                ttk.Label(
                    spectrum_frame,
                    text="Sustain (ms)",
                    font=("Helvetica", 9, "bold"),
                ).grid(row=2, column=0, sticky="w", padx=(0, 10), pady=(8, 0))

                fft_sustain_spinbox = tk.Spinbox(
                    spectrum_frame,
                    from_=0,
                    to=5000,
                    increment=50,
                    textvariable=fft_sustain_var,
                    width=8,
                )
                fft_sustain_spinbox.grid(row=2, column=1, sticky="w", pady=(8, 0))

                colors_frame = ttk.Frame(controls_frame)
                colors_frame.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))

                ttk.Label(
                    colors_frame,
                    text="Low frequency color",
                    font=("Helvetica", 9, "bold"),
                ).grid(row=0, column=0, sticky="w", padx=(0, 10))

                low_swatch = tk.Frame(
                    colors_frame,
                    width=40,
                    height=20,
                    relief="solid",
                    borderwidth=1,
                    bg=self._rgb_to_hex(*fft_color_low),
                )
                low_swatch.grid(row=0, column=1, padx=(0, 8))
                low_swatch.grid_propagate(False)

                def _choose_fft_color_low():
                    selected_rgb = _ask_color(
                        tuple(fft_color_low),
                        f"Choose low-frequency color for {strip['name']}",
                    )
                    if selected_rgb is None:
                        return
                    fft_color_low[:] = list(selected_rgb)
                    low_swatch.configure(bg=self._rgb_to_hex(*selected_rgb))

                ttk.Button(
                    colors_frame,
                    text="Choose...",
                    command=_choose_fft_color_low,
                ).grid(row=0, column=2)

                ttk.Label(
                    colors_frame,
                    text="High frequency color",
                    font=("Helvetica", 9, "bold"),
                ).grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(8, 0))

                high_swatch = tk.Frame(
                    colors_frame,
                    width=40,
                    height=20,
                    relief="solid",
                    borderwidth=1,
                    bg=self._rgb_to_hex(*fft_color_high),
                )
                high_swatch.grid(row=1, column=1, padx=(0, 8), pady=(8, 0))
                high_swatch.grid_propagate(False)

                def _choose_fft_color_high():
                    selected_rgb = _ask_color(
                        tuple(fft_color_high),
                        f"Choose high-frequency color for {strip['name']}",
                    )
                    if selected_rgb is None:
                        return
                    fft_color_high[:] = list(selected_rgb)
                    high_swatch.configure(bg=self._rgb_to_hex(*selected_rgb))

                ttk.Button(
                    colors_frame,
                    text="Choose...",
                    command=_choose_fft_color_high,
                ).grid(row=1, column=2, pady=(8, 0))

                return

            placeholder_text = {
                "placeholder_cycle": "Color cycle controls will go here.",
            }.get(selected_mode, "Mode controls will go here.")

            ttk.Label(
                controls_frame,
                text="Placeholder",
                font=("Helvetica", 11, "bold"),
            ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))

            ttk.Label(
                controls_frame,
                text=placeholder_text,
                font=("Helvetica", 9),
            ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 12))

        _render_mode_controls()

        def _on_apply():
            if mode_var.get() == "full_color":
                red = red_var.get()
                green = green_var.get()
                blue = blue_var.get()
                self._stop_strip_animation(strip)
                try:
                    output_red, output_green, output_blue = self._send_full_color_update(
                        strip,
                        red,
                        green,
                        blue,
                    )
                except (OSError, ValueError) as exc:
                    messagebox.showerror(
                        "Full color failed",
                        f"Failed to update {strip['name']}.\n{exc}",
                        parent=dialog,
                    )
                    return

                strip["mode"] = "full_color"
                strip["color"] = (red, green, blue)
                selected_hex = self._rgb_to_hex(red, green, blue).upper()
                output_hex = self._rgb_to_hex(output_red, output_green, output_blue).upper()
                if (output_red, output_green, output_blue) == (red, green, blue):
                    status_var.set(f"Applied {output_hex} to {strip['num_leds']} LEDs.")
                else:
                    status_var.set(
                        f"Applied {selected_hex} as {output_hex} to {strip['num_leds']} LEDs."
                    )
                self._save_settings()
                return

            if mode_var.get() == "strobe":
                try:
                    interval_ms = _parse_strobe_interval()
                except ValueError as exc:
                    messagebox.showerror("Invalid strobe interval", str(exc), parent=dialog)
                    return

                try:
                    speed_leds = _parse_strobe_speed()
                except ValueError as exc:
                    messagebox.showerror("Invalid strobe speed", str(exc), parent=dialog)
                    return

                if len(strobe_points) < 2:
                    messagebox.showerror(
                        "Too few color points",
                        "Add at least two color points to run strobe mode.",
                        parent=dialog,
                    )
                    return

                previous_mode = strip.get("mode", "full_color")
                strip["mode"] = "strobe"
                strip["strobe_points"] = [tuple(point) for point in strobe_points]
                strip["strobe_interval_ms"] = interval_ms
                strip["strobe_speed_leds"] = speed_leds
                try:
                    self._start_strobe_animation(strip)
                except (OSError, ValueError) as exc:
                    strip["mode"] = previous_mode
                    messagebox.showerror(
                        "Strobe failed",
                        f"Failed to start strobe on {strip['name']}.\n{exc}",
                        parent=dialog,
                    )
                    return

                if speed_leds == 0:
                    status_var.set(
                        f"Running strobe with {len(strobe_points)} points over {interval_ms / 1000:.2f}s, uniform across strip."
                    )
                else:
                    status_var.set(
                        f"Running strobe with {len(strobe_points)} points over {interval_ms / 1000:.2f}s at {speed_leds:.2f} LEDs per one loop."
                    )
                self._save_settings()
                return

            if mode_var.get() == "live_volume":
                try:
                    gain = _parse_live_volume_gain()
                except ValueError as exc:
                    messagebox.showerror("Invalid live volume gain", str(exc), parent=dialog)
                    return

                try:
                    live_volume_led_count = _parse_live_volume_led_count()
                except ValueError as exc:
                    messagebox.showerror("Invalid live volume LED count", str(exc), parent=dialog)
                    return

                previous_mode = strip.get("mode", "full_color")
                strip["mode"] = "live_volume"
                strip["live_volume_gain"] = gain
                strip["live_volume_led_count"] = live_volume_led_count
                try:
                    self._start_live_volume_animation(strip)
                except (OSError, ValueError) as exc:
                    strip["mode"] = previous_mode
                    messagebox.showerror(
                        "Live volume failed",
                        f"Failed to start live volume mode on {strip['name']}.\n{exc}",
                        parent=dialog,
                    )
                    return

                status_var.set(
                    f"Running live volume with gain {gain:.1f} using {live_volume_led_count}/{strip['num_leds']} LEDs. "
                    "Zones: 70% green, 20% yellow, 10% red."
                )
                self._save_settings()
                return

            if mode_var.get() == "fft_spectrum":
                try:
                    fft_led_cnt = _parse_fft_led_count()
                except ValueError as exc:
                    messagebox.showerror("Invalid FFT LED count", str(exc), parent=dialog)
                    return

                try:
                    gain = _parse_fft_gain()
                except ValueError as exc:
                    messagebox.showerror("Invalid FFT gain", str(exc), parent=dialog)
                    return

                try:
                    sustain_ms = _parse_fft_sustain()
                except ValueError as exc:
                    messagebox.showerror("Invalid FFT sustain", str(exc), parent=dialog)
                    return

                previous_mode = strip.get("mode", "full_color")
                strip["mode"] = "fft_spectrum"
                strip["fft_led_count"] = fft_led_cnt
                strip["fft_gain"] = gain
                strip["fft_sustain_ms"] = sustain_ms
                strip["fft_color_low"] = tuple(fft_color_low)
                strip["fft_color_high"] = tuple(fft_color_high)
                try:
                    self._start_fft_animation(strip)
                except (OSError, ValueError) as exc:
                    strip["mode"] = previous_mode
                    messagebox.showerror(
                        "Spectrum analyzer failed",
                        f"Failed to start spectrum analyzer on {strip['name']}.\n{exc}",
                        parent=dialog,
                    )
                    return

                status_var.set(
                    f"Running spectrum analyzer — {fft_led_cnt}/{strip['num_leds']} LEDs, "
                    f"gain {gain:.1f}, sustain {sustain_ms}ms."
                )
                self._save_settings()
                return

            messagebox.showinfo(
                "Placeholder",
                f"Selected mode: {mode_var.get()}\n\nMode controls are placeholders for now.",
                parent=dialog,
            )

        actions_frame = ttk.Frame(dialog)
        actions_frame.grid(row=6, column=0, sticky="ew", padx=16, pady=(0, 16))
        actions_frame.columnconfigure(0, weight=1)

        ttk.Label(
            actions_frame,
            textvariable=status_var,
            font=("Helvetica", 9),
        ).grid(row=0, column=0, sticky="w", padx=(0, 12))

        ttk.Button(actions_frame, text="Close", command=dialog.destroy).grid(
            row=0, column=1, padx=(0, 8)
        )
        ttk.Button(actions_frame, text="Apply", command=_on_apply).grid(
            row=0, column=2
        )

    def _parse_pong_packet(self, packet):
        """Parse and validate the pong packet returned by a strip."""
        if len(packet) < 5:
            raise ValueError("Packet is too short.")

        num_leds = (packet[0] << 8) | packet[1]
        data_pin = packet[2]
        name_len = packet[3]
        expected_len = 5 + name_len
        if len(packet) != expected_len:
            raise ValueError(
                f"Unexpected packet size ({len(packet)} bytes, expected {expected_len})."
            )

        name_bytes = packet[4 : 4 + name_len]
        if packet[4 + name_len] != 0:
            raise ValueError("Packet trailer byte is invalid.")

        if b"\x00" in name_bytes:
            name_bytes = name_bytes.split(b"\x00", 1)[0]

        if not name_bytes:
            raise ValueError("Device name is empty.")

        try:
            name = name_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Device name is not valid UTF-8.") from exc

        return {
            "name": name,
            "num_leds": num_leds,
            "data_pin": data_pin,
        }

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
                parsed_ip = ipaddress.ip_address(ip)
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

            family = socket.AF_INET6 if parsed_ip.version == 6 else socket.AF_INET
            target = (ip, port_int, 0, 0) if family == socket.AF_INET6 else (ip, port_int)
            try:
                with socket.socket(family, socket.SOCK_DGRAM) as udp_socket:
                    udp_socket.settimeout(1.0)
                    udp_socket.sendto(b"p", target)
                    response, _ = udp_socket.recvfrom(1024)
                    if not response:
                        raise TimeoutError

            except (socket.timeout, TimeoutError):
                messagebox.showerror(
                    "No response",
                    f"No response was received from {ip}:{port_int}.",
                    parent=dialog,
                )
                return
            except OSError as exc:
                messagebox.showerror(
                    "UDP error",
                    f"Failed to contact {ip}:{port_int} over UDP.\n{exc}",
                    parent=dialog,
                )
                return

            try:
                pong_data = self._parse_pong_packet(response)
            except ValueError as exc:
                messagebox.showerror(
                    "Invalid response",
                    f"{ip}:{port_int} returned an invalid pong packet.\n{exc}",
                    parent=dialog,
                )
                return

            self._add_strip(
                name=pong_data["name"],
                ip=ip,
                port=str(port_int),
                num_leds=pong_data["num_leds"],
                data_pin=pong_data["data_pin"],
            )
            dialog.destroy()

        ttk.Button(dialog, text="OK", command=_on_ok).grid(
            row=3, column=0, columnspan=2, pady=(12, 16)
        )

    def _add_strip(self, name, ip, port, num_leds, data_pin):
        """Save a validated strip entry and refresh the list view."""
        self._strips.append(
            self._build_strip_record(
                name=name,
                ip=ip,
                port=port,
                num_leds=num_leds,
                data_pin=data_pin,
            )
        )
        self._save_settings()
        self._refresh_list()
