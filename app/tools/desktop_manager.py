"""
Desktop Manager Tool
--------------------
Captures desktop screenshot, reads icon positions via Win32 API,
analyzes layout using Gemini Vision, and rearranges icons.
"""

import ctypes
import ctypes.wintypes as wintypes
import os
import sys
import json
import time
import struct
import subprocess
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Win32 Constants
# ─────────────────────────────────────────────────────────────────────────────
LVM_FIRST           = 0x1000
LVM_GETITEMCOUNT    = LVM_FIRST + 4
LVM_GETITEMTEXTW    = LVM_FIRST + 115
LVM_GETITEMPOSITION = LVM_FIRST + 16
LVM_SETITEMPOSITION = LVM_FIRST + 15

LVIF_TEXT = 0x0001

MEM_COMMIT  = 0x1000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04

PROCESS_VM_READ      = 0x0010
PROCESS_VM_WRITE     = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400

user32   = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

class LVITEM(ctypes.Structure):
    _fields_ = [
        ("mask",       ctypes.c_uint),
        ("iItem",      ctypes.c_int),
        ("iSubItem",   ctypes.c_int),
        ("state",      ctypes.c_uint),
        ("stateMask",  ctypes.c_uint),
        ("pszText",    ctypes.c_void_p),
        ("cchTextMax", ctypes.c_int),
        ("iImage",     ctypes.c_int),
        ("lParam",     ctypes.c_long),
        ("iIndent",    ctypes.c_int),
    ]


def _get_desktop_listview():
    """Return HWND of the desktop SysListView32."""
    progman = user32.FindWindowW("Progman", None)
    if not progman:
        return None
    shell_view = user32.FindWindowExW(progman, None, "SHELLDLL_DefView", None)
    if not shell_view:
        hwnd = user32.FindWindowExW(None, None, "WorkerW", None)
        while hwnd:
            shell_view = user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None)
            if shell_view:
                break
            hwnd = user32.FindWindowExW(None, hwnd, "WorkerW", None)
    if not shell_view:
        return None
    return user32.FindWindowExW(shell_view, None, "SysListView32", None)


def get_desktop_icons():
    """Read all desktop icon names and positions. Returns list of dicts."""
    listview = _get_desktop_listview()
    if not listview:
        return _fallback_get_icons()

    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(listview, ctypes.byref(pid))
    h_process = kernel32.OpenProcess(
        PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION | PROCESS_QUERY_INFORMATION,
        False, pid.value
    )
    if not h_process:
        return _fallback_get_icons()

    try:
        count = user32.SendMessageW(listview, LVM_GETITEMCOUNT, 0, 0)
        icons = []
        buf_size = 512
        remote_buf = kernel32.VirtualAllocEx(h_process, None, buf_size, MEM_COMMIT, PAGE_READWRITE)
        remote_pt  = kernel32.VirtualAllocEx(h_process, None, ctypes.sizeof(POINT), MEM_COMMIT, PAGE_READWRITE)
        remote_lvi = kernel32.VirtualAllocEx(h_process, None, ctypes.sizeof(LVITEM), MEM_COMMIT, PAGE_READWRITE)

        for i in range(count):
            user32.SendMessageW(listview, LVM_GETITEMPOSITION, i, remote_pt)
            pt = POINT()
            kernel32.ReadProcessMemory(h_process, remote_pt, ctypes.byref(pt), ctypes.sizeof(POINT), None)

            lvi = LVITEM()
            lvi.mask       = LVIF_TEXT
            lvi.iItem      = i
            lvi.iSubItem   = 0
            lvi.pszText    = remote_buf
            lvi.cchTextMax = 256
            kernel32.WriteProcessMemory(h_process, remote_lvi, ctypes.byref(lvi), ctypes.sizeof(LVITEM), None)
            user32.SendMessageW(listview, LVM_GETITEMTEXTW, i, remote_lvi)

            text_buf = (ctypes.c_wchar * 256)()
            kernel32.ReadProcessMemory(h_process, remote_buf, text_buf, 512, None)
            icons.append({"index": i, "name": text_buf.value, "x": pt.x, "y": pt.y})

        kernel32.VirtualFreeEx(h_process, remote_buf, 0, MEM_RELEASE)
        kernel32.VirtualFreeEx(h_process, remote_pt,  0, MEM_RELEASE)
        kernel32.VirtualFreeEx(h_process, remote_lvi, 0, MEM_RELEASE)
        return icons
    finally:
        kernel32.CloseHandle(h_process)


def _fallback_get_icons():
    desktop = Path(os.path.expanduser("~")) / "Desktop"
    return [{"index": i, "name": p.name, "x": -1, "y": -1}
            for i, p in enumerate(sorted(desktop.iterdir()))]


def set_icon_position(index: int, x: int, y: int) -> bool:
    listview = _get_desktop_listview()
    if not listview:
        return False
    lparam = (y << 16) | (x & 0xFFFF)
    return user32.SendMessageW(listview, LVM_SETITEMPOSITION, index, lparam) != 0


