import argparse
from collections.abc import Iterable
import ctypes
import logging
from pathlib import Path
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import traceback

from ez_reset.d4 import D4ControlBackend
from ez_reset.devices import by_model
from ez_reset.exceptions import DeviceError, VerificationError
from ez_reset.printer import Printer
from ez_reset.status import InkColor, InkLevel
from ez_reset.utils import parse_identifier
from ez_reset.win_usbprint import USBPRINTTransport, enumerate_printers

logger = logging.getLogger("ez_reset")


def get_icon_paths() -> tuple[Path | None, Path | None, Path | None]:
    """Find paths to (icon.ico, icon.png, icon_42.png) from assets folder across frozen/dev environments."""
    search_dirs = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        meipass = Path(sys._MEIPASS)
        search_dirs.extend([meipass / "assets", meipass])
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        search_dirs.extend([exe_dir / "assets", exe_dir])

    src_parent = Path(__file__).resolve().parent
    search_dirs.extend([
        src_parent.parent.parent / "assets",
        src_parent.parent / "assets",
        src_parent / "assets",
    ])

    found_ico = None
    found_png = None
    found_thumb = None
    for d in search_dirs:
        if not found_ico and (d / "icon.ico").exists():
            found_ico = d / "icon.ico"
        if not found_png and (d / "icon.png").exists():
            found_png = d / "icon.png"
        if not found_thumb and (d / "icon_42.png").exists():
            found_thumb = d / "icon_42.png"

    if not found_thumb:
        found_thumb = found_png

    return found_ico, found_png, found_thumb


_GLOBAL_ICON_PHOTO: tk.PhotoImage | None = None


def apply_window_icon(win: tk.Misc) -> None:
    """Apply the application icon to a Tk or Toplevel window."""
    global _GLOBAL_ICON_PHOTO
    ico_path, png_path, _thumb = get_icon_paths()

    if ico_path:
        try:
            if isinstance(win, tk.Tk):
                win.iconbitmap(default=str(ico_path))
            else:
                win.iconbitmap(str(ico_path))
        except Exception as e:
            logger.debug("iconbitmap error: %s", e)

    if png_path:
        try:
            if _GLOBAL_ICON_PHOTO is None:
                _GLOBAL_ICON_PHOTO = tk.PhotoImage(file=str(png_path))
            win.iconphoto(True, _GLOBAL_ICON_PHOTO)
        except Exception as e:
            logger.debug("iconphoto error: %s", e)


