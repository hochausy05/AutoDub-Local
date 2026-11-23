$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$requestText = [Console]::In.ReadToEnd()
$request = ConvertFrom-Json -InputObject $requestText
if ([string]::IsNullOrWhiteSpace($request.nonce) -or $request.purpose -notin @('source', 'relink') -or [string]::IsNullOrWhiteSpace($request.project_id) -or ($request.purpose -eq 'relink' -and [string]::IsNullOrWhiteSpace($request.asset_id))) {
    exit 2
}
Add-Type -AssemblyName System.Windows.Forms
$dialog = [System.Windows.Forms.OpenFileDialog]::new()
$dialog.Title = if ($request.purpose -eq 'relink') { 'Chọn lại video nguồn' } else { 'Chọn video nguồn' }
$dialog.Filter = 'Video files|*.mp4;*.mkv;*.mov;*.avi;*.webm;*.m4v;*.mpeg;*.mpg;*.ts;*.wmv|All files|*.*'
$dialog.Multiselect = $false
try {
    $result = $dialog.ShowDialog()
    $payload = if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
        @{ nonce = $request.nonce; cancelled = $false; path = $dialog.FileName }
    } else {
        @{ nonce = $request.nonce; cancelled = $true }
    }
    [Console]::Out.Write((ConvertTo-Json -InputObject $payload -Compress))
} finally {
    $dialog.Dispose()
}
