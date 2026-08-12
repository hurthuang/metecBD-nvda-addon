"""
NVDA add-on installation tasks for MetecBD.
Runs automatically when the user installs this add-on through NVDA's
Add-on Manager. Two one-time, admin-only setup steps are combined into a
single elevated session (one UAC prompt) so re-running this on an update
where everything is already set up needs no prompt at all:

  1. Install the WinUSB INF if the device doesn't already have WinUSB as
     its driver service.
  2. Register the "MetecBD_ReenumUSB" scheduled task (runs as SYSTEM, highest
     privileges) that the driver's automatic STALL-recovery calls via
     "schtasks /run" at runtime — that call needs no elevation itself
     because the privilege was already granted here, once, at install time.
"""

import os
import subprocess
import tempfile
import winreg

import gui
import wx
from logHandler import log

_ADDON_DIR   = os.path.dirname(os.path.abspath(__file__))
_INF_PATH    = os.path.join(_ADDON_DIR, "driver", "MetecBD_WinUSB.inf")
_REENUM_PS1  = os.path.join(_ADDON_DIR, "driver", "reenum_device.ps1")
_REENUM_TASK = "MetecBD_ReenumUSB"

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


def _reenum_task_registered() -> bool:
    """Return True if the MetecBD_ReenumUSB scheduled task already exists.
    Querying task existence is unprivileged — no elevation needed here."""
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", _REENUM_TASK],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _ps_quote(s: str) -> str:
    """Quote a string as a single-quoted PowerShell literal."""
    return "'" + s.replace("'", "''") + "'"


def _run_elevated_setup(need_driver: bool, need_task: bool, timeout_s: int = 40):
    """
    Perform whichever of (WinUSB driver install, MetecBD_ReenumUSB task
    registration) are needed, inside a single elevated PowerShell session —
    the user only sees one UAC prompt even when both are needed.

    A plain exit code can't carry two independent results, so the elevated
    script writes them to a temp file that this (non-elevated) function
    reads back afterward.

    Returns {"driver": int|None, "task": int|None} — None per-key means
    "not attempted", otherwise the real exit code (0 = success). Returns
    None (not a dict) if the UAC prompt was cancelled or PowerShell itself
    could not be launched.
    """
    if not need_driver and not need_task:
        return {"driver": None, "task": None}

    result_fd, result_file = tempfile.mkstemp(suffix=".txt", prefix="metecbd_setup_result_")
    os.close(result_fd)
    os.remove(result_file)  # only want the unique path; the elevated script creates it

    # NOTE: Start-Process -ArgumentList, when given a PowerShell *array*,
    # joins the elements with plain spaces and does NOT quote elements that
    # themselves contain spaces — so a multi-word value (like the /tr command
    # line below) gets silently split into separate argv tokens for the
    # child process. Every value below is therefore built as a single,
    # already-quoted string and passed as ONE -ArgumentList string, not an
    # array. (Confirmed by hand: the array form fails with
    # "schtasks: bad argument '-NoProfile'"; the single-string form works.)
    script_lines = [
        "$ErrorActionPreference = 'SilentlyContinue'",
        "$results = @{}",
    ]
    if need_driver:
        pnputil_args = f'/add-driver "{_INF_PATH}" /install'
        script_lines += [
            f'$p = Start-Process -FilePath "pnputil.exe" '
            f'-ArgumentList {_ps_quote(pnputil_args)} -Wait -PassThru -WindowStyle Hidden',
            '$results["driver"] = $p.ExitCode',
        ]
    if need_task:
        # Registered for the CURRENT user (not SYSTEM) with /IT (interactive
        # token only, no stored password) + /RL HIGHEST: this is the
        # well-known pattern where "schtasks /run" from a normal, unelevated
        # process does NOT trigger a UAC prompt, because the elevation
        # decision was already made once, here, at registration time — the
        # Task Scheduler service (running as SYSTEM) launches it elevated on
        # our behalf. A task registered with /RU SYSTEM instead was tested
        # and does NOT have this property: Task Scheduler's default ACL on
        # a SYSTEM-run task still requires the *caller* to already be
        # elevated to query/run it, which defeats the purpose entirely.
        run_as_user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
        task_action = (
            f'powershell.exe -NoProfile -WindowStyle Hidden '
            f'-ExecutionPolicy Bypass -File \\"{_REENUM_PS1}\\"'
        )
        schtasks_args = (
            f'/create /tn "{_REENUM_TASK}" /tr "{task_action}" '
            f'/sc once /st 00:00 /ru "{run_as_user}" /it /rl highest /f'
        )
        script_lines += [
            f'$p2 = Start-Process -FilePath "schtasks.exe" '
            f'-ArgumentList {_ps_quote(schtasks_args)} -Wait -PassThru -WindowStyle Hidden',
            '$results["task"] = $p2.ExitCode',
        ]
    script_lines.append(
        '$results.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" } '
        f'| Out-File -FilePath "{result_file}" -Encoding utf8'
    )

    script_fd, script_path = tempfile.mkstemp(suffix=".ps1", prefix="metecbd_setup_")
    with os.fdopen(script_fd, "w", encoding="utf-8") as f:
        f.write("\r\n".join(script_lines))

    try:
        outer_cmd = (
            "$p = Start-Process -FilePath powershell.exe -ArgumentList "
            f"'-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden',"
            f"'-File',{_ps_quote(script_path)} "
            "-Verb RunAs -Wait -PassThru -WindowStyle Hidden; "
            "exit $p.ExitCode"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", outer_cmd],
            capture_output=True,
            timeout=timeout_s,
        )
    except Exception:
        log.exception("MetecBD installTasks: failed to launch elevated setup")
        return None
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass

    log.info(f"MetecBD installTasks: elevated Start-Process exit_code={result.returncode}")
    if result.returncode != 0:
        # Start-Process -Verb RunAs surfaces a non-zero exit if the UAC
        # prompt itself was cancelled.
        return None

    outcomes = {"driver": None, "task": None}
    try:
        with open(result_file, "r", encoding="utf-8") as f:
            for line in f:
                key, _, val = line.strip().partition("=")
                if key in outcomes and val:
                    outcomes[key] = int(val)
    except OSError:
        log.warning("MetecBD installTasks: result file missing after elevation")
        return None
    finally:
        try:
            os.remove(result_file)
        except OSError:
            pass

    return outcomes


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


