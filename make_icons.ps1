# Builds icons\<name>.png for the app buttons in config.json, taken from the apps installed on THIS PC.
# Run it again after adding apps:  powershell -ExecutionPolicy Bypass -File make_icons.ps1
Add-Type -AssemblyName System.Drawing
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$out = Join-Path $here "icons"
New-Item -ItemType Directory -Force $out | Out-Null
$cfg = Get-Content (Join-Path $here "config.json") -Raw | ConvertFrom-Json

function Save-ExeIcon($exe, $dest) {
  $ico = [System.Drawing.Icon]::ExtractAssociatedIcon($exe)
  $bmp = $ico.ToBitmap(); $bmp.Save($dest, [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose(); $ico.Dispose()
}

function Save-StoreAppIcon($appId, $dest) {
  # shell:AppsFolder\<PackageFamilyName>!<App>  ->  the package's own 44x44 logo from its manifest
  $family = ($appId -replace "^shell:AppsFolder\\", "") -split "!" | Select-Object -First 1
  $pkg = Get-AppxPackage | Where-Object { $_.PackageFamilyName -eq $family } | Select-Object -First 1
  if (-not $pkg) { return $false }
  [xml]$m = Get-Content (Join-Path $pkg.InstallLocation "AppxManifest.xml")
  $logo = $m.Package.Applications.Application | Select-Object -First 1 | ForEach-Object { $_.VisualElements.Square44x44Logo }
  if (-not $logo) { return $false }
  $base = Join-Path $pkg.InstallLocation ($logo -replace "\.png$", "")
  $dir = Split-Path $base -Parent; $stem = Split-Path $base -Leaf
  # prefer an unplated, large-ish version
  $candidates = Get-ChildItem $dir -Filter "$stem*.png" -ErrorAction SilentlyContinue
  $pick = $candidates | Where-Object { $_.Name -match "targetsize-(48|64|96|256)(?!.*altform)" -and $_.Name -notmatch "altform-(lightunplated|unplated)" } | Select-Object -First 1
  if (-not $pick) { $pick = $candidates | Where-Object { $_.Name -match "scale-(200|400|100)" } | Select-Object -First 1 }
  if (-not $pick) { $pick = $candidates | Select-Object -First 1 }
  if (-not $pick) { return $false }
  Copy-Item $pick.FullName $dest -Force
  return $true
}

foreach ($a in $cfg.apps) {
  $name = ($a.name.ToLower() -replace "[^a-z0-9]+", "-").Trim("-")
  $dest = Join-Path $out "$name.png"
  $how = $null
  try {
    if ($a.target -like "shell:AppsFolder*") { if (Save-StoreAppIcon $a.target $dest) { $how = "store package logo" } }
    elseif ($a.target -like "steam:*") {
      $s = (Get-ItemProperty "HKCU:\Software\Valve\Steam" -ErrorAction SilentlyContinue).SteamExe
      if ($s -and (Test-Path $s)) { Save-ExeIcon $s $dest; $how = "steam.exe icon" }
    }
    elseif ($a.target -match "^[\w\.-]+$") {   # a program name such as msedge
      $p = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\$($a.target).exe" -ErrorAction SilentlyContinue).'(default)'
      if ($p -and (Test-Path $p)) { Save-ExeIcon $p $dest; $how = "$($a.target).exe icon" }
    }
  } catch { $how = $null }
  "{0,-12} {1}" -f $a.name, $(if ($how -and (Test-Path $dest)) { "OK  ($how, " + (Get-Item $dest).Length + " bytes) -> icons\$name.png" } else { "no local icon (a drawn icon will be used)" })
}
