# MetecBD — NVDA Braille Display Driver

Metec BD / M30245 Starter Kit（25 格 USB 點字顯示器）的 NVDA 附加元件驅動程式。

- 裝置：VID `0452` / PID `0100`
- 純 Windows WinUSB API（透過 `ctypes`），**不需要安裝 BRLTTY 或任何外部 DLL**
- 支援 Windows 11 / NVDA 2023.1 以上（已於 NVDA 2026.1 測試）

## 安裝步驟

### 1. 安裝 WinUSB 驅動程式

裝置接上電腦後，Windows 預設可能會安裝原廠驅動（u-tran 或其他），但這個附加元件需要 **WinUSB** 驅動才能運作。

直接安裝附加元件時（見下方步驟 2），安裝程式會自動偵測並嘗試安裝：

- 如果裝置已經是 WinUSB → 不會出現任何提示，直接完成
- 如果還不是 WinUSB → 會跳出對話框詢問是否安裝，按「是」後會跳出 Windows 的 UAC 視窗，請按「是」繼續，接著程式會呼叫 `pnputil /add-driver` 嘗試自動安裝

**這個自動安裝在大多數電腦上會失敗**，並彈出「請改用 Zadig」的錯誤訊息。原因不是程式碼問題，而是 Windows 的根本限制：

> 這個附加元件附帶的 `driver/MetecBD_WinUSB.inf` 沒有數位簽章（沒有 CatalogFile）。Windows 自 Vista x64 起，要求加入「驅動程式存放區」的 INF 必須有效簽署（或鏈結到受信任的憑證），否則 `pnputil` 會直接拒絕安裝，即使它只是引用 Microsoft 內建、已簽署的 `winusb.sys`。要正式解決這個限制，需要付費申請程式碼簽署憑證並送 Microsoft 簽署，這對小型開源附加元件不太划算，因此目前沒有採用。

所以**請預期需要手動跑一次 Zadig**（一次性操作，之後就不用再做）：

1. 下載並開啟 [Zadig](https://zadig.akeo.ie/)
2. 選單 `Options` 勾選 `List All Devices`
3. 從裝置清單選擇 Metec BD（VID_0452 PID_0100）
4. 右側驅動選擇 **WinUSB**，按 `Replace Driver`
5. 看到「Driver Installation: SUCCESSFUL」後，重新插拔 USB

> Zadig 之所以能成功，是因為它會自動產生一個自簽憑證並讓你信任它，藉此繞過上述的驅動簽署限制；這是 USB 工具社群處理未簽署 WinUSB 驅動的標準做法，並不是這個附加元件特有的權宜方法。

### 2. 安裝附加元件

1. 到 [Releases](../../releases) 頁面下載最新的 `metecBD-x.x.x.nvda-addon`
2. 在 NVDA 選單開啟「工具 > 附加元件市集」，或直接雙擊下載的 `.nvda-addon` 檔案
3. 依照畫面指示完成安裝，並重新啟動 NVDA

### 3. 啟用點字顯示器

1. NVDA 選單 > 偏好設定 > 點字設定
2. 點字顯示器選擇「Metec BD / M30245 (25 格)」
3. 確定後點顯器應立即顯示內容

## 按鍵對應

顯示器左右各有三顆按鍵（上／中／下），實測對應如下：

| 顯示器按鍵 | NVDA 動作 |
|---|---|
| 路由鍵（cursor routing key） | 移動游標到該格對應位置 |
| 左中 | 點字視窗向後捲動 |
| 右中 | 點字視窗向前捲動 |
| 左上 / 右上 | 移動到上一行行首 |
| 左下 / 右下 | 移動到下一行行首 |
| 左上+左下 | Home |
| 左上+左中+左下 | Ctrl+Home |
| 右上+右下 | End |
| 右上+右中+右下 | Ctrl+End |

## 開發者資訊

協定參考 [BRLTTY](https://brltty.app/) 的 Metec 驅動（`Drivers/Braille/Metec/braille.c`），改寫為純 WinUSB 實作：

- **格點輸出**：EP0 vendor control transfer，`req = 0x0A + 模組編號`，每模組 8 格
- **按鍵/狀態輪詢**：EP0 vendor control transfer IN，`req = 0x80`，每 50ms 一次，回傳 8 bytes（路由鍵、格數、巡覽鍵位元遮罩）
- **初始化**：`req=0x01`（高電壓 ON）→ `req=0x04`（識別觸發）→ `req=0x80`（讀取格數）

`tools/diagnose_usb.py` 提供獨立的 USB 診斷工具（透過 libusb-1.0），可用於排除裝置連線問題。

## 授權

附加元件原始碼採用 MIT 授權。