def _run_setup(need_driver: bool, need_task: bool):
    outcomes = _run_elevated_setup(need_driver, need_task)
    log.info(f"MetecBD installTasks: setup outcomes={outcomes!r}")

    if outcomes is None:
        parts = []
        if need_driver:
            parts.append("驅動程式請改用 Zadig 手動安裝（見下方說明）。")
        if need_task:
            parts.append("自動軟重置功能將不可用，顯示器卡住時仍需手動拔插 USB。")
        gui.messageBox(
            "一次性設定已取消（UAC 視窗未被允許，或無法啟動 PowerShell）。\n\n"
            + "\n".join(parts),
            "MetecBD - 設定取消",
            wx.OK | wx.ICON_WARNING,
        )
        if need_driver:
            _show_manual_install_message()
        return

    driver_failed = False
    msgs = []
    if need_driver:
        if outcomes.get("driver") == 0:
            msgs.append("WinUSB 驅動程式已安裝完成。")
        else:
            driver_failed = True
            msgs.append(
                "WinUSB 驅動程式自動安裝失敗（多半是 INF 未簽章），"
                "請改用 Zadig 手動安裝，見下方說明。")
    if need_task:
        if outcomes.get("task") == 0:
            msgs.append(
                "自動軟重置功能已設定完成 —— 之後點字顯示器連線卡住時，"
                "NVDA 會自動嘗試恢復，不用拔插 USB。")
        else:
            msgs.append("自動軟重置功能設定失敗，顯示器卡住時仍需手動拔插 USB 才能恢復。")

    gui.messageBox(
        "\n\n".join(msgs) + "\n\n請重新啟動 NVDA 以套用變更。",
        "MetecBD",
        wx.OK | (wx.ICON_WARNING if driver_failed else wx.ICON_INFORMATION),
    )
    if driver_failed:
        _show_manual_install_message()


def onInstall():
    """Called by NVDA after the add-on files are copied."""
    need_driver = (not _winusb_already_installed()) and os.path.isfile(_INF_PATH)
    need_task = (not _reenum_task_registered()) and os.path.isfile(_REENUM_PS1)

    if not need_driver and not need_task:
        return

    def _prompt():
        try:
            parts = []
            if need_driver:
                parts.append("為 Metec BD 點字顯示器安裝 WinUSB 驅動程式")
            if need_task:
                parts.append("設定自動軟重置功能（顯示器連線卡住時不用拔插 USB 就能自動恢復）")
            msg = (
                "MetecBD add-on 需要進行以下一次性設定：\n"
                + "\n".join(f"• {p}" for p in parts)
                + "\n\n（需要系統管理員權限，會出現 UAC 視窗；之後就不用再要求權限。）"
                "\n\n立即設定嗎？"
            )
            # gui.messageBox's return value is no longer guaranteed to be a
            # stock wx.ID_* constant on newer NVDA (observed returning its
            # own internal enum, e.g. 2 for "Yes" instead of wx.ID_YES).
            # Use a plain wx.MessageDialog so ShowModal()'s return code is
            # always the genuine wx.ID_YES / wx.ID_NO.
            dlg = wx.MessageDialog(
                gui.mainFrame, msg, "MetecBD - 一次性設定",
                wx.YES_NO | wx.ICON_QUESTION,
            )
            result = dlg.ShowModal()
            dlg.Destroy()
            log.info(f"MetecBD installTasks: prompt result={result!r} (wx.ID_YES={wx.ID_YES!r})")
            if result == wx.ID_YES:
                _run_setup(need_driver, need_task)
        except Exception:
            log.exception("MetecBD installTasks: unexpected error in install prompt")

    wx.CallAfter(_prompt)