class PrinterList(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)

        self._printers: list[str] = []

        # Header branding with logo
        top_bar = ttk.Frame(self)
        top_bar.pack(fill="x", padx=6, pady=(6, 4))

        self._thumb_img = None
        _ico, _png, thumb_path = get_icon_paths()
        if thumb_path:
            try:
                self._thumb_img = tk.PhotoImage(file=str(thumb_path))
                lbl_icon = ttk.Label(top_bar, image=self._thumb_img)
                lbl_icon.pack(side="left", padx=(0, 8))
            except Exception:
                pass

        title_box = ttk.Frame(top_bar)
        title_box.pack(side="left", fill="both", expand=True)

        lbl_app = ttk.Label(title_box, text="EZ-Reset Engine", font=("Segoe UI", 11, "bold"))
        lbl_app.pack(anchor="w")

        lbl_sub = ttk.Label(title_box, text="Universal Waste Ink Resetter for Epson (L5190 & L5290 Series)", font=("Segoe UI", 8))
        lbl_sub.pack(anchor="w")

        header = ttk.Label(self, text="Detected USB Printers (Double-click to open):", font=("Segoe UI", 9, "bold"))
        header.pack(fill="x", padx=6, pady=(4, 2))

        self.list = tk.Listbox(self, font=("Consolas", 10), height=8)
        self.list.pack(fill=tk.BOTH, expand=True, padx=6, pady=2)
        self.list.bind("<Double-Button-1>", self.open_printer)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=6, pady=4)

        self.refresh_btn = ttk.Button(btn_frame, text="Refresh Device List", command=self.update_printers)
        self.refresh_btn.pack(side="left")

        self.open_btn = ttk.Button(btn_frame, text="Open Selected Printer", command=lambda: self.open_printer(None))
        self.open_btn.pack(side="right")

        self.update_printers()

    def update_printers(self) -> None:
        raw_printers = list(enumerate_printers())
        self._printers = []
        self.list.delete(0, tk.END)

        for p in raw_printers:
            # Filter out FAX interface (MI_03) as per L5190 technical report
            if "mi_03" in p.lower():
                logger.info("Ignoring FAX interface (MI_03): %s", p)
                continue
            self._printers.append(p)

        if not self._printers:
            self.list.insert(0, "No USB printers found. Connect your printer via USB and click Refresh.")
            return

        for idx, device in enumerate(self._printers):
            tag = ""
            if "114d" in device.lower():
                tag = "  [L5190 PRINTER]"
            elif "1185" in device.lower():
                tag = "  [L5290 PRINTER]"
            self.list.insert(idx, f"[{idx + 1}] {device}{tag}")

    def open_printer(self, _event: tk.Event | None) -> None:
        cur = self.list.curselection()
        if not cur or not self._printers:
            return

        selected_index = cur[0]
        if selected_index >= len(self._printers):
            return

        device_path = self._printers[selected_index]
        self.open_btn.config(state="disabled", text="Connecting to printer...")
        self.refresh_btn.config(state="disabled")

        def _worker() -> None:
            usb_transport = None
            backend = None
            try:
                usb_transport = USBPRINTTransport(device_path).__enter__()
                raw_id = usb_transport.identify()
                identifier = parse_identifier(raw_id)
                des_name = identifier.get("DES", identifier.get("MDL", "Epson Printer"))
                model_name = identifier.get("MDL", "")
                is_l5190 = any(k in model_name.upper() for k in ("L5190", "L5196", "L5198"))
                is_l5290 = any(k in model_name.upper() for k in ("L5290", "L5296", "L5298"))
                is_l5 = is_l5190 or is_l5290

                # L5 series uses fixed socket 0x02
                backend = D4ControlBackend(usb_transport, socket_id=0x02 if is_l5 else None).__enter__()
                device = by_model(model_name)
                printer = Printer(backend, device=device)

                self.after(0, lambda: self._on_printer_connected(usb_transport, backend, printer, des_name, model_name, is_l5190, is_l5290))
            except Exception as e:
                if backend is not None:
                    try:
                        backend.__exit__(None, None, None)
                    except Exception:
                        pass
                if usb_transport is not None:
                    try:
                        usb_transport.__exit__(None, None, None)
                    except Exception:
                        pass
                self.after(0, lambda: self._on_printer_error(e))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_printer_connected(
        self,
        usb_transport: USBPRINTTransport,
        backend: D4ControlBackend,
        printer: Printer,
        des_name: str,
        model_name: str,
        is_l5190: bool,
        is_l5290: bool,
    ) -> None:
        self.open_btn.config(state="normal", text="Open Selected Printer")
        self.refresh_btn.config(state="normal")

        window = tk.Toplevel()
        apply_window_icon(window)
        window.minsize(440, 560)

        def on_closing() -> None:
            try:
                backend.__exit__(None, None, None)
                usb_transport.__exit__(None, None, None)
            except Exception:
                pass
            window.destroy()

        window.protocol("WM_DELETE_WINDOW", on_closing)

        if is_l5190:
            window.title(f"{des_name} (L5JX Golden Engine) - ez-reset")
        elif is_l5290:
            window.title(f"{des_name} (L5TX Engine) - ez-reset")
        else:
            window.title(f"{des_name} - ez-reset")

        info = PrinterInfo(window, printer, des_name, model_name, is_l5190=is_l5190, is_l5290=is_l5290)
        info.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

    def _on_printer_error(self, e: Exception) -> None:
        self.open_btn.config(state="normal", text="Open Selected Printer")
        self.refresh_btn.config(state="normal")

        err_str = str(e)
        if "timed out" in err_str.lower() or "timeout" in err_str.lower():
            msg = (
                "Komunikasi ke printer timeout (printer tidak merespons).\n\n"
                "Penyebab Umum:\n"
                "1. Layar LCD printer sedang menampilkan peringatan 'Kendala Pemindai'.\n"
                "   -> Solusi: Tekan tombol 'OK' atau 'Batal' pada panel printer untuk menutup dialog tersebut, lalu klik 'Open Selected Printer' kembali.\n\n"
                "2. Program Epson Status Monitor masih berjalan di background.\n"
                "   -> Solusi: Tutup Epson Status Monitor dari Taskbar / Task Manager."
            )
            messagebox.showwarning("Printer Tidak Merespons (Timeout)", msg)
        elif isinstance(e, DeviceError):
            messagebox.showwarning(
                "Model Tidak Didukung",
                f"Printer terdeteksi, tetapi model tidak ada di devices.xml:\n{e}",
            )
        else:
            messagebox.showerror("Gagal Menghubungkan Printer", f"Terjadi kesalahan saat membuka koneksi printer:\n{e}")


