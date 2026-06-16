"""
NVDA add-on installation tasks for MetecBD.
Runs automatically when the user installs this add-on through NVDA's
Add-on Manager.  Installs the WinUSB INF if the device doesn't already
have WinUSB as its driver service.
"""

import os
import subprocess
import winreg

import gui
import wx
from logHandler import log

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


def _ps_quote(s: str) -> str:
    """Quote a string as a single-quoted PowerShell literal."""
    return "'" + s.replace("'", "''") + "'"


def _run_pnputil_elevated(timeout_s: int = 40):
    """
    Run "pnputil /add-driver <inf> /install" elevated, waiting for it to
    finish, and return its real exit code (0 = success).

    Uses PowerShell's "Start-Process -Verb RunAs -Wait -PassThru" instead
    of hand-rolled ShellExecuteEx/ctypes: it reliably triggers UAC (or
    silently elevates if we're already admin), blocks until the process
    exits, and exposes the real exit code via $p.ExitCode. Returns None
    if the UAC prompt was cancelled or PowerShell itself could not be
    launched.
    """
    ps_cmd = (
        "$p = Start-Process -FilePath pnputil.exe "
        f"-ArgumentList '/add-driver',{_ps_quote(_INF_PATH)},'/install' "
        "-Verb RunAs -Wait -PassThru -WindowStyle Hidden; "
        "exit $p.ExitCode"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True,
            timeout=timeout_s,
        )
    except Exception:
        log.exception("MetecBD installTasks: failed to launch elevated pnputil")
        return None
    return result.returncode


def _install_winusb():
    try:
        exit_code = _run_pnputil_elevated()
    except Exception:
        log.exception("MetecBD installTasks: unexpected error during driver install")
        _show_manual_install_message()
        return
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
        try:
            # gui.messageBox's return value is no longer guaranteed to be a
            # stock wx.ID_* constant on newer NVDA (observed returning its
            # own internal enum, e.g. 2 for "Yes" instead of wx.ID_YES).
            # Use a plain wx.MessageDialog so ShowModal()'s return code is
            # always the genuine wx.ID_YES / wx.ID_NO.
            dlg = wx.MessageDialog(
                gui.mainFrame,
                "MetecBD add-on 需要為 Metec BD 點字顯示器安裝 WinUSB 驅動程式。\n"
                "（這是一次性操作，需要系統管理員權限，會出現 UAC 視窗。）\n\n"
                "立即安裝嗎？",
                "MetecBD - 驅動程式安裝",
                wx.YES_NO | wx.ICON_QUESTION,
            )
            result = dlg.ShowModal()
            dlg.Destroy()
            log.info(f"MetecBD installTasks: prompt result={result!r} (wx.ID_YES={wx.ID_YES!r})")
            if result == wx.ID_YES:
                _install_winusb()
        except Exception:
            log.exception("MetecBD installTasks: unexpected error in install prompt")

    wx.CallAfter(_prompt)
