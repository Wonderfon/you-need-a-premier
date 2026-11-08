Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$Root = $PSScriptRoot
$TokenPath = Join-Path $Root '.secrets\bot-token.dpapi'
$Form = New-Object System.Windows.Forms.Form
$Form.Text = 'Discord Bot Token Setup'
$Form.ClientSize = [System.Drawing.Size]::new(520, 190)
$Form.StartPosition = 'CenterScreen'
$Form.FormBorderStyle = 'FixedDialog'
$Form.MaximizeBox = $false
$Form.MinimizeBox = $false

$Label = New-Object System.Windows.Forms.Label
$Label.Text = 'Token 仅保存在本机，并使用当前 Windows 用户的 DPAPI 加密。请勿将 Token 发到聊天中；输入内容不会显示。'
$Label.Location = New-Object System.Drawing.Point(16, 16)
$Label.Size = New-Object System.Drawing.Size(488, 48)
$Form.Controls.Add($Label)

$TokenBox = New-Object System.Windows.Forms.TextBox
$TokenBox.Location = New-Object System.Drawing.Point(16, 72)
$TokenBox.Size = New-Object System.Drawing.Size(488, 26)
$TokenBox.UseSystemPasswordChar = $true
$TokenBox.MaxLength = 4096
$Form.Controls.Add($TokenBox)

$SaveButton = New-Object System.Windows.Forms.Button
$SaveButton.Text = '保存'
$SaveButton.Location = New-Object System.Drawing.Point(328, 126)
$SaveButton.Size = New-Object System.Drawing.Size(82, 30)
$Form.Controls.Add($SaveButton)

$CancelButton = New-Object System.Windows.Forms.Button
$CancelButton.Text = '取消'
$CancelButton.Location = New-Object System.Drawing.Point(422, 126)
$CancelButton.Size = New-Object System.Drawing.Size(82, 30)
$CancelButton.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$Form.Controls.Add($CancelButton)
$Form.AcceptButton = $SaveButton
$Form.CancelButton = $CancelButton

$SaveButton.Add_Click({
    $PlainText = $TokenBox.Text
    if ([string]::IsNullOrWhiteSpace($PlainText) -or $PlainText -match '\s') {
        $PlainText = $null
        $TokenBox.Clear()
        [void][System.Windows.Forms.MessageBox]::Show(
            'Token 不能为空，也不能包含空格或换行。', '无法保存',
            [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning
        )
        return
    }

    $Secure = $null
    try {
        $Secure = New-Object System.Security.SecureString
        foreach ($Character in $PlainText.ToCharArray()) { $Secure.AppendChar($Character) }
        $Secure.MakeReadOnly()
        $Ciphertext = ConvertFrom-SecureString -SecureString $Secure
        $PlainText = $null
        $TokenBox.Clear()

        $SecretsDirectory = Split-Path -Parent $TokenPath
        New-Item -ItemType Directory -Path $SecretsDirectory -Force | Out-Null
        [System.IO.File]::WriteAllText($TokenPath, $Ciphertext)
        $Ciphertext = $null

        [void][System.Windows.Forms.MessageBox]::Show(
            'Token 已为当前 Windows 用户加密并保存在本机。', '保存完成',
            [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information
        )
        $Form.DialogResult = [System.Windows.Forms.DialogResult]::OK
        $Form.Close()
    }
    catch {
        $PlainText = $null
        $TokenBox.Clear()
        [void][System.Windows.Forms.MessageBox]::Show(
            '保存失败。请检查本机文件权限后重试。', '无法保存',
            [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error
        )
    }
    finally {
        if ($null -ne $Secure) { $Secure.Dispose() }
        $PlainText = $null
        $Ciphertext = $null
        $TokenBox.Clear()
    }
}.GetNewClosure())

$Form.Add_FormClosed({ $TokenBox.Clear() }.GetNewClosure())

[void]$Form.ShowDialog()
$TokenBox.Clear()
$Form.Dispose()
