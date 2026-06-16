"""
NVDA Braille Display Driver - Metec BD / M30245 Starter Kit
25-cell USB braille display, VID 0452 / PID 0100.

Uses Windows WinUSB API via ctypes — no external DLL required.
Protocol (from BRLTTY Drivers/Braille/Metec/braille.c):
  Init:  EP0 OUT req=0x01 (high voltage ON, 8 bytes)
         EP0 OUT req=0x04 (identity trigger, 1 byte)
         EP 0x81 Bulk IN read (identity data, up to 1024 bytes)
         EP0 IN  req=0x80 (status packet: [routing, cellCount, navLo, navHi, ...])
  Write: EP0 OUT req=0x0A+N (module N, 8 cell bytes each), bits reversed
         (25 cells → 3–4 modules depending on device-reported count)
  Poll:  EP0 IN  req=0x80 every 50 ms for key/routing events

All control transfers use OVERLAPPED I/O (same as libusb-1.0 Windows backend).
"""

import ctypes
import ctypes.wintypes as wintypes
import threading
import time
import winreg
import uuid as _uuid

import braille
import inputCore
from logHandler import log

# ─── DLL handles ─────────────────────────────────────────────────────────────
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_api = ctypes.WinDLL("setupapi",  use_last_error=True)
_usb = ctypes.WinDLL("winusb",    use_last_error=True)

# ─── Win32 constants ──────────────────────────────────────────────────────────
GENERIC_RW            = 0xC0000000
FILE_SHARE_RW         = 0x00000003
OPEN_EXISTING         = 3
FILE_FLAG_OVERLAPPED  = 0x40000000
DIGCF_PRESENT         = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010
ERROR_IO_PENDING      = 997
WAIT_OBJECT_0         = 0x00000000
INFINITE              = 0xFFFFFFFF

# ─── Device / protocol parameters ────────────────────────────────────────────
VENDOR_ID    = 0x0452
PRODUCT_ID   = 0x0100
NUM_CELLS    = 25
POLL_MS      = 50

CTRL_OUT            = 0x40   # bmRequestType: vendor | host→device | device
CTRL_IN             = 0xC0   # bmRequestType: vendor | device→host | device
REQ_HIGH_VOLTAGE    = 0x01   # EP0 OUT: high voltage ON/OFF (8 bytes)
REQ_IDENTITY        = 0x04   # EP0 OUT: trigger identity read (1 byte)
REQ_STATUS          = 0x80   # EP0 IN:  status/key packet (8 bytes)
REQ_MODULE_BASE     = 0x0A   # EP0 OUT: write module N cells (req = 0x0A + N, 8 bytes)

MT_STATUS_SIZE      = 8      # bytes returned by req=0x80
MT_IDENTITY_SIZE    = 0x400  # max identity bytes to read from EP 0x81

EP_BULK_IN          = 0x81   # Bulk IN: identity data after req=0x04

NUM_MODULES         = 4      # default; overwritten by device-reported cell count
MODULE_SIZE         = 8      # bytes per module (BRLTTY MT_MODULE_SIZE)

# Bit-reversal table: NVDA bit order → Metec device bit order
_BIT_REV = bytes(
    sum(((b >> i) & 1) << (7 - i) for i in range(8))
    for b in range(256)
)

# ─── ctypes structures ────────────────────────────────────────────────────────
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize",             wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags",              wintypes.DWORD),
        ("Reserved",           ctypes.c_void_p),
    ]

class SP_DEVICE_INTERFACE_DETAIL_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize",     wintypes.DWORD),
        ("DevicePath", ctypes.c_wchar * 512),
    ]

class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal",     ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset",       wintypes.DWORD),
        ("OffsetHigh",   wintypes.DWORD),
        ("hEvent",       ctypes.c_void_p),
    ]

class WINUSB_SETUP_PACKET(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("RequestType", ctypes.c_ubyte),
        ("Request",     ctypes.c_ubyte),
        ("Value",       ctypes.c_ushort),
        ("Index",       ctypes.c_ushort),
        ("Length",      ctypes.c_ushort),
    ]

