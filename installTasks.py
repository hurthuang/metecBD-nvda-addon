"""
NVDA add-on installation tasks for MetecBD.
Runs automatically when the user installs this add-on through NVDA's
Add-on Manager.  Installs the WinUSB INF if the device doesn't already
have WinUSB as its driver service.
"""

import os
import ctypes
import subprocess
import winreg

import gui
import wx

_ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
_INF_PATH  = os.path.join(_ADDON_DIR, "driver", "MetecBD_WinUSB.inf")

VENDOR_ID  = 0x0452
PRODUCT_ID = 0x0100


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


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _install_winusb():
    """Install the WinUSB INF via pnputil, elevating if needed."""
    if _is_admin():
        # Already elevated — run synchronously and know the result.
        try:
            result = subprocess.run(
                ["pnputil", "/add-driver", _INF_PATH, "/install"],
                capture_output=True, timeout=30)
            success = result.returncode == 0
        except Exception:
            success = False

        if success:
            gui.messageBox(
                "WinUSB 驅動程式已安裝完成。\n\n"
                "請重新啟動 NVDA，並在「偏好設定 > 點字設定」中選擇\n"
                "「Metec BD / M30245 (25 格)」。\n\n"
                "若裝置目前未連接，請插入後 Windows 將自動套用驅動。",
                "MetecBD",
                wx.OK | wx.ICON_INFORMATION,
            )
        else:
            _show_manual_install_message()
    else:
        # Need UAC elevation — launch pnputil asynchronously with runas.
        args = f'/add-driver "{_INF_PATH}" /install'
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "pnputil.exe", args, None, 1)
        if ret > 32:
            gui.messageBox(
                "已送出 WinUSB 驅動程式安裝請求。\n\n"
                "請在彈出的「使用者帳戶控制」視窗中按「是」以繼續安裝。\n"
                "安裝完成後，請重新啟動 NVDA，並在「偏好設定 > 點字設定」中選擇\n"
                "「Metec BD / M30245 (25 格)」。",
                "MetecBD",
                wx.OK | wx.ICON_INFORMATION,
            )
        else:
            _show_manual_install_message()


def _show_manual_install_message():
    gui.messageBox(
        "WinUSB 驅動程式安裝失敗。\n\n"
        "請改用 Zadig（https://zadig.akeo.ie/）手動為裝置安裝 WinUSB 驅動，\n"
        "然後重新啟動 NVDA。",
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
            "（這是一次性操作，需要系統管理員權限。）\n\n"
            "立即安裝嗎？",
            "MetecBD - 驅動程式安裝",
            wx.YES_NO | wx.ICON_QUESTION,
        )
        if result == wx.ID_YES:
            _install_winusb()

    wx.CallAfter(_prompt)
