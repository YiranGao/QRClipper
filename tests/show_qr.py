"""真机验收辅助：在屏幕正中显示一个二维码窗口，保持 N 秒后自动关闭。

用途：验证「识别成功 -> 已复制到剪贴板」这条链路在真实桌面上确实能走通、
      且剪贴板内容与显示的一致。
"""

from __future__ import annotations

import sys
import tkinter as tk

import qrcode

TEXT = "QRClipper-E2E-VERIFY-20261101"
SIZE = 340
HOLD_MS = 20000


def main() -> int:
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(TEXT)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    path = r"C:\Users\38292\WorkBuddy\2026-10-24-01-13-02\QRClipper\tests\_qr_verify.png"
    image.save(path)

    root = tk.Tk()
    root.title("QR-VERIFY")
    root.attributes("-topmost", True)
    photo = tk.PhotoImage(file=path)
    tk.Label(root, image=photo, bg="white").pack()
    root.geometry(f"{photo.width()}x{photo.height()}+1500+800")
    root.update_idletasks()
    print(TEXT, flush=True)
    root.after(HOLD_MS, root.destroy)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
