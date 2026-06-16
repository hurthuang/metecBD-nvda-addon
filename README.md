# MetecBD — NVDA Braille Display Driver

Metec BD / M30245 Starter Kit（25 格 USB 點字顯示器）的 NVDA 附加元件驅動程式。

- 裝置：VID `0452` / PID `0100`
- 純 Windows WinUSB API（透過 `ctypes`），**不需要安裝 BRLTTY 或任何外部 DLL**
- 支援 Windows 11 / NVDA 2023.1 以上（已於 NVDA 2026.1 測試）

## 安裝步驟

### 1. 安裝 WinUSB 驅動程式

裝置接上電腦後，Windows 預設可能會安裝原廠驅動（u-tran 或其他），但這個附加元件需要 **WinUSB** 驅動才能運作。

直接安裝附加元件時（見下方步驟 2），安裝程式會自動偵測：

- 如果裝置已經是 WinUSB → 不會出現任何提示，直接完成
- 如果還不是 WinUSB → 會跳出對話框詢問是否安裝，按「是」後：
  - 若 NVDA 以系統管理員身分執行 → 直接背景安裝完成
  - 否則 → 會跳出 Windows 的 UAC 視窗，請按「是」繼續

如果自動安裝失敗，請改用 [Zadig](https://zadig.akeo.ie/) 手動安裝：

1. 開啟 Zadig，選單 `Options` 勾選 `List All Devices`
2. 從裝置清單選擇 Metec BD（VID_0452 PID_0100）
3. 右側驅動選擇 **WinUSB**，按 `Replace Driver`
4. 安裝完成後重新插拔 USB

### 2. 安裝附加元件

1. 到 [Releases](../../releases) 頁面下載最新的 `metecBD-x.x.x.nvda-addon`
2. 在 NVDA 選單開啟「工具 > 附加元件市集」，或直接雙擊下載的 `.nvda-addon` 檔案
3. 依照畫面指示完成安裝，並重新啟動 NVDA

### 3. 啟用點字顯示器

1. NVDA 選單 > 偏好設定 > 點字設定
2. 點字顯示器選擇「Metec BD / M30245 (25 格)」
3. 確定後點顯器應立即顯示內容

## 按鍵對應

| 顯示器按鍵 | NVDA 動作 |
|---|---|
| 路由鍵（cursor routing key） | 移動游標到該格對應位置 |
| fk2 | 點字向後捲動 |
| fk5 | 點字向前捲動 |
| fk1 / fk4 | 上一行 |
| fk3 / fk6 | 下一行 |
| ckl / cku / ckr / ckd | 方向鍵（左/上/右/下） |
| fk1+fk3 | Home |
| fk1+fk2+fk3 | Ctrl+Home |
| fk4+fk6 | End |
| fk4+fk5+fk6 | Ctrl+End |

## 開發者資訊

協定參考 [BRLTTY](https://brltty.app/) 的 Metec 驅動（`Drivers/Braille/Metec/braille.c`），改寫為純 WinUSB 實作：

- **格點輸出**：EP0 vendor control transfer，`req = 0x0A + 模組編號`，每模組 8 格
- **按鍵/狀態輪詢**：EP0 vendor control transfer IN，`req = 0x80`，每 50ms 一次，回傳 8 bytes（路由鍵、格數、巡覽鍵位元遮罩）
- **初始化**：`req=0x01`（高電壓 ON）→ `req=0x04`（識別觸發）→ `req=0x80`（讀取格數）

`tools/diagnose_usb.py` 提供獨立的 USB 診斷工具（透過 libusb-1.0），可用於排除裝置連線問題。

## 授權

附加元件原始碼採用 MIT 授權（如需調整請自行修改本節）。
