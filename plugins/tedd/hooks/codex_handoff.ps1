#requires -Version 5.1
# Codex uses the selected PowerShell session shell. Its native-command launcher
# can replace inherited pipes with console handles, so launch Python directly.
$ErrorActionPreference = 'Stop'
if ($env:TEDD_NO_LOCAL_WRITES -eq '1') { exit 0 }

$teddInput = [Console]::OpenStandardInput()
$teddBuffer = New-Object byte[] (2 * 1024 * 1024 + 1)
$teddCount = 0
$teddProcess = [Diagnostics.Process]::new()
$teddStarted = $false
try {
    while ($teddCount -lt $teddBuffer.Length) {
        $teddRead = $teddInput.Read($teddBuffer, $teddCount, $teddBuffer.Length - $teddCount)
        if ($teddRead -eq 0) { break }
        $teddCount += $teddRead
    }
    if ($teddCount -eq $teddBuffer.Length) {
        [Console]::Out.WriteLine('{"systemMessage":"Tedd handoff input exceeds its limit; report pending memory."}')
        exit 0
    }

    $teddScript = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../skills/tedd/scripts/tedd_handoff_hook.py'))
    # Resolve through PATH, not CreateProcess's implicit project-directory search.
    $teddProcess.StartInfo.FileName = (Get-Command py.exe -CommandType Application -ErrorAction Stop).Source
    $teddProcess.StartInfo.Arguments = '-3 -B "' + $teddScript + '" --host codex'
    $teddProcess.StartInfo.UseShellExecute = $false
    $teddProcess.StartInfo.CreateNoWindow = $true
    $teddProcess.StartInfo.RedirectStandardInput = $true
    $teddProcess.StartInfo.RedirectStandardOutput = $true
    $teddProcess.StartInfo.RedirectStandardError = $true
    $teddProcess.StartInfo.StandardOutputEncoding = [Text.UTF8Encoding]::new($false)
    $teddProcess.StartInfo.StandardErrorEncoding = [Text.UTF8Encoding]::new($false)
    $null = $teddProcess.Start()
    $teddStarted = $true
    # Drain both outputs while delivering the bounded, unchanged UTF-8 bytes.
    $teddOutput = $teddProcess.StandardOutput.ReadToEndAsync()
    $teddError = $teddProcess.StandardError.ReadToEndAsync()
    $teddProcess.StandardInput.BaseStream.Write($teddBuffer, 0, $teddCount)
    $teddProcess.StandardInput.Close()
    $teddProcess.WaitForExit()
    [Console]::Out.Write($teddOutput.GetAwaiter().GetResult())
    [Console]::Error.Write($teddError.GetAwaiter().GetResult())
    exit $teddProcess.ExitCode
} catch [IO.IOException], [ComponentModel.Win32Exception], [InvalidOperationException], [Management.Automation.CommandNotFoundException] {
    [Console]::Out.WriteLine('{"systemMessage":"Tedd handoff launcher unavailable; report pending memory."}')
    exit 0
} finally {
    $teddInput.Dispose()
    [Array]::Clear($teddBuffer, 0, $teddBuffer.Length)
    if ($teddStarted -and -not $teddProcess.HasExited) {
        $teddProcess.Kill()
        $teddProcess.WaitForExit()
    }
    $teddProcess.Dispose()
}
