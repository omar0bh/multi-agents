# ============================================================
# rename_onedrive_folders.ps1
# Safely renames Arabic/French OneDrive shell folders to English.
# Run once from PowerShell as your normal user (no admin needed).
# ============================================================

$OneDrive = Join-Path $env:USERPROFILE "OneDrive"
$Home     = $env:USERPROFILE

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " OneDrive Folder Rename: Arabic -> English" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# Map: current name (Arabic/French) -> new English name
$renames = @(
    @{ From = Join-Path $OneDrive "سطح المكتب";    To = Join-Path $OneDrive "Desktop"   },
    @{ From = Join-Path $OneDrive "المستندات";     To = Join-Path $OneDrive "Documents"  },
    @{ From = Join-Path $OneDrive "الصور";         To = Join-Path $OneDrive "Pictures"   },
    @{ From = Join-Path $OneDrive "ملفات الفيديو"; To = Join-Path $OneDrive "Videos"     },
    @{ From = Join-Path $OneDrive "اموسيقى";       To = Join-Path $OneDrive "Music"      },
    @{ From = Join-Path $OneDrive "الموسيقى";      To = Join-Path $OneDrive "Music"      },
    @{ From = Join-Path $OneDrive "التنزيلات";     To = Join-Path $OneDrive "Downloads"  },
    @{ From = Join-Path $Home     "سطح المكتب";    To = Join-Path $Home     "Desktop"    },
    @{ From = Join-Path $Home     "ملفات الفيديو"; To = Join-Path $Home     "Videos"     },
    @{ From = Join-Path $Home     "الصور";         To = Join-Path $Home     "Pictures"   },
    @{ From = Join-Path $Home     "المستندات";     To = Join-Path $Home     "Documents"  },
    @{ From = Join-Path $Home     "اموسيقى";       To = Join-Path $Home     "Music"      }
)

$count = 0
foreach ($r in $renames) {
    $from = $r.From
    $to   = $r.To

    if (Test-Path $from) {
        if (Test-Path $to) {
            Write-Host "  [SKIP] Target already exists: $to" -ForegroundColor Yellow
        } else {
            try {
                Rename-Item -Path $from -NewName (Split-Path $to -Leaf) -ErrorAction Stop
                Write-Host "  [OK]   $from  ->  $to" -ForegroundColor Green
                $count++
            } catch {
                Write-Host "  [ERR]  Failed to rename '$from': $_" -ForegroundColor Red
            }
        }
    }
}

Write-Host ""
if ($count -gt 0) {
    Write-Host "$count folder(s) renamed successfully!" -ForegroundColor Green
    Write-Host ""
    Write-Host "IMPORTANT: Please restart your PC or sign out/in" -ForegroundColor Yellow
    Write-Host "so Windows and OneDrive pick up the new folder names." -ForegroundColor Yellow
} else {
    Write-Host "No folders needed renaming (all already in English or not found)." -ForegroundColor Green
}
Write-Host "==================================================" -ForegroundColor Cyan