class PrinterInfo(ttk.Frame):
    def __init__(self, master: tk.Misc, printer: Printer, des_name: str, model_name: str, is_l5190: bool = False, is_l5290: bool = False) -> None:
        super().__init__(master)

        self.printer = printer
        self.des_name = des_name
        self.model_name = model_name
        self.is_l5190 = is_l5190
        self.is_l5290 = is_l5290

        # Header Info Card
        info_frame = ttk.LabelFrame(self, text="Device Information")
        info_frame.pack(fill="x", padx=4, pady=2)

        if self.is_l5190:
            engine_tag = "  [L5JX Golden Protocol Active]"
        elif self.is_l5290:
            engine_tag = "  [L5TX Engine Active]"
        else:
            engine_tag = ""
        self.lbl_model = ttk.Label(info_frame, text=f"Model: {des_name} ({model_name}){engine_tag}")
        self.lbl_model.pack(anchor="w", padx=6, pady=1)

        self.lbl_serial = ttk.Label(info_frame, text="Serial: Reading...")
        self.lbl_serial.pack(anchor="w", padx=6, pady=1)

        # Ink Levels
        self.levels_frame = ttk.LabelFrame(self, text="Ink Levels")
        self.levels_frame.pack(fill="x", padx=4, pady=4)

        # Waste Levels
        self.waste_frame = ttk.LabelFrame(self, text="Waste Ink Counters")
        self.waste_frame.pack(fill="x", padx=4, pady=4)

        # Safety & Reset Options
        reset_box = ttk.LabelFrame(self, text="Reset & Hardware Safety")
        reset_box.pack(fill="x", padx=4, pady=4)

        opts_row = ttk.Frame(reset_box)
        opts_row.pack(fill="x", padx=4, pady=2)

        self.var_verify = tk.BooleanVar(value=True)
        chk_verify = ttk.Checkbutton(opts_row, text="Verify Writes (Read-back check)", variable=self.var_verify)
        chk_verify.pack(side="left", padx=4)

        self.var_dryrun = tk.BooleanVar(value=False)
        chk_dryrun = ttk.Checkbutton(opts_row, text="Dry-Run Mode (Simulation)", variable=self.var_dryrun)
        chk_dryrun.pack(side="left", padx=4)

        btn_row1 = ttk.Frame(reset_box)
        btn_row1.pack(fill="x", padx=4, pady=4)

        self.btn_reset_perm = ttk.Button(btn_row1, text="Reset Counters (Permanent)", command=self.reset_waste)
        self.btn_reset_perm.pack(side="left", fill="x", expand=True, padx=2)

        self.btn_reset_temp = ttk.Button(btn_row1, text="Temporary Reset ('rw')", command=self.temporary_reset)
        self.btn_reset_temp.pack(side="right", fill="x", expand=True, padx=2)

        # EEPROM Backup & Restore Frame
        eeprom_box = ttk.LabelFrame(self, text="EEPROM Tools (Backup / Restore)")
        eeprom_box.pack(fill="x", padx=4, pady=4)

        eeprom_row = ttk.Frame(eeprom_box)
        eeprom_row.pack(fill="x", padx=4, pady=2)

        btn_backup = ttk.Button(eeprom_row, text="Backup EEPROM...", command=self.backup_eeprom)
        btn_backup.pack(side="left", fill="x", expand=True, padx=2)

        btn_restore = ttk.Button(eeprom_row, text="Restore EEPROM...", command=self.restore_eeprom)
        btn_restore.pack(side="right", fill="x", expand=True, padx=2)

        # Maintenance Tools Frame
        maint_box = ttk.LabelFrame(self, text="Maintenance Utilities")
        maint_box.pack(fill="x", padx=4, pady=4)

        maint_row = ttk.Frame(maint_box)
        maint_row.pack(fill="x", padx=4, pady=2)

        btn_clean_std = ttk.Button(maint_row, text="Standard Clean", command=lambda: self.run_clean(1))
        btn_clean_std.pack(side="left", fill="x", expand=True, padx=2)

        btn_clean_pwr = ttk.Button(maint_row, text="Power Clean", command=lambda: self.run_clean(3))
        btn_clean_pwr.pack(side="left", fill="x", expand=True, padx=2)

        btn_restart = ttk.Button(maint_row, text="Restart Printer", command=self.restart_printer)
        btn_restart.pack(side="right", fill="x", expand=True, padx=2)

        # Refresh Bar
        bottom_bar = ttk.Frame(self)
        bottom_bar.pack(fill="x", padx=4, pady=6)

        self.refresh_btn = ttk.Button(bottom_bar, text="Refresh Status", command=self.update_status)
        self.refresh_btn.pack(side="right")

        self.levels: dict[InkColor, Level] = {}
        self.waste: dict[int, Waste] = {}

        self.update_status()

    def update_status(self) -> None:
        self.refresh_btn.config(state="disabled", text="Reading...")
        self.lbl_serial.config(text="Querying printer status & counters...")

        def _worker() -> None:
            try:
                status = self.printer.get_status()
                wastes = list(self.printer.get_waste())
                self.after(0, lambda: self._on_status_ready(status, wastes))
            except Exception as e:
                self.after(0, lambda: self._on_status_error(e))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_status_ready(self, status, wastes) -> None:
        self.refresh_btn.config(state="normal", text="Refresh Status")
        serial_no = status.serial or "Unknown"
        self.lbl_serial.config(text=f"Serial: {serial_no}  |  State: {status.state.name}")
        self.update_levels(status.levels)
        self.update_waste(wastes)

    def _on_status_error(self, e: Exception) -> None:
        self.refresh_btn.config(state="normal", text="Refresh Status")
        self.lbl_serial.config(text="Read error / timeout (Click Refresh Status to retry)")
        logger.warning("Failed to refresh status: %s", e)

    def update_levels(self, levels: Iterable[InkLevel]) -> None:
        for i, level in enumerate(levels):
            if level.color not in self.levels:
                self.levels[level.color] = Level(self.levels_frame, level.color.name, level.level)
            self.levels[level.color].update_level(level.level)
            self.levels[level.color].grid(column=i, row=0, padx=2, pady=2)
            self.levels_frame.columnconfigure(i, weight=1)

    def update_waste(self, levels: Iterable[tuple[int, int]]) -> None:
        for i, (level, max_level) in enumerate(levels):
            if i not in self.waste:
                self.waste[i] = Waste(self.waste_frame, level, max_level, f"Waste Counter #{i + 1}")
            self.waste[i].update_level(level)
            self.waste[i].pack(fill="x", padx=4, pady=2)

    def reset_waste(self) -> None:
        verify = self.var_verify.get()
        dry_run = self.var_dryrun.get()
        prefix = "[DRY RUN] " if dry_run else ""

        if self.is_l5190:
            prompt_msg = (
                f"{prefix}Epson L5190 Series (L5JX) Terdeteksi!\n\n"
                "Prosedur 'Golden Initialize Sequence' (11 Write + 2 Guard Read) akan dijalankan:\n"
                "• W0030=00, W0031=00 (Main Pad)\n"
                "• R002F, W002F=00\n"
                "• W0034=00, W0035=00, W0036=5E\n"
                "• W0032=00, W0033=00 (Platen Pad)\n"
                "• R002F, W002F=00\n"
                "• W0037=5E, W001C=00\n"
                "• Kunci Wire: Nbsjcbzb (4E 62 73 6A 63 62 7A 62)\n"
                "• Tanpa W0100 (Sesuai Golden APDA Trace)\n\n"
                "Lanjutkan proses reset?"
            )
            if not messagebox.askyesno(f"{prefix}L5190 Golden Initialize", prompt_msg):
                return

            self.btn_reset_perm.config(state="disabled", text="Resetting...")

            def _do_l5190() -> list:
                from ez_reset.l5190.constants import GOLDEN_INITIALIZE_SEQUENCE
                report = []
                for action, addr, target_val, _desc in GOLDEN_INITIALIZE_SEQUENCE:
                    if action == "WRITE":
                        old_v = -1
                        if not dry_run:
                            try:
                                old_v = self.printer.read_eeprom(addr)
                            except Exception:
                                old_v = -1
                        self.printer.write_eeprom(addr, target_val, verify=verify, dry_run=dry_run)
                        report.append((addr, old_v, target_val, True))
                    elif action == "READ":
                        if not dry_run:
                            self.printer.read_eeprom(addr)
                return report

            def _on_ok(_result) -> None:
                self.btn_reset_perm.config(state="normal", text="Reset Counters (Permanent)")
                self.update_status()
                messagebox.showinfo(
                    "Initialize Acknowledged",
                    f"{prefix}L5190 Golden Initialize Selesai (11/11 ACK ||:42:OK; Terverifikasi)!\n\n"
                    "Langkah Wajib Power-Cycle (Sesuai Laporan Teknis):\n"
                    "1. Matikan tombol daya printer (Power OFF).\n"
                    "2. Tunggu lampu printer mati total.\n"
                    "3. Nyalakan printer kembali (Power ON).\n"
                    "4. Klik Refresh Status di aplikasi untuk memastikan counter sudah 0%."
                )

            def _on_err(e: Exception) -> None:
                self.btn_reset_perm.config(state="normal", text="Reset Counters (Permanent)")
                if isinstance(e, VerificationError):
                    messagebox.showerror("Verification Failed", f"EEPROM Write Failed Verification!\n\n{e}")
                else:
                    messagebox.showerror("Reset Error", f"An error occurred while resetting L5190 counters:\n{e}")

            def _worker() -> None:
                try:
                    r = _do_l5190()
                    self.after(0, lambda: _on_ok(r))
                except Exception as exc:
                    self.after(0, lambda: _on_err(exc))

            threading.Thread(target=_worker, daemon=True).start()
            return

        if self.is_l5290:
            prompt_msg = (
                f"{prefix}Epson L5290 Series (L5TX) Terdeteksi!\n\n"
                "Prosedur 'Golden Initialize Sequence' (15 Write + 2 Guard Read) akan dijalankan:\n"
                "• W0030=00, W0031=00 (Main Pad)\n"
                "• R002F, W002F=00\n"
                "• W0034=00, W0035=00, W0036=5E\n"
                "• W0032=00, W0033=00 (Platen Pad)\n"
                "• R002F, W002F=00\n"
                "• W0037=5E, W001C=00\n"
                "• W00FC=00, W00FD=00 (Extra Ink Pad)\n"
                "• W00FE=00, W00FF=5E\n"
                "• Kunci Wire: Nbsjcbzb (4E 62 73 6A 63 62 7A 62)\n"
                "• Model Code: 4A 36 (L5TX)\n\n"
                "Lanjutkan proses reset?"
            )
            if not messagebox.askyesno(f"{prefix}L5290 Golden Initialize", prompt_msg):
                return

            self.btn_reset_perm.config(state="disabled", text="Resetting...")

            def _do_l5290() -> list:
                from ez_reset.l5190.constants import GOLDEN_INITIALIZE_SEQUENCE
                L5290_EXTRA = [
                    ("WRITE", 0x00FC, 0x00, "W00FC = 00 (Extra Ink Pad Lo)"),
                    ("WRITE", 0x00FD, 0x00, "W00FD = 00 (Extra Ink Pad Hi)"),
                    ("WRITE", 0x00FE, 0x00, "W00FE = 00 (Extra Ink Aux)"),
                    ("WRITE", 0x00FF, 0x5E, "W00FF = 5E (Extra Ink Req Lvl: 94)"),
                ]
                report = []
                for action, addr, target_val, _desc in list(GOLDEN_INITIALIZE_SEQUENCE) + L5290_EXTRA:
                    if action == "WRITE":
                        old_v = -1
                        if not dry_run:
                            try:
                                old_v = self.printer.read_eeprom(addr)
                            except Exception:
                                old_v = -1
                        self.printer.write_eeprom(addr, target_val, verify=verify, dry_run=dry_run)
                        report.append((addr, old_v, target_val, True))
                    elif action == "READ":
                        if not dry_run:
                            self.printer.read_eeprom(addr)
                return report

            def _on_ok_l5290(_result) -> None:
                self.btn_reset_perm.config(state="normal", text="Reset Counters (Permanent)")
                self.update_status()
                messagebox.showinfo(
                    "Initialize Acknowledged",
                    f"{prefix}L5290 Golden Initialize Selesai (15/15 ACK ||:42:OK; Terverifikasi)!\n\n"
                    "Langkah Wajib Power-Cycle:\n"
                    "1. Matikan tombol daya printer (Power OFF).\n"
                    "2. Tunggu lampu printer mati total.\n"
                    "3. Nyalakan printer kembali (Power ON).\n"
                    "4. Klik Refresh Status untuk memastikan counter sudah 0%."
                )

            def _on_err_l5290(e: Exception) -> None:
                self.btn_reset_perm.config(state="normal", text="Reset Counters (Permanent)")
                if isinstance(e, VerificationError):
                    messagebox.showerror("Verification Failed", f"EEPROM Write Failed Verification!\n\n{e}")
                else:
                    messagebox.showerror("Reset Error", f"An error occurred while resetting L5290 counters:\n{e}")

            def _worker_l5290() -> None:
                try:
                    r = _do_l5290()
                    self.after(0, lambda: _on_ok_l5290(r))
                except Exception as exc:
                    self.after(0, lambda: _on_err_l5290(exc))

            threading.Thread(target=_worker_l5290, daemon=True).start()
            return

        if not messagebox.askyesno(
            f"{prefix}Confirm Reset",
            f"{prefix}Are you sure you want to reset all waste ink counters for this printer?\n\n"
            f"Write Verification: {'Enabled' if verify else 'Disabled'}\n"
            f"Dry Run: {'Yes' if dry_run else 'No'}",
        ):
            return

        self.btn_reset_perm.config(state="disabled", text="Resetting...")

        def _do_generic() -> list:
            return self.printer.reset_waste(verify=verify, dry_run=dry_run)

        def _on_ok_generic(report: list) -> None:
            self.btn_reset_perm.config(state="normal", text="Reset Counters (Permanent)")
            self.update_status()
            summary = "\n".join(
                f"Address 0x{addr:04X}: Old=0x{old_val:02X} -> New=0x{new_val:02X} (Verified: {ok})"
                for (addr, old_val, new_val, ok) in report
            )
            messagebox.showinfo(
                "Reset Successful",
                f"{prefix}Waste ink counters have been successfully reset!\n\n"
                f"Summary of changes:\n{summary}\n\n"
                "Please restart the printer to apply changes."
            )

        def _on_err_generic(e: Exception) -> None:
            self.btn_reset_perm.config(state="normal", text="Reset Counters (Permanent)")
            if isinstance(e, VerificationError):
                messagebox.showerror("Verification Failed", f"EEPROM Write Failed Verification!\n\n{e}")
            else:
                messagebox.showerror("Reset Error", f"An error occurred while resetting counters:\n{e}")

        def _worker_generic() -> None:
            try:
                r = _do_generic()
                self.after(0, lambda: _on_ok_generic(r))
            except Exception as exc:
                self.after(0, lambda: _on_err_generic(exc))

        threading.Thread(target=_worker_generic, daemon=True).start()

    def temporary_reset(self) -> None:
        dry_run = self.var_dryrun.get()
        prefix = "[DRY RUN] " if dry_run else ""

        if not messagebox.askyesno(
            f"{prefix}Temporary Reset",
            "This will send the 'rw' service command using the printer's serial number.\n"
            "This resets waste ink levels temporarily to allow printing without permanent EEPROM write.\n\n"
            "Proceed?",
        ):
            return

        self.btn_reset_temp.config(state="disabled", text="Processing...")

        def _worker() -> None:
            try:
                success = self.printer.temporary_reset_waste(dry_run=dry_run)
                self.after(0, lambda: _done(success))
            except Exception as e:
                self.after(0, lambda: _fail(e))

        def _done(success: bool) -> None:
            self.btn_reset_temp.config(state="normal", text="Temporary Reset ('rw')")
            self.update_status()
            if success:
                messagebox.showinfo("Temporary Reset", f"{prefix}Temporary reset command acknowledged by printer.")
            else:
                messagebox.showwarning("Temporary Reset", "Printer responded with refusal (:NA;).")

        def _fail(e: Exception) -> None:
            self.btn_reset_temp.config(state="normal", text="Temporary Reset ('rw')")
            messagebox.showerror("Error", f"Failed to execute temporary reset:\n{e}")

        threading.Thread(target=_worker, daemon=True).start()

    def backup_eeprom(self) -> None:
        suggested_name = f"eeprom_backup_{self.model_name}.json"
        filepath = filedialog.asksaveasfilename(
            title="Save EEPROM Backup",
            initialfile=suggested_name,
            filetypes=[("JSON Backup (*.json)", "*.json"), ("Raw Binary (*.bin)", "*.bin")],
        )
        if not filepath:
            return

        self.winfo_toplevel().config(cursor="watch")
        self.winfo_toplevel().update()

        def _worker() -> None:
            try:
                dump = self.printer.backup_eeprom(filepath, start=0x00, end=0xFF)
                self.after(0, lambda: _done(dump))
            except Exception as e:
                self.after(0, lambda: _fail(e))

        def _done(dump: dict) -> None:
            self.winfo_toplevel().config(cursor="")
            messagebox.showinfo(
                "Backup Saved",
                f"Successfully backed up {len(dump)} EEPROM bytes to:\n{Path(filepath).name}",
            )

        def _fail(e: Exception) -> None:
            self.winfo_toplevel().config(cursor="")
            messagebox.showerror("Backup Error", f"Failed to backup EEPROM:\n{e}")

        threading.Thread(target=_worker, daemon=True).start()

    def restore_eeprom(self) -> None:
        filepath = filedialog.askopenfilename(
            title="Select EEPROM Backup to Restore",
            filetypes=[("Backup Files (*.json, *.bin)", "*.json;*.bin"), ("All Files", "*.*")],
        )
        if not filepath:
            return

        verify = self.var_verify.get()
        dry_run = self.var_dryrun.get()

        if not messagebox.askyesno(
            "Confirm Restore",
            f"WARNING: Restoring EEPROM will overwrite memory registers from backup:\n{Path(filepath).name}\n\n"
            "Are you sure you want to continue?",
        ):
            return

        self.winfo_toplevel().config(cursor="watch")
        self.winfo_toplevel().update()

        def _worker() -> None:
            try:
                report = self.printer.restore_eeprom(filepath, verify=verify, dry_run=dry_run)
                self.after(0, lambda: _done(report))
            except Exception as e:
                self.after(0, lambda: _fail(e))

        def _done(report: list) -> None:
            self.winfo_toplevel().config(cursor="")
            self.update_status()
            messagebox.showinfo(
                "Restore Completed",
                f"Successfully restored {len(report)} EEPROM registers.\nPlease restart your printer.",
            )

        def _fail(e: Exception) -> None:
            self.winfo_toplevel().config(cursor="")
            messagebox.showerror("Restore Error", f"Failed to restore EEPROM:\n{e}")

        threading.Thread(target=_worker, daemon=True).start()

    def run_clean(self, level: int) -> None:
        desc = "Power Clean" if level == 3 else "Standard Clean"
        if not messagebox.askyesno("Clean Nozzles", f"Start {desc} routine on printer?"):
            return

        def _worker() -> None:
            try:
                self.printer.clean(level=level)
                self.after(0, lambda: messagebox.showinfo("Cleaning Started", f"{desc} command sent. Please wait for printer to finish."))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Error", f"Failed to start cleaning:\n{e}"))

        threading.Thread(target=_worker, daemon=True).start()

    def restart_printer(self) -> None:
        if not messagebox.askyesno("Restart", "Send restart command to printer?"):
            return

        def _worker() -> None:
            try:
                self.printer.restart()
                self.after(0, lambda: messagebox.showinfo("Restart", "Restart command issued."))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Error", f"Failed to restart printer:\n{e}"))

        threading.Thread(target=_worker, daemon=True).start()


