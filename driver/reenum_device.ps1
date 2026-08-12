# reenum_device.ps1
# Forces Windows to restart (disable+enable) the Metec BD / M30245 USB
# device — the software equivalent of unplugging and replugging the cable.
# Run by the "MetecBD_ReenumUSB" scheduled task (created by installTasks.py
# at add-on install time, with highest privileges), triggered on demand by
# metecBD.py's _soft_reset_device() via "schtasks /run" — no elevation is
# needed at trigger time because the task's privilege was already granted
# when it was registered.
#
# Looks the device up by VID/PID instead of a hardcoded instance ID so the
# same task definition works across different machines / device units.

$ErrorActionPreference = "SilentlyContinue"

$dev = Get-PnpDevice | Where-Object { $_.InstanceId -like "USB\VID_0452&PID_0100*" } | Select-Object -First 1

if (-not $dev) {
    Write-Output "MetecBD reenum: device not found (VID_0452&PID_0100)"
    exit 1
}

Write-Output "MetecBD reenum: restarting $($dev.InstanceId)"
& pnputil.exe /restart-device "$($dev.InstanceId)"
exit $LASTEXITCODE
