$ErrorActionPreference = 'Stop'
$Path = Join-Path $PSScriptRoot '.secrets\bot-token.dpapi'
if (-not (Test-Path -LiteralPath $Path)) { throw 'Encrypted bot token is not configured.' }
$Secure = Get-Content -Raw -LiteralPath $Path | ConvertTo-SecureString
$Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
try {
    [Console]::Out.Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer))
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
}