# ─── Win32 / WinUSB function signatures ──────────────────────────────────────
_api.SetupDiGetClassDevsW.restype  = ctypes.c_void_p
_api.SetupDiGetClassDevsW.argtypes = [
    ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]

_api.SetupDiEnumDeviceInterfaces.restype  = wintypes.BOOL
_api.SetupDiEnumDeviceInterfaces.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p,
    ctypes.POINTER(GUID), wintypes.DWORD,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]

_api.SetupDiGetDeviceInterfaceDetailW.restype  = wintypes.BOOL
_api.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA),
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]

_api.SetupDiDestroyDeviceInfoList.restype  = wintypes.BOOL
_api.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

_k32.CreateFileW.restype  = ctypes.c_void_p
_k32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]

_k32.CloseHandle.restype  = wintypes.BOOL
_k32.CloseHandle.argtypes = [ctypes.c_void_p]

_k32.CreateEventW.restype  = ctypes.c_void_p
_k32.CreateEventW.argtypes = [
    ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]

_k32.WaitForSingleObject.restype  = wintypes.DWORD
_k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]

_k32.GetOverlappedResult.restype  = wintypes.BOOL
_k32.GetOverlappedResult.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(OVERLAPPED),
    ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]

_k32.CancelIoEx.restype  = wintypes.BOOL
_k32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.POINTER(OVERLAPPED)]

_usb.WinUsb_Initialize.restype  = wintypes.BOOL
_usb.WinUsb_Initialize.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]

_usb.WinUsb_Free.restype  = wintypes.BOOL
_usb.WinUsb_Free.argtypes = [ctypes.c_void_p]

_usb.WinUsb_ControlTransfer.restype  = wintypes.BOOL
_usb.WinUsb_ControlTransfer.argtypes = [
    ctypes.c_void_p,                   # InterfaceHandle
    WINUSB_SETUP_PACKET,               # SetupPacket (by value, 8 bytes)
    ctypes.c_void_p,                   # Buffer
    wintypes.DWORD,                    # BufferLength
    ctypes.POINTER(wintypes.DWORD),    # LengthTransferred
    ctypes.POINTER(OVERLAPPED),        # Overlapped (async)
]

_usb.WinUsb_ReadPipe.restype  = wintypes.BOOL
_usb.WinUsb_ReadPipe.argtypes = [
    ctypes.c_void_p,                   # InterfaceHandle
    ctypes.c_ubyte,                    # PipeID
    ctypes.c_void_p,                   # Buffer
    wintypes.DWORD,                    # BufferLength
    ctypes.POINTER(wintypes.DWORD),    # LengthTransferred
    ctypes.POINTER(OVERLAPPED),        # Overlapped (async)
]

_usb.WinUsb_WritePipe.restype  = wintypes.BOOL
_usb.WinUsb_WritePipe.argtypes = [
    ctypes.c_void_p,                   # InterfaceHandle
    ctypes.c_ubyte,                    # PipeID
    ctypes.c_void_p,                   # Buffer
    wintypes.DWORD,                    # BufferLength
    ctypes.POINTER(wintypes.DWORD),    # LengthTransferred
    ctypes.POINTER(OVERLAPPED),        # Overlapped (async)
]

_usb.WinUsb_QueryPipe.restype  = wintypes.BOOL
_usb.WinUsb_QueryPipe.argtypes = [
    ctypes.c_void_p,                   # InterfaceHandle
    ctypes.c_ubyte,                    # AlternateInterfaceNumber
    ctypes.c_ubyte,                    # PipeIndex
    ctypes.c_void_p,                   # PipeInformation (WINUSB_PIPE_INFORMATION*)
]

_usb.WinUsb_SetCurrentAlternateSetting.restype  = wintypes.BOOL
_usb.WinUsb_SetCurrentAlternateSetting.argtypes = [
    ctypes.c_void_p,                   # InterfaceHandle
    ctypes.c_ubyte,                    # SettingNumber
]