class Level(ttk.Frame):
    def __init__(self, master: tk.Misc, color: str, level: int) -> None:
        super().__init__(master)
        self.color = color
        self.level = level

        self.gauge = ttk.Progressbar(self, length=64, orient="vertical")
        self.label = ttk.Label(self, text="", anchor="center", font=("Segoe UI", 8))
        self.title_lbl = ttk.Label(self, text=color[:3], anchor="center", font=("Segoe UI", 7, "bold"))

        self.title_lbl.pack(side="top", fill="x")
        self.gauge.pack(side="top", fill="x", padx=2)
        self.label.pack(side="bottom", fill="x")
        self.update_level(level)

    def update_level(self, level: int) -> None:
        self.gauge["value"] = level
        self.label["text"] = f"{level}%"


class Waste(ttk.Frame):
    def __init__(self, master: tk.Misc, level: int, max_level: int, waste_type: str) -> None:
        super().__init__(master)
        self.level = level
        self.max = max_level
        self.type = waste_type

        self.label = ttk.Label(self, text=waste_type, anchor="w", font=("Segoe UI", 9))
        self.gauge = ttk.Progressbar(self)
        self.amount = ttk.Label(self, text="", font=("Segoe UI", 9, "bold"))

        self.columnconfigure(0, weight=1)
        self.label.grid(row=0, column=0, sticky="WE")
        self.amount.grid(row=0, column=1, sticky="E")
        self.gauge.grid(row=1, column=0, sticky="NSWE", columnspan=2, pady=2)
        self.update_level(level)

    def update_level(self, level: int) -> None:
        pct = (level / self.max) * 100 if self.max > 0 else 0
        self.gauge["value"] = pct
        self.amount["text"] = f"{pct:0.1f}% ({level}/{self.max})"


