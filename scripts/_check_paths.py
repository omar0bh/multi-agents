import sys, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from pathlib import Path
import winreg

home = Path.home()
onedrive = home / 'OneDrive'

print("=== HOME ===")
print(home)
print()

checks = {
    "Desktop (local)":       home / "Desktop",
    "Desktop (OD Arabic)":   onedrive / "\u0633\u0637\u062d \u0627\u0644\u0645\u0643\u062a\u0628",
    "Desktop (OD English)":  onedrive / "Desktop",
    "Downloads (local)":     home / "Downloads",
    "Downloads (OD)":        onedrive / "Downloads",
    "Documents (local)":     home / "Documents",
    "Documents (OD Arabic)": onedrive / "\u0627\u0644\u0645\u0633\u062a\u0646\u062f\u0627\u062a",
    "Documents (OD Eng)":    onedrive / "Documents",
    "Pictures (local)":      home / "Pictures",
    "Pictures (OD Arabic)":  onedrive / "\u0627\u0644\u0635\u0648\u0631",
    "Pictures (OD Eng)":     onedrive / "Pictures",
    "Videos (local)":        home / "Videos",
    "Videos (OD Arabic)":    onedrive / "\u0645\u0644\u0641\u0627\u062a \u0627\u0644\u0641\u064a\u062f\u064a\u0648",
    "Videos (OD Eng)":       onedrive / "Videos",
    "Music (local)":         home / "Music",
    "Music (OD Arabic)":     onedrive / "\u0627\u0645\u0648\u0633\u064a\u0642\u0649",
}

print("=== EXISTING PATHS ===")
for label, p in checks.items():
    status = "OK" if p.exists() else "--"
    print(f"  [{status}] {label}: {p}")

print()
print("=== REGISTRY Shell Folders ===")
try:
    sub_key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub_key) as key:
        i = 0
        while True:
            try:
                name, val, _ = winreg.EnumValue(key, i)
                expanded = Path(os.path.expandvars(val))
                print(f"  {name}: {expanded}")
                i += 1
            except OSError:
                break
except Exception as e:
    print(f"Registry error: {e}")