_usb.WinUsb_ResetPipe.restype  = wintypes.BOOL
_usb.WinUsb_ResetPipe.argtypes = [
    ctypes.c_void_p,                   # InterfaceHandle
    ctypes.c_ubyte,                    # PipeID
]

_usb.WinUsb_SetPipePolicy.restype  = wintypes.BOOL
_usb.WinUsb_SetPipePolicy.argtypes = [
    ctypes.c_void_p,                   # InterfaceHandle
    ctypes.c_ubyte,                    # PipeID
    ctypes.c_ulong,                    # PolicyType
    ctypes.c_ulong,                    # ValueLength
    ctypes.c_void_p,                   # Value
]

# ─── Device discovery ─────────────────────────────────────────────────────────
def _str_to_guid(s):
    u = _uuid.UUID(s.strip("{}"))
    b = u.bytes_le
    g = GUID()
    g.Data1 = int.from_bytes(b[0:4], 'little')
    g.Data2 = int.from_bytes(b[4:6], 'little')
    g.Data3 = int.from_bytes(b[6:8], 'little')
    g.Data4 = (ctypes.c_ubyte * 8)(*b[8:16])
    return g


def _find_path_via_device_classes():
    vid_str = f"VID_{VENDOR_ID:04X}"
    pid_str = f"PID_{PRODUCT_ID:04X}"
    base = r"SYSTEM\CurrentControlSet\Control\DeviceClasses"
    try:
        base_key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return None
    with base_key:
        i = 0
        while True:
            try:
                guid_name = winreg.EnumKey(base_key, i)
            except OSError:
                break
            i += 1
            try:
                guid_key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE, rf"{base}\{guid_name}")
            except OSError:
                continue
            with guid_key:
                j = 0
                while True:
                    try:
                        dev_name = winreg.EnumKey(guid_key, j)
                    except OSError:
                        break
                    j += 1
                    if vid_str in dev_name.upper() and pid_str in dev_name.upper():
                        sym_reg = rf"{base}\{guid_name}\{dev_name}\#"
                        try:
                            sym_key = winreg.OpenKey(
                                winreg.HKEY_LOCAL_MACHINE, sym_reg)
                            with sym_key:
                                link, _ = winreg.QueryValueEx(sym_key, "SymbolicLink")
                                if link.startswith("\\??\\"):
                                    link = "\\\\?\\" + link[4:]
                                return link
                        except OSError:
                            pass
    return None


def _find_path_via_setupapi():
    reg_path = (
        rf"SYSTEM\CurrentControlSet\Enum\USB"
        rf"\VID_{VENDOR_ID:04X}&PID_{PRODUCT_ID:04X}"
    )
    guid_str = None
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
        with root:
            n = winreg.QueryInfoKey(root)[0]
            for i in range(n):
                instance = winreg.EnumKey(root, i)
                for vname in ("DeviceInterfaceGUIDs", "DeviceInterfaceGUID"):
                    try:
                        params = winreg.OpenKey(
                            winreg.HKEY_LOCAL_MACHINE,
                            rf"{reg_path}\{instance}\Device Parameters")
                        with params:
                            val, _ = winreg.QueryValueEx(params, vname)
                            g = val[0] if isinstance(val, list) else val
                            if g:
                                guid_str = g
                    except OSError:
                        pass
                if guid_str:
                    break
    except OSError:
        pass

    if not guid_str:
        log.warning("MetecBD: DeviceInterfaceGUID not found in registry")
        return None

    log.info(f"MetecBD: SetupAPI with GUID {guid_str}")
    guid = _str_to_guid(guid_str)
    hdi = _api.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None,
        DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not hdi or hdi == -1:
        return None

    try:
        iface = SP_DEVICE_INTERFACE_DATA()
        iface.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
        for idx in range(256):
            if not _api.SetupDiEnumDeviceInterfaces(
                    ctypes.c_void_p(hdi), None, ctypes.byref(guid),
                    idx, ctypes.byref(iface)):
                break
            for cb in (6, 8):
                detail = SP_DEVICE_INTERFACE_DETAIL_DATA()
                detail.cbSize = cb
                req = wintypes.DWORD(0)
                if _api.SetupDiGetDeviceInterfaceDetailW(
                        ctypes.c_void_p(hdi), ctypes.byref(iface),
                        ctypes.byref(detail), ctypes.sizeof(detail),
                        ctypes.byref(req), None):
                    path = detail.DevicePath
                    if (f"VID_{VENDOR_ID:04X}" in path.upper() and
                            f"PID_{PRODUCT_ID:04X}" in path.upper()):
                        log.info(f"MetecBD: SetupAPI found (cbSize={cb}): {path}")
                        return path
    finally:
        _api.SetupDiDestroyDeviceInfoList(ctypes.c_void_p(hdi))
    return None