def run_cli(args: argparse.Namespace) -> int:
    raw_printers = list(enumerate_printers())
    printers = [p for p in raw_printers if "mi_03" not in p.lower()]
    if not printers and raw_printers:
        printers = raw_printers
    if args.list:
        print("Detected USB Printers:")
        if not printers:
            print("  (None found)")
            return 0
        for idx, p in enumerate(printers):
            print(f"  [{idx + 1}] {p}")
        return 0

    if not printers:
        print("Error: No USB printers found.", file=sys.stderr)
        return 1

    selected_device = args.device or printers[0]
    if selected_device.isdigit():
        idx = int(selected_device) - 1
        if 0 <= idx < len(printers):
            selected_device = printers[idx]
        else:
            print(f"Error: Invalid printer index {args.device}", file=sys.stderr)
            return 1

    print(f"Connecting to: {selected_device}")
    with USBPRINTTransport(selected_device) as transport:
        raw_id = transport.identify()
        ident = parse_identifier(raw_id)
        model_name = ident.get("MDL", "")
        des_name = ident.get("DES", model_name)
        is_l5 = any(k in model_name.upper() for k in ("L5190", "L5196", "L5198", "L5290", "L5296", "L5298"))
        socket_id = 0x02 if is_l5 else None

        with D4ControlBackend(transport, socket_id=socket_id) as backend:
            print(f"Connected: {des_name} ({model_name})")

            device = by_model(model_name)
            printer = Printer(backend, device)

            if args.status or (not args.reset and not args.temp_reset and not args.backup and not args.restore and not args.clean):
                st = printer.get_status()
                print(f"Serial Number: {st.serial}")
                print(f"Printer State: {st.state.name}")
                print(f"Error Status : {st.error.name}")
                print("\nInk Levels:")
                for ink in st.levels:
                    print(f"  - {ink.color.name:<15}: {ink.level}%")
                print("\nWaste Counters:")
                for i, (cnt, max_cnt) in enumerate(printer.get_waste()):
                    pct = (cnt / max_cnt * 100) if max_cnt > 0 else 0
                    print(f"  - Counter #{i + 1:<8}: {pct:0.1f}% ({cnt}/{max_cnt})")

            if args.backup:
                print(f"\nBacking up EEPROM to {args.backup}...")
                dump = printer.backup_eeprom(args.backup)
                print(f"Backup complete. {len(dump)} bytes saved.")

            if args.restore:
                print(f"\nRestoring EEPROM from {args.restore} (verify={not args.no_verify}, dry_run={args.dry_run})...")
                rep = printer.restore_eeprom(args.restore, verify=not args.no_verify, dry_run=args.dry_run)
                print(f"Restore complete. {len(rep)} registers processed.")

            if args.reset:
                print(f"\nResetting waste counters (verify={not args.no_verify}, dry_run={args.dry_run})...")
                rep = printer.reset_waste(verify=not args.no_verify, dry_run=args.dry_run)
                for addr, old_v, new_v, ok in rep:
                    print(f"  Addr 0x{addr:04X}: 0x{old_v:02X} -> 0x{new_v:02X} [Verified: {ok}]")
                print("Reset operation complete. Please restart the printer.")

            if args.temp_reset:
                print(f"\nExecuting temporary reset ('rw') (dry_run={args.dry_run})...")
                ok = printer.temporary_reset_waste(dry_run=args.dry_run)
                print(f"Result: {'Success (:OK;)' if ok else 'Refused (:NA;)'}")

            if args.clean is not None:
                lvl = int(args.clean)
                print(f"\nSending head clean command (level {lvl})...")
                printer.clean(level=lvl)
                print("Clean command dispatched.")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="ez-reset (Enhanced) - Waste Ink Resetter for Epson Printers")
    parser.add_argument("-l", "--list", action="store_true", help="List detected USB printers")
    parser.add_argument("-d", "--device", type=str, help="Device path or index (default: first device)")
    parser.add_argument("-s", "--status", action="store_true", help="Print ink levels and waste status")
    parser.add_argument("-r", "--reset", action="store_true", help="Reset waste ink counters (permanent)")
    parser.add_argument("-t", "--temp-reset", action="store_true", help="Execute temporary waste reset ('rw')")
    parser.add_argument("-b", "--backup", type=str, help="Backup EEPROM to file (.json or .bin)")
    parser.add_argument("--restore", type=str, help="Restore EEPROM from backup file")
    parser.add_argument("--clean", type=int, nargs="?", const=1, help="Trigger head cleaning (1=Std, 3=Power)")
    parser.add_argument("--no-verify", action="store_true", help="Disable write read-back verification")
    parser.add_argument("--dry-run", action="store_true", help="Simulate writes without modifying EEPROM")
    parser.add_argument("--gui", action="store_true", help="Force graphical user interface mode")

    args = parser.parse_args()

    # If any CLI action flags were passed (and not forced GUI), run CLI
    cli_flags = [args.list, args.status, args.reset, args.temp_reset, args.backup, args.restore, args.clean is not None]
    if any(cli_flags) and not args.gui:
        sys.exit(run_cli(args))

    # Otherwise run GUI
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Windows taskbar icon separation: set AppUserModelID before window creation
    try:
        myappid = "ezreset.wasteink.epson.app.v1"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass

    root = tk.Tk()
    root.title("ez-reset (Enhanced)")
    apply_window_icon(root)
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    elif "winnative" in style.theme_names():
        style.theme_use("winnative")

    def show_error(self, *args) -> None:  # noqa: ANN001,ANN002,ARG001
        err = traceback.format_exc()
        messagebox.showerror("Exception", str(err))

    tk.Tk.report_callback_exception = show_error

    root.minsize(480, 280)
    root.geometry("500x310")

    app = PrinterList(root)
    app.pack(fill="both", expand=True)

    root.mainloop()


if __name__ == "__main__":
    main()