def get_screen_size():
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def disable_auto_arrange():
    script = r"""
$regPath = 'HKCU:\Software\Microsoft\Windows\Shell\Bags\1\Desktop'
if (!(Test-Path $regPath)) { New-Item -Path $regPath -Force | Out-Null }
Set-ItemProperty -Path $regPath -Name 'FFlags' -Value 0x40200001 -Type DWord -Force
Stop-Process -Name explorer -Force
Start-Sleep 2
Start-Process explorer
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True)
    time.sleep(3)


ZONE_PRESETS = {
    "top_left":      (0.0,  0.25, 0.0,  0.5 ),
    "top_center":    (0.25, 0.75, 0.0,  0.35),
    "top_right":     (0.75, 1.0,  0.0,  0.5 ),
    "left":          (0.0,  0.2,  0.0,  1.0 ),
    "right":         (0.8,  1.0,  0.0,  1.0 ),
    "center":        (0.25, 0.75, 0.2,  0.8 ),
    "bottom_left":   (0.0,  0.25, 0.65, 0.9 ),
    "bottom_center": (0.25, 0.75, 0.7,  0.9 ),
    "bottom_right":  (0.75, 1.0,  0.65, 0.9 ),
    "bottom":        (0.0,  1.0,  0.7,  0.9 ),
}
ICON_W, ICON_H = 80, 80


def arrange_icons(layout: dict) -> dict:
    screen_w, screen_h = get_screen_size()
    all_icons = get_desktop_icons()
    icon_map  = {ic["name"].lower(): ic for ic in all_icons}
    moved, not_found = [], []

    for zone, names in layout.items():
        if zone not in ZONE_PRESETS:
            continue
        x0_f, x1_f, y0_f, y1_f = ZONE_PRESETS[zone]
        x0 = int(x0_f * screen_w)
        y0 = int(y0_f * screen_h)
        y1 = int(y1_f * screen_h)
        col, row = x0 + 10, y0 + 10

        for name in names:
            key = name.lower()
            ic  = icon_map.get(key) or _fuzzy_match(key, icon_map)
            if not ic:
                not_found.append(name)
                continue
            set_icon_position(ic["index"], col, row)
            moved.append({"name": ic["name"], "zone": zone, "x": col, "y": row})
            row += ICON_H + 10
            if row + ICON_H > int(y1_f * screen_h):
                row = y0 + 10
                col += ICON_W + 10

    return {"moved": moved, "not_found": not_found}


def _fuzzy_match(key, icon_map):
    for k, v in icon_map.items():
        if key in k or k in key:
            return v
    return None


def take_screenshot(save_path=None):
    """Take a full-desktop screenshot. Works from any session type."""
    if not save_path:
        save_path = str(Path(os.environ.get("TEMP", "C:/Temp")) / "desktop_screenshot.png")

    # Method 1: PowerShell System.Drawing (works from any session, including services)
    ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$screen  = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp     = New-Object System.Drawing.Bitmap($screen.Width, $screen.Height)
$gfx     = [System.Drawing.Graphics]::FromImage($bmp)
$gfx.CopyFromScreen($screen.Location, [System.Drawing.Point]::Empty, $screen.Size)
$bmp.Save('{save_path.replace(chr(92), '/')}')
$gfx.Dispose()
$bmp.Dispose()
Write-Output 'OK'
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and os.path.exists(save_path):
            return save_path
    except Exception:
        pass

    # Method 2: mss (fast, but needs active display session)
    try:
        import mss, mss.tools
        with mss.mss() as sct:
            monitor = sct.monitors[1]  # Primary monitor (index 1, not 0)
            img = sct.grab(monitor)
            mss.tools.to_png(img.rgb, img.size, output=save_path)
            return save_path
    except Exception:
        pass

    # Method 3: PIL ImageGrab
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(all_screens=True)
        img.save(save_path)
        return save_path
    except Exception as e:
        raise RuntimeError(f"All screenshot methods failed: {e}")



def analyze_desktop_with_ai(screenshot_path: str) -> dict:
    import base64, httpx
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return {"error": "No GEMINI_API_KEY in environment"}

    with open(screenshot_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    prompt = """Look at this Windows desktop screenshot. List every visible icon.
For each icon determine category: app, system, folder, file, dev.
- system: This PC, Recycle Bin, Network, Control Panel
- dev: VS Code, Git, Terminal, Python, Docker, GitHub
- app: any other application shortcut
- folder: a folder
- file: a standalone file

Return ONLY valid JSON:
{"icons":[{"name":"...","category":"app|system|folder|file|dev","x_approx":0,"y_approx":0}],"total_count":0,"description":"..."}"""

    payload = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/png", "data": img_b64}}
        ]}],
        "generationConfig": {"temperature": 0.1}
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    resp = httpx.post(url, json=payload, timeout=60)
    resp.raise_for_status()

    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"): text = text[4:]
    data = json.loads(text.strip("` \n"))

    categories = {}
    for ic in data.get("icons", []):
        categories.setdefault(ic.get("category","other"), []).append(ic["name"])
    data["categories"] = categories
    return data


def cmd_screenshot_and_analyze() -> dict:
    path     = take_screenshot()
    analysis = analyze_desktop_with_ai(path)
    analysis["screenshot_path"] = path
    return analysis


def cmd_get_icon_list() -> dict:
    icons = get_desktop_icons()
    return {"icons": icons, "count": len(icons)}


def cmd_arrange_by_category(rules=None) -> dict:
    analysis = cmd_screenshot_and_analyze()
    if "error" in analysis:
        return analysis

    category_zones = {
        "app":    "left",
        "dev":    "top_center",
        "system": "right",
        "folder": "bottom",
        "file":   "bottom_right",
    }
    if rules:
        category_zones.update(rules)

    layout = {}
    for cat, names in analysis.get("categories", {}).items():
        zone = category_zones.get(cat, "center")
        layout.setdefault(zone, []).extend(names)

    disable_auto_arrange()
    result = arrange_icons(layout)
    result["analysis"]    = analysis.get("description", "")
    result["layout_used"] = layout
    return result
