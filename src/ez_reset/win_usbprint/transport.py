import ctypes
import logging
import threading
import time
from types import TracebackType
from typing import Self

from win32file import (
    FILE_FLAG_NO_BUFFERING,
    FILE_FLAG_WRITE_THROUGH,
    FILE_SHARE_READ,
    FILE_SHARE_WRITE,
    GENERIC_READ,
    GENERIC_WRITE,
    OPEN_EXISTING,
    CreateFileW,
    DeviceIoControl,
    ReadFile,
    WriteFile,
)

from ez_reset.transport import Transport

from .winapi import IOCTL_USBPRINT_GET_1284_ID, IOCTL_USBPRINT_SOFT_RESET

logger = logging.getLogger(__name__)

MAX_TRANSFER_SIZE = 0x400000


class USBPRINTTransport(Transport):
    def __init__(self, path: str) -> None:
        self.path = path
        self.handle = None
        self.closed = True

        self._buffer = b""

    def __enter__(self) -> Self:
        logger.debug("CreateFileW(%s)", self.path)
        handle = CreateFileW(
            self.path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_NO_BUFFERING | FILE_FLAG_WRITE_THROUGH,
            None,
        )
        self.handle = handle

        try:
            logger.debug("    Opened %s on handle %d", self.path, self.handle.handle)
            logger.debug(
                "DeviceIoControl(%d, IOCTL_USBPRINT_SOFT_RESET, NULL, 1024)",
                self.handle.handle,
            )
            DeviceIoControl(self.handle, IOCTL_USBPRINT_SOFT_RESET, None, 1024)
            logger.debug("    Issued soft reset to %d", self.handle.handle)
            self.closed = False
        except Exception:
            if self.handle is not None:
                try:
                    self.handle.close()
                except Exception:
                    pass
                self.handle = None
            self.closed = True
            raise

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        logger.debug("Closing...")
        if not self.closed and self.handle is not None:
            try:
                ctypes.windll.kernel32.CancelIoEx(int(self.handle), None)
            except Exception:
                pass
            try:
                self.handle.close()
            except Exception:
                pass
            self.handle = None
            self.closed = True

        return False

    def write(self, data: bytes) -> None:
        if self.closed or self.handle is None:
            msg = f"Handle to USBPRINT device {self.path} is closed"
            raise OSError(msg)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("WriteFile(%s)", data[:32])
            if len(data) > 32:
                logger.debug("    Above call was truncated")

        _status, bytes_written = WriteFile(self.handle, data)

        assert bytes_written == len(data)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("    Wrote %d bytes to %d", bytes_written, self.handle)

    def _read_chunk_with_watchdog(self, size: int, timeout_sec: float = 3.0) -> bytes:
        """Read a chunk from the USB device with a watchdog timer to abort blocking ReadFile."""
        if self.closed or self.handle is None:
            raise OSError(f"Handle to USBPRINT device {self.path} is closed")

        handle_int = int(self.handle)
        done_flag = threading.Event()

        def _watchdog() -> None:
            if not done_flag.wait(timeout_sec):
                if not self.closed:
                    try:
                        ctypes.windll.kernel32.CancelIoEx(handle_int, None)
                    except Exception:
                        pass

        wd = threading.Thread(target=_watchdog, daemon=True)
        wd.start()

        try:
            _status, data = ReadFile(self.handle, size)
            done_flag.set()
            return data
        except Exception as e:
            done_flag.set()
            # WinError 995: ERROR_OPERATION_ABORTED by CancelIoEx
            err_code = getattr(e, "winerror", None)
            if err_code is None and isinstance(e.args, tuple) and len(e.args) > 0:
                err_code = e.args[0] if isinstance(e.args[0], int) else None

            if err_code == 995:
                raise TimeoutError(f"Read timed out after {timeout_sec:.1f}s (no data received from USB device).") from e
            raise

    def read(self, size: int, timeout_sec: float = 4.0) -> bytes:
        if self.closed:
            msg = f"Handle to USBPRINT device {self.path} is closed"
            raise OSError(msg)

        deadline = time.monotonic() + timeout_sec
        while len(self._buffer) < size:
            rem = max(0.5, deadline - time.monotonic())
            if time.monotonic() >= deadline:
                # Flush stale buffer on timeout to prevent desync
                self._buffer = b""
                raise TimeoutError(f"Timed out waiting for {size} bytes from printer (got {len(self._buffer)} bytes).")

            data = self._read_chunk_with_watchdog(MAX_TRANSFER_SIZE, timeout_sec=rem)
            if data:
                self._buffer += data
            else:
                time.sleep(0.01)

        read = self._buffer[:size]
        self._buffer = self._buffer[size:]

        return read

    def drain(self, timeout_sec: float = 0.2) -> None:
        """Safely drain any pending data in the bulk IN buffer without blocking indefinitely."""
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                data = self._read_chunk_with_watchdog(MAX_TRANSFER_SIZE, timeout_sec=timeout_sec)
                if not data:
                    break
            except (TimeoutError, OSError):
                break

    def identify(self) -> str:
        return DeviceIoControl(self.handle, IOCTL_USBPRINT_GET_1284_ID, None, 1024)[2:].decode("ascii")
