[简体中文](README.md) | English

# QRClipper — Screen QR Code Reader

A lightweight Windows 10 background utility: press a hotkey, read the QR code currently on your screen, and the content is copied straight to your clipboard.

## Features

- Lives in the system tray — no taskbar clutter, no pop-ups
- **Two customizable global hotkeys**: full-screen scan / region selection
- Works across multiple monitors and high-DPI displays
- Optional launch at Windows startup
- Dual recognition engines (ZXing-C++ + Zbar), so QR codes containing Chinese text decode correctly

---

## Quick start

1. Double-click `dist\QRClipper.exe` (a single file — no Python installation needed)
2. A QR code icon appears in the tray at the bottom-right corner; the app is now running in the background
3. When a QR code is on screen, press **Ctrl + Alt + Q** — the content goes straight to the clipboard, ready to paste with Ctrl+V

### Hotkeys

| Hotkey | Action |
| --- | --- |
| `Ctrl + Alt + Q` | Full-screen scan: captures every monitor and reads any QR code it finds |
| `Ctrl + Alt + W` | Region selection: drag to select a screen area, then read it (more accurate when several QR codes are close together) |

> These are the **defaults**. Change them from the tray menu under "Settings…". The hotkeys
> actually in effect are shown in the startup notice and in the tray menu labels.

Every trigger always shows a bottom-right notice: "Copied to clipboard" plus a preview on
success, "No QR code found" when the screen has none, and "Scan failed" with the reason on
errors. See the next section.

## Bottom-right notice: is the app alive?

Every trigger always produces a bottom-right notice. The four titles are distinct,
so you can tell at a glance what happened:

| Title | Meaning | What to do |
| --- | --- | --- |
| `QRClipper 已启动` | The app is up; the message lists the hotkeys actually in effect | Check it matches the keys you pressed |
| `已复制到剪贴板` | Recognized and copied | Just Ctrl+V |
| `未识别到二维码` | **The app is fine — it simply found no code** | Follow the hint; use region select for small codes |
| `识别失败` | A real error (capture/decoding), with the reason | Retry, or run `QRClipper.exe --doctor` |

This notice is drawn by QRClipper itself. It does not depend on Windows notification
settings or on the tray icon working, so it still appears with `--no-tray` or when the
tray icon is collapsed. If a system notification happens to sit in the same corner, the
notice moves up instead of being hidden behind it.

## Tray menu

Right-click the tray icon:

- **Full-screen scan / Region selection** — same as the hotkeys (double-clicking the icon = full-screen scan)
- **Settings…** — change hotkeys, toggle the notice and the sound (only success notices are
  silenced; failures and empty results are always shown)
- **Launch at startup** — writes `HKCU\...\Run` so the app starts with Windows
- **Exit**

## Changing hotkeys

Tray → Settings → click the input box → press the new combination (e.g. `Ctrl + Alt + E`) → Save.

The change takes effect immediately — no restart needed. If the combination is already registered by another application, the app logs it and skips that hotkey.

Modifiers (Ctrl / Alt / Shift / Win) are detected by reading the live keyboard state instead of relying on Tk key events, so combinations the system menu would normally swallow — such as `Alt + letter` — are still captured correctly. Symbol keys (`; / = [ ] \` etc.) are handled by their actual virtual key codes and work right after saving.

## Command line

```bat
QRClipper.exe --scan       :: scan the full screen once, copy the result, and exit
QRClipper.exe --snip       :: open region selection once, then exit after reading
QRClipper.exe --selftest   :: self-test (generates a test QR code to verify the recognition pipeline, and checks clipboard read/write)
QRClipper.exe --doctor     :: environment diagnostics (DPI / coordinates / hotkeys / engines / elevation / autostart / clipboard)
QRClipper.exe --no-tray    :: run without a tray icon (stays resident but takes no tray space)
```

`--scan` can be hooked into other automation workflows; only one resident instance is allowed at a time (a second launch shows a message box).

## Troubleshooting

Run `QRClipper.exe --doctor` first (or check the startup log). It prints the DPI
awareness level, scaling, physical screen size, whether Tk coordinates match,
whether both hotkeys registered, the available engines, whether the process is
elevated, the autostart state, and a clipboard round-trip check.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Hotkeys dead after a reboot | `autostart` is `false`, so the app never starts | Enable "Launch at startup" in the tray menu, or start it manually |
| Double-clicking the exe does nothing | An instance is already running (the tray icon may be collapsed into the hidden area) | A message box is shown; quit from the tray and start again |
| Recognized but cannot paste | Running elevated (UIPI) or the clipboard was taken over by another app | Do not run as administrator; the app now reports copy failures explicitly |
| Small QR on a 4K screen not found | The code is tiny relative to the full screen | Use region selection; whole-image failures now fall back to a tiled sliding-window scan |

## Config & logs

- Config: `%APPDATA%\QRClipper\config.json`
- Log: `%APPDATA%\QRClipper\qrclip.log`

Sample config:

```json
{
  "hotkey_full": "ctrl+alt+q",
  "hotkey_snip": "ctrl+alt+w",
  "notify": true,
  "autostart": true,
  "copy_to_clipboard": true,
  "sound": false
}
```

Editing this file directly and restarting the app works just as well.

## Build from source

```bat
pip install pillow pyzbar zxing-cpp pystray
python qrclip.py            :: resident mode
python qrclip.py --selftest :: self-test
python qrclip.py --doctor   :: environment diagnostics
python tests\test_smoke.py                              :: smoke tests (run the file directly)
python -m unittest tests.test_smoke tests.test_hotkey_entry tests.test_decoder_tiles  :: non-interactive regression (recommended)
python build.py             :: package with PyInstaller into dist\QRClipper.exe
```

> Requires a Python build with tkinter (the official installer ships it by default). Packaging environment: Python 3.11.9 + PyInstaller 6.22.

## Project layout

```
QRClipper/
├─ qrclip.py       main program: tray, hotkey dispatch, command-line modes
├─ config.py       config read/write + launch at startup (registry)
├─ capture.py      Win32 GDI screen capture (virtual screen across monitors, DPI-aware)
├─ decoder.py      QR recognition (dual engines + multi-scale retries)
├─ clipboard.py    Win32 clipboard read/write (UTF-16, supports Chinese text and emoji)
├─ hotkey.py       global hotkeys (RegisterHotKey, no admin rights required, hot-reloadable)
├─ ui.py           region-selection overlay, settings window, tray icon
├─ build.py        packaging script
└─ tests/          smoke tests and GUI end-to-end tests
```

## FAQ

**Nothing gets recognized?**
- Try region selection first (`Ctrl+Alt+W`) and frame just the QR code — that has the highest success rate
- If the code is too small or blurred by scaling, the app upscales and retries automatically; if it still fails, zoom the page in and try again
- When several QR codes are on screen, all of them are read and copied to the clipboard as multiple lines

**The hotkey does nothing?**
- Another application registered that combination first — pick a different one (combinations including `Win` or `Shift` are recommended)
- Check `%APPDATA%\QRClipper\qrclip.log` for a "failed to register hotkey" entry

**Launch at startup doesn't work?**
- Some security tools block writes to the registry Run key; you can drop a shortcut to the exe into the `shell:startup` folder manually

**Memory usage?**
- Around 25–40 MB while resident, with almost no CPU use when idle — it only does work the moment you press a hotkey.
