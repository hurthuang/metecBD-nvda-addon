"""
NVDA add-on installation tasks for MetecBD.
Runs automatically when the user installs this add-on through NVDA's
Add-on Manager.  Installs the WinUSB INF if the device doesn't already
have WinUSB as its driver service.
"""

import os
import ctypes
import winreg

import gui
import wx
from logHandler import log

_ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
_INF_PATH  = os.path.join(_ADDON_DIR, "driver", "MetecBD_WinUSB.inf")

VENDOR_ID  = 0x0452
PRODUCT_ID = 0x0100

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_HIDE = 0


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize",          ctypes.c_ulong),
        ("fMask",           ctypes.c_ulong),
        ("hwnd",            ctypes.c_void_p),
        ("lpVerb",          ctypes.c_wchar_p),
        ("lpFile",          ctypes.c_wchar_p),
        ("lpParameters",    ctypes.c_wchar_p),
        ("lpDirectory",     ctypes.c_wchar_p),
        ("nShow",           ctypes.c_int),
        ("hInstApp",        ctypes.c_void_p),
        ("lpIDList",        ctypes.c_void_p),
        ("lpClass",         ctypes.c_wchar_p),
        ("hkeyClass",       ctypes.c_void_p),
        ("dwHotKey",        ctypes.c_ulong),
        ("hIconOrMonitor",  ctypes.c_void_p),
        ("hProcess",        ctypes.c_void_p),
    ]


def _winusb_already_installed() -> bool:
    """Return True if the device's current driver service is WinUSB."""
    reg_key = (
        rf"SYSTEM\CurrentControlSet\Enum\USB"
        rf"\VID_{VENDOR_ID:04X}&PID_{PRODUCT_ID:04X}"
    )
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_key)
    except FileNotFoundError:
        return False
    with root:
        count = winreg.QueryInfoKey(root)[0]
        for i in range(count):
            try:
                instance = winreg.EnumKey(root, i)
                dev_key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE, rf"{reg_key}\{instance}")
                with dev_key:
                    try:
                        service, _ = winreg.QueryValueEx(dev_key, "Service")
                        if service.lower() == "winusb":
                            return True
                    except FileNotFoundError:
                        pass
            except OSError:
                continue
    return False


def _run_pnputil_elevated(args: str, timeout_ms: int = 30000):
    """
    Run pnputil with elevation, waiting for it to finish, and return its
    real exit code (0 = success). Uses ShellExecuteExW with verb="runas":
    this shows a UAC prompt only if the current process isn't already
    elevated. Returns None if the user cancelled the UAC prompt or the
    process could not be launched at all.
    """
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.hwnd = None
    sei.lpVerb = "runas"
    sei.lpFile = "pnputil.exe"
    sei.lpParameters = args
    sei.lpDirectory = None
    sei.nShow = SW_HIDE
    sei.hInstApp = None

    ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
    if not ok or not sei.hProcess:
        return None

    ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, timeout_ms)
    exit_code = ctypes.c_ulong(0)
    ctypes.windll.kernel32.GetExitCodeProcess(
        sei.hProcess, ctypes.byref(exit_code))
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    return exit_code.value


def _install_winusb():
    args = f'/add-driver "{_INF_PATH}" /install'
    exit_code = _run_pnputil_elevated(args)
    log.info(f"MetecBD installTasks: pnputil /add-driver exit_code={exit_code}")

    if exit_code == 0:
        gui.messageBox(
            "WinUSB 驅動程式已安裝完成。\n\n"
            "請重新啟動 NVDA，並在「偏好設定 > 點字設定」中選擇\n"
            "「Metec BD / M30245 (25 格)」。\n\n"
            "若裝置目前未連接，請插入後 Windows 將自動套用驅動。",
            "MetecBD",
            wx.OK | wx.ICON_INFORMATION,
        )
    elif exit_code is None:
        gui.messageBox(
            "驅動程式安裝已取消（UAC 視窗未被允許，或無法啟動 pnputil）。\n\n"
            "請改用 Zadig 手動安裝（見下方說明），然後重新啟動 NVDA。",
            "MetecBD - 安裝取消",
            wx.OK | wx.ICON_WARNING,
        )
        _show_manual_install_message()
    else:
        # Most likely cause: this hand-written INF has no digital
        # signature/catalog, so Windows refuses to add it to the driver
        # store even though it only re-uses the Microsoft-signed WinUSB
        # co-installer. Zadig works around this by generating and
        # trusting a self-signed certificate automatically.
        _show_manual_install_message()


def _show_manual_install_message():
    gui.messageBox(
        "WinUSB 驅動程式自動安裝失敗（常見原因：這個 INF 沒有數位簽章，"
        "Windows 拒絕將它加入驅動程式存放區）。\n\n"
        "請改用 Zadig（https://zadig.akeo.ie/）手動安裝：\n"
        "1. 開啟 Zadig，選單 Options 勾選 List All Devices\n"
        "2. 選擇 Metec BD 裝置（VID_0452 PID_0100）\n"
        "3. 右側選擇 WinUSB，按 Replace Driver\n"
        "4. 安裝完成後重新插拔 USB，再重新啟動 NVDA。",
        "MetecBD - 安裝錯誤",
        wx.OK | wx.ICON_ERROR,
    )


def onInstall():
    """Called by NVDA after the add-on files are copied."""
    if _winusb_already_installed():
        return

    if not os.path.isfile(_INF_PATH):
        return

    def _prompt():
        result = gui.messageBox(
            "MetecBD add-on 需要為 Metec BD 點字顯示器安裝 WinUSB 驅動程式。\n"
            "（這是一次性操作，需要系統管理員權限，會出現 UAC 視窗。）\n\n"
            "立即安裝嗎？",
            "MetecBD - 驅動程式安裝",
            wx.YES_NO | wx.ICON_QUESTION,
        )
        if result == wx.ID_YES:
            _install_winusb()

    wx.CallAfter(_prompt)
