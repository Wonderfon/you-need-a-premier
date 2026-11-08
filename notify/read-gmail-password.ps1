$ErrorActionPreference = 'Stop'
$Path = Join-Path $PSScriptRoot '.secrets\gmail-app-password.dpapi'
if (-not (Test-Path -LiteralPath $Path)) { throw 'Gmail App Password is not configured; run set-gmail-password.ps1 first.' }
$Secure = (Get-Content -Raw -LiteralPath $Path).Trim() | ConvertTo-SecureString
$Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
try {
    [Console]::Out.Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer))
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
}