def _find_device_path():
    path = _find_path_via_device_classes()
    if path:
        return path
    return _find_path_via_setupapi()


def _get_device_service():
    reg_path = (
        rf"SYSTEM\CurrentControlSet\Enum\USB"
        rf"\VID_{VENDOR_ID:04X}&PID_{PRODUCT_ID:04X}"
    )
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
    except OSError:
        return ""
    with root:
        count = winreg.QueryInfoKey(root)[0]
        for i in range(count):
            try:
                instance = winreg.EnumKey(root, i)
                dev_key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE, rf"{reg_path}\{instance}")
                with dev_key:
                    try:
                        service, _ = winreg.QueryValueEx(dev_key, "Service")
                        if service:
                            log.info(f"MetecBD: device service = {service!r}")
                            return service
                    except OSError:
                        pass
            except OSError:
                continue
    return ""


# ─── Gesture ──────────────────────────────────────────────────────────────────
class InputGesture(braille.BrailleDisplayGesture):
    source = "metecBD"

    def __init__(self, *, routingIndex=None, keys=None):
        super().__init__()
        if routingIndex is not None:
            self.routingIndex = routingIndex
            self.id = "routing"
        else:
            self.id = "+".join(sorted(keys or {"unknown"}))


# ─── Driver ───────────────────────────────────────────────────────────────────
class BrailleDisplayDriver(braille.BrailleDisplayDriver):
    name        = "metecBD"
    description = "Metec BD / M30245 (25 格)"
    isThreadSafe = True
    numCells    = NUM_CELLS

    @classmethod
    def check(cls):
        return True

    def __init__(self):
        super().__init__()
        self._dev_handle   = None
        self._usb_handle   = None
        self._lock         = threading.Lock()
        self._running      = False
        self._thread       = None
        self._last_cells   = None
        self._num_modules  = NUM_MODULES
        self._key_mask     = None
        self._routing_key  = 0xFF
        self._fct_key      = 0
        self._open()

    def terminate(self):
        self._close()
        super().terminate()

    # ── open / close ──────────────────────────────────────────────────────────
    def _open(self):
        path = _find_device_path()
        if not path:
            raise RuntimeError(
                f"找不到 WinUSB 裝置 {VENDOR_ID:04X}:{PRODUCT_ID:04X}。\n"
                "請確認點字顯示器已連接且已用 Zadig 安裝 WinUSB 驅動。")

        dev = _k32.CreateFileW(
            path, GENERIC_RW, FILE_SHARE_RW, None,
            OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if dev is None or dev == -1:
            raise RuntimeError(
                f"CreateFileW 失敗 (error {ctypes.get_last_error()})")

        try:
            service = _get_device_service()
            if service.lower() != "winusb":
                raise RuntimeError(
                    f"裝置驅動程式是「{service or '未知'}」，需要 WinUSB。\n"
                    "請用 Zadig 將驅動換成 WinUSB，重插 USB 並重啟 NVDA。")

            usb_h = ctypes.c_void_p()
            if not _usb.WinUsb_Initialize(
                    ctypes.c_void_p(dev), ctypes.byref(usb_h)):
                raise RuntimeError(
                    f"WinUsb_Initialize 失敗 (error {ctypes.get_last_error()})")
        except Exception:
            _k32.CloseHandle(ctypes.c_void_p(dev))
            raise

        self._dev_handle = dev
        self._usb_handle = usb_h.value

        self._init_device()

        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, name="MetecBD-Input", daemon=True)
        self._thread.start()
        log.info("MetecBD: 連線成功")

    def _close(self):
        if self._usb_handle:
            self._clear_display()
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._usb_handle:
            _usb.WinUsb_Free(ctypes.c_void_p(self._usb_handle))
            self._usb_handle = None
        if self._dev_handle:
            _k32.CloseHandle(ctypes.c_void_p(self._dev_handle))
            self._dev_handle = None
        log.info("MetecBD: 已中斷連線")

    def _clear_display(self):
        """Blank all cells and turn off high voltage before disconnecting,
        so the display doesn't keep showing the last NVDA content after exit
        (mirrors BRLTTY brl_destruct in braille.c)."""
        zeros = bytes(self._num_modules * MODULE_SIZE)
        for mod in range(self._num_modules):
            chunk = zeros[mod * MODULE_SIZE : (mod + 1) * MODULE_SIZE]
            self._ctrl_out(REQ_MODULE_BASE + mod, chunk)
        ok = self._ctrl_out(REQ_HIGH_VOLTAGE, bytes(8))
        log.info(f"MetecBD: 清空顯示 + 高電壓 OFF {'OK' if ok else 'FAIL'}")

    # ── Overlapped control transfer helper ────────────────────────────────────
    def _ctrl_transfer(self, request_type, request, length, out_data=None):
        """
        Send a USB vendor control transfer using OVERLAPPED I/O
        (same approach as libusb-1.0 Windows/WinUSB backend).
        Returns bytes (IN) or True (OUT) on success, None on failure.
        """
        pkt = WINUSB_SETUP_PACKET(
            RequestType=request_type, Request=request,
            Value=0, Index=0, Length=length)

        buf = ctypes.create_string_buffer(length) if length > 0 else None
        if out_data and length > 0:
            ctypes.memmove(buf, bytes(out_data)[:length], min(len(out_data), length))

        ev = _k32.CreateEventW(None, True, False, None)
        if not ev:
            return None
        ov = OVERLAPPED()
        ov.hEvent = ev

        try:
            transferred = wintypes.DWORD(0)
            ok = _usb.WinUsb_ControlTransfer(
                ctypes.c_void_p(self._usb_handle), pkt,
                buf, length,
                ctypes.byref(transferred), ctypes.byref(ov))

            if not ok:
                err = ctypes.get_last_error()
                if err != ERROR_IO_PENDING:
                    log.warning(
                        f"MetecBD: ControlTransfer req={request:#04x} "
                        f"type={request_type:#04x} FAIL err={err}")
                    return None
                # Wait for async completion (2 second timeout)
                wr = _k32.WaitForSingleObject(ctypes.c_void_p(ev), 2000)
                if wr != WAIT_OBJECT_0:
                    _k32.CancelIoEx(
                        ctypes.c_void_p(self._dev_handle), ctypes.byref(ov))
                    _k32.WaitForSingleObject(ctypes.c_void_p(ev), 1000)
                    log.warning(
                        f"MetecBD: ControlTransfer req={request:#04x} "
                        f"timeout (wait={wr:#x})")
                    return None
                ok2 = _k32.GetOverlappedResult(
                    ctypes.c_void_p(self._dev_handle),
                    ctypes.byref(ov),
                    ctypes.byref(transferred), False)
                if not ok2:
                    log.warning(
                        f"MetecBD: ControlTransfer req={request:#04x} "
                        f"GetOverlappedResult FAIL err={ctypes.get_last_error()}")
                    return None

            # Success
            if request_type & 0x80:  # IN direction
                return bytes(buf[:transferred.value]) if transferred.value > 0 else b""
            else:
                return True
        finally:
            _k32.CloseHandle(ctypes.c_void_p(ev))

    def _ctrl_out(self, request, data=b""):
        return self._ctrl_transfer(CTRL_OUT, request, len(data), data) is not None

    def _ctrl_in(self, request, length):
        result = self._ctrl_transfer(CTRL_IN, request, length)
        if result is None or len(result) == 0:
            return None
        return result

    def _bulk_read(self, pipe_id, length, timeout_ms=1000):
        """Read from a bulk/interrupt IN endpoint using OVERLAPPED I/O."""
        buf = ctypes.create_string_buffer(length)
        ev = _k32.CreateEventW(None, True, False, None)
        if not ev:
            return None
        ov = OVERLAPPED()
        ov.hEvent = ev
        try:
            transferred = wintypes.DWORD(0)
            ok = _usb.WinUsb_ReadPipe(
                ctypes.c_void_p(self._usb_handle),
                ctypes.c_ubyte(pipe_id),
                buf, length,
                ctypes.byref(transferred),
                ctypes.byref(ov))
            if not ok:
                err = ctypes.get_last_error()
                if err != ERROR_IO_PENDING:
                    log.info(
                        f"MetecBD: ReadPipe pipe={pipe_id:#04x} 即時失敗 err={err}")
                    return None
                wr = _k32.WaitForSingleObject(ctypes.c_void_p(ev), timeout_ms)
                if wr != WAIT_OBJECT_0:
                    _k32.CancelIoEx(
                        ctypes.c_void_p(self._dev_handle), ctypes.byref(ov))
                    _k32.WaitForSingleObject(ctypes.c_void_p(ev), 1000)
                    log.info(
                        f"MetecBD: ReadPipe pipe={pipe_id:#04x} "
                        f"timeout (wr={wr:#x})")
                    return None
                ok2 = _k32.GetOverlappedResult(
                    ctypes.c_void_p(self._dev_handle),
                    ctypes.byref(ov),
                    ctypes.byref(transferred), False)
                if not ok2:
                    log.info(
                        f"MetecBD: ReadPipe pipe={pipe_id:#04x} "
                        f"GetOverlappedResult FAIL err={ctypes.get_last_error()}")
                    return None
            return bytes(buf[:transferred.value]) if transferred.value > 0 else b""
        finally:
            _k32.CloseHandle(ctypes.c_void_p(ev))

    # ── Initialisation sequence ────────────────────────────────────────────────
    def _init_device(self):
        self._log_endpoints()

        # Step 1: High voltage ON — EP0 OUT req=0x01, 8 bytes [0xEF, 0×7]
        ok = self._ctrl_out(REQ_HIGH_VOLTAGE, bytes([0xEF, 0, 0, 0, 0, 0, 0, 0]))
        log.info(f"MetecBD: 高電壓 ON {'OK' if ok else f'FAIL err={ctypes.get_last_error()}'}")

        # Step 2: Identity trigger — EP0 OUT req=0x04, then attempt to read from EP 0x81.
        # BRLTTY retries twice; our device doesn't respond to the bulk read, so 1 attempt
        # with a short timeout avoids a 4-second delay in init.
        ok2 = self._ctrl_out(REQ_IDENTITY, bytes([0x00]))
        log.info(
            f"MetecBD: req=0x04 OUT "
            f"{'OK' if ok2 else f'FAIL err={ctypes.get_last_error()}'}")
        if ok2:
            identity = self._bulk_read(EP_BULK_IN, MT_IDENTITY_SIZE, timeout_ms=300)
            if identity is not None:
                log.info(
                    f"MetecBD: identity ({len(identity)}B): "
                    f"{identity[:16].hex() if identity else '(empty)'}")
            else:
                log.info("MetecBD: EP 0x81 identity — 裝置無回應（正常）")

        # Step 3: Status packet — EP0 IN req=0x80, 8 bytes.
        # Byte [0]=routing key, [1]=cell count, [2..3]=nav keys bitmask.
        status = self._ctrl_in(REQ_STATUS, MT_STATUS_SIZE)
        if status and len(status) >= 2:
            cell_count = status[1]
            # Ceiling division: include partial last module so all NUM_CELLS are sent.
            # e.g. 25 cells → ⌈25/8⌉ = 4 modules (req=0x0A..0x0D).
            # If the device rejects req=0x0D (STALL), _write_cells reduces _num_modules.
            self._num_modules = max(1, (cell_count + MODULE_SIZE - 1) // MODULE_SIZE)
            log.info(
                f"MetecBD: status={status.hex()} "
                f"cell_count={cell_count} modules={self._num_modules}")
        else:
            log.warning(
                f"MetecBD: status read FAIL err={ctypes.get_last_error()}, "
                f"預設 modules={self._num_modules}")

        log.info("MetecBD: init complete")

    def _log_endpoints(self):
        """Enumerate and log all USB pipes for diagnostics."""
        PIPE_TYPES = {0: "Control", 1: "Iso", 2: "Bulk", 3: "Interrupt"}
        # WINUSB_PIPE_INFORMATION: PipeType(4) PipeId(1) pad(1) MaxPacket(2) Interval(1) pad(3)
        buf = ctypes.create_string_buffer(12)
        idx = 0
        while True:
            ctypes.memset(buf, 0, 12)
            ok = _usb.WinUsb_QueryPipe(
                ctypes.c_void_p(self._usb_handle),
                ctypes.c_ubyte(0), ctypes.c_ubyte(idx),
                buf)
            if not ok:
                break
            pipe_type = int.from_bytes(buf[0:4], 'little')
            pipe_id   = int.from_bytes(buf[4:5], 'little')
            max_pkt   = int.from_bytes(buf[6:8], 'little')
            interval  = int.from_bytes(buf[8:9], 'little')
            log.info(
                f"MetecBD: pipe[{idx}] "
                f"type={PIPE_TYPES.get(pipe_type, pipe_type)} "
                f"id={pipe_id:#04x} maxPkt={max_pkt} interval={interval}")
            idx += 1

    # ── Braille output ─────────────────────────────────────────────────────────
    def display(self, cells):
        raw = bytes(cells[:NUM_CELLS]).ljust(NUM_CELLS, b'\x00')
        log.info(f"MetecBD: display() cells[0]={raw[0]:#04x}")
        with self._lock:
            if not self._usb_handle:
                return
            self._write_cells(raw)

    def _write_cells(self, raw):
        # Bit-reverse each cell byte (NVDA bit order → Metec device bit order)
        rev = bytes(_BIT_REV[b] for b in raw[:NUM_CELLS])
        # Pad to cover all modules (module_size × num_modules bytes)
        total = self._num_modules * MODULE_SIZE
        rev = (rev + bytes(total))[:total]

        if rev == self._last_cells:
            return

        # Write each 8-byte module via EP0 vendor control transfer.
        # Protocol from BRLTTY braille.c:460-461:
        #   tellUsbDevice(brl, 0x0A + moduleNumber, cells, MT_MODULE_SIZE)
        # wValue=0, wIndex=0, data=8 bytes of bit-reversed cell values.
        log.info(
            f"MetecBD: _write_cells {self._num_modules} modules "
            f"rev[0:4]={rev[0:4].hex()}")
        t0 = time.monotonic()

        for mod in range(self._num_modules):
            chunk = rev[mod * MODULE_SIZE : (mod + 1) * MODULE_SIZE]
            ok = self._ctrl_out(REQ_MODULE_BASE + mod, chunk)
            elapsed = int((time.monotonic() - t0) * 1000)
            if ok:
                log.info(
                    f"MetecBD: mod {mod} req={REQ_MODULE_BASE+mod:#04x} "
                    f"OK ({elapsed}ms) data={chunk.hex()}")
            else:
                err = ctypes.get_last_error()
                log.warning(
                    f"MetecBD: mod {mod} req={REQ_MODULE_BASE+mod:#04x} "
                    f"FAIL err={err} ({elapsed}ms)")
                if err == 22:  # ERROR_BAD_COMMAND = USB STALL
                    # Device rejected this module — permanently reduce module count
                    # so we don't retry it every display() call.
                    log.info(f"MetecBD: STALL on mod {mod} → _num_modules 降為 {mod}")
                    self._num_modules = mod
                return

        self._last_cells = rev
        elapsed = int((time.monotonic() - t0) * 1000)
        log.info(f"MetecBD: _write_cells all modules OK ({elapsed}ms)")

    # ── Key polling thread ─────────────────────────────────────────────────────
    def _poll_loop(self):
        # Keys and routing are reported via EP0 IN req=0x80 (same endpoint as
        # status packet used during init). BRLTTY polls this on a timer:
        # handleUsbStatusAlarm in braille.c:265-282.
        log.info("MetecBD: poll loop 啟動 (EP0 req=0x80 每 50ms)")
        while self._running:
            pkt = self._ctrl_in(REQ_STATUS, MT_STATUS_SIZE)
            if pkt and len(pkt) >= 4:
                self._dispatch(pkt)
            time.sleep(POLL_MS / 1000)

    # ── Gesture dispatch ───────────────────────────────────────────────────────
    def _dispatch(self, packet):
        routing_key = packet[0]
        fct_key     = (packet[2] | (packet[3] << 8)) & 0xFFFF

        if self._key_mask is None:
            self._key_mask = fct_key
            log.info(
                f"MetecBD: keyMask={fct_key:#06x}  "
                f"格數={packet[1] if len(packet) > 1 else '?'}")

        fct_key = fct_key & ~self._key_mask & 0xFFFF

        if routing_key != 0xFF:
            self._routing_key = routing_key
        self._fct_key |= fct_key

        if (self._fct_key or self._routing_key != 0xFF) \
                and fct_key == 0 and routing_key == 0xFF:
            self._fire(self._fct_key, self._routing_key)
            self._fct_key     = 0
            self._routing_key = 0xFF

    def _fire(self, fct_key, routing_key):
        try:
            if fct_key == 0 and routing_key != 0xFF:
                inputCore.manager.executeGesture(
                    InputGesture(routingIndex=routing_key))
                return
            names = set()
            if fct_key & 0x0001: names.add("fk6")
            if fct_key & 0x0002: names.add("fk5")
            if fct_key & 0x0004: names.add("fk3")
            if fct_key & 0x0008: names.add("fk4")
            if fct_key & 0x0010: names.add("fk2")
            if fct_key & 0x0040: names.add("fk1")
            if fct_key & 0x0400: names.add("ckl")
            if fct_key & 0x0800: names.add("cku")
            if fct_key & 0x1000: names.add("ckr")
            if fct_key & 0x4000: names.add("ckd")
            if routing_key != 0xFF:
                names.add(f"r{routing_key:02d}")
            if names:
                inputCore.manager.executeGesture(InputGesture(keys=names))
        except inputCore.NoInputGestureAction:
            pass

    gestureMap = inputCore.GlobalGestureMap({
        "globalCommands.GlobalCommands": {
            "braille_routeTo":       ("br(metecBD):routing",),
            "braille_scrollBack":    ("br(metecBD):fk2",),
            "braille_scrollForward": ("br(metecBD):fk5",),
            "braille_previousLine":  ("br(metecBD):fk1",
                                      "br(metecBD):fk4",),
            "braille_nextLine":      ("br(metecBD):fk3",
                                      "br(metecBD):fk6",),
            "kb:leftArrow":          ("br(metecBD):ckl",),
            "kb:upArrow":            ("br(metecBD):cku",),
            "kb:rightArrow":         ("br(metecBD):ckr",),
            "kb:downArrow":          ("br(metecBD):ckd",),
            "kb:home":               ("br(metecBD):fk1+fk3",),
            "kb:control+home":       ("br(metecBD):fk1+fk2+fk3",),
            "kb:end":                ("br(metecBD):fk4+fk6",),
            "kb:control+end":        ("br(metecBD):fk4+fk5+fk6",),
        },
    })
