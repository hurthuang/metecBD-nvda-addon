"""
MetecBD USB 診斷工具
用法（以系統管理員身分，在 Python 3 環境下執行）：
  python diagnose_usb.py

功能：
  1. 確認 libusb-1.0.dll 載入成功
  2. 確認裝置 VID_0452/PID_0100 可被找到
  3. 列出所有 USB 端點（協助確認 EP_OUT / EP_IN 是否正確）
  4. 嘗試讀取 3 個 IN 封包並印出原始位元組（確認按鍵協定格式）
  5. 傳送測試格樣到前 5 格（確認輸出端點正確）

需要先安裝 libusb 並確認 WinUSB 或 libusbK 驅動已安裝。
"""

import ctypes
import sys
import os
import time

# ── 尋找 libusb-1.0.dll ─────────────────────────────────────────────────────
CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "..", "_libusb", "libusb-1.0.dll"),
    r"C:\Program Files\BRLTTY\libusb-1.0.dll",
    r"C:\Program Files (x86)\BRLTTY\libusb-1.0.dll",
    "libusb-1.0",
]

lib = None
for path in CANDIDATES:
    try:
        lib = ctypes.CDLL(path)
        print(f"[OK] 載入 libusb-1.0 from: {path}")
        break
    except OSError:
        pass

if lib is None:
    print("[錯誤] 找不到 libusb-1.0.dll")
    print("請將 libusb-1.0.dll 放入 _libusb/ 資料夾，或安裝 BRLTTY。")
    sys.exit(1)

# ── libusb 型別設定 ─────────────────────────────────────────────────────────
lib.libusb_open_device_with_vid_pid.restype  = ctypes.c_void_p
lib.libusb_open_device_with_vid_pid.argtypes = [
    ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]

lib.libusb_get_device.restype  = ctypes.c_void_p
lib.libusb_get_device.argtypes = [ctypes.c_void_p]

# ── 初始化 ──────────────────────────────────────────────────────────────────
ctx = ctypes.c_void_p()
ret = lib.libusb_init(ctypes.byref(ctx))
if ret != 0:
    print(f"[錯誤] libusb_init 失敗: {ret}")
    sys.exit(1)
print("[OK] libusb_init")

# ── 開啟裝置 ────────────────────────────────────────────────────────────────
VENDOR_ID  = 0x0452
PRODUCT_ID = 0x0100

handle = lib.libusb_open_device_with_vid_pid(ctx, VENDOR_ID, PRODUCT_ID)
if not handle:
    print(f"[錯誤] 找不到裝置 {VENDOR_ID:04X}:{PRODUCT_ID:04X}")
    print("請確認：")
    print("  1. 點字顯示器已連接")
    print("  2. 裝置管理員中顯示 WinUSB 或 libusbK 驅動（不是 U-tran 或 Unknown）")
    lib.libusb_exit(ctx)
    sys.exit(1)
print(f"[OK] 找到裝置 {VENDOR_ID:04X}:{PRODUCT_ID:04X}")

# ── 取得介面 ────────────────────────────────────────────────────────────────
handle_p = ctypes.c_void_p(handle)
ret = lib.libusb_claim_interface(handle_p, 0)
if ret != 0:
    print(f"[錯誤] 無法取得介面: {ret}")
    if ret == -6:
        print("  裝置被其他程式佔用（BRLTTY 還在執行？）")
    lib.libusb_close(handle_p)
    lib.libusb_exit(ctx)
    sys.exit(1)
print("[OK] 取得介面 0")

# ── 測試輸出（前 5 格顯示測試點字）────────────────────────────────────────
print("\n── 輸出測試 ──")
TEST_PATTERN = bytes([
    0x01,  # ⠁ 第 1 格：點 1
    0x03,  # ⠃ 第 2 格：點 1+2
    0x07,  # ⠇ 第 3 格：點 1+2+3
    0x0F,  # ⠏ 第 4 格：點 1+2+3+4
    0xFF,  # ⣿ 第 5 格：全點
]) + bytes(20)  # 其餘 20 格清空

EP_OUT = 0x01
buf_out = ctypes.create_string_buffer(TEST_PATTERN, 25)
transferred = ctypes.c_int(0)
ret = lib.libusb_bulk_transfer(handle_p, EP_OUT, buf_out, 25,
                                ctypes.byref(transferred), 1000)
if ret == 0:
    print(f"[OK] 輸出端點 0x{EP_OUT:02X} 回應正常（傳送 {transferred.value} bytes）")
    print("     請確認顯示器前 5 格是否有顯示測試點字（⠁⠃⠇⠏⣿）")
else:
    print(f"[警告] 輸出端點 0x{EP_OUT:02X} 錯誤 {ret}")
    print("       請嘗試修改 metecBD.py 中的 EP_OUT = 0x02 後重試")

# ── 嘗試讀取按鍵封包 ────────────────────────────────────────────────────────
print("\n── 輸入測試（請在 5 秒內按顯示器上的按鍵）──")
EP_IN = 0x81
buf_in = ctypes.create_string_buffer(16)
for i in range(10):
    transferred.value = 0
    ret = lib.libusb_bulk_transfer(handle_p, EP_IN, buf_in, 16,
                                    ctypes.byref(transferred), 500)
    if ret == 0 and transferred.value > 0:
        raw = bytes(buf_in[:transferred.value])
        print(f"  封包 #{i+1}: {raw.hex(' ')}  ({transferred.value} bytes)")
        # 嘗試解析 routing keys
        print("  → 路由鍵分析:", end=" ")
        pressed = []
        for cell in range(25):
            byte_i = cell >> 3
            bit    = 1 << (cell & 7)
            if byte_i < len(raw) and (raw[byte_i] & bit):
                pressed.append(cell)
        if pressed:
            print(f"格 {pressed}")
        else:
            print("無路由鍵（可能是功能鍵，請查看 byte[4] 以後）")
    elif ret == -7:
        pass  # timeout - normal when no key pressed
    else:
        print(f"  輸入端點 0x{EP_IN:02X} 錯誤 {ret}")
        print("  請嘗試修改 metecBD.py 中的 EP_IN = 0x82 後重試")
        break

# ── 清理 ────────────────────────────────────────────────────────────────────
lib.libusb_release_interface(handle_p, 0)
lib.libusb_close(handle_p)
lib.libusb_exit(ctx)
print("\n[OK] 診斷完成")
