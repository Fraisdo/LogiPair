$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName 'LogiPair' -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName 'LogiPair' -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName 'LogiPair' -Confirm:$false
}
