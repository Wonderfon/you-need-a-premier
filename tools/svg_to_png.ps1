# Render each SVG in a folder to a PNG next to it with headless Edge (Windows). Usage: svg_to_png.ps1 <folder>
param([Parameter(Mandatory = $true)][string]$Folder)
$edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
foreach ($svg in Get-ChildItem -LiteralPath $Folder -Filter '*.svg') {
    $head = (Get-Content -LiteralPath $svg.FullName -TotalCount 1)
    $w = [regex]::Match($head, 'width="(\d+)"').Groups[1].Value
    $h = [regex]::Match($head, 'height="(\d+)"').Groups[1].Value
    $png = [IO.Path]::ChangeExtension($svg.FullName, '.png')
    & $edge --headless --disable-gpu --hide-scrollbars --force-device-scale-factor=2 "--window-size=$w,$h" "--screenshot=$png" ([Uri]$svg.FullName).AbsoluteUri | Out-Null
    Write-Output "$($svg.Name) -> $([IO.Path]::GetFileName($png)) ($w x $h)"
}
