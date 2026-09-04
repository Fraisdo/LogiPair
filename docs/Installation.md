# Windows installation and real-device validation

## Installation

1. Exit CleverSwitch or any OpenLogi instance configured with `host_switch_targets`. Keep Logi Options+ running if desired.
2. Run `LogiPair-Setup-1.0.0.exe` as the normal user.
3. Confirm the per-user task exists:

   ```powershell
   Get-ScheduledTask -TaskName LogiPair | Select-Object TaskName, State
   ```

4. Wait a few seconds, then inspect readiness:

   ```powershell
   & "$env:LOCALAPPDATA\Programs\LogiPair\LogiPair.exe" --status
   ```

Both devices must say `READY` and the pair must say `PAIR_READY` before evaluating a switch.

## Real-device test (manual, 8 steps)

This procedure intentionally leaves the physical button presses to the user.

1. Install LogiPair on each Windows host that should forward a departure event.
2. On the current host, confirm `PAIR_READY` with `--status`.
3. Note the current time for log correlation.
4. Press MX Keys Easy-Switch 1, 2, or 3 once.
5. Confirm that MX Anywhere 3S follows to that same host without pressing its underside button.
6. On the destination host, wait for `PAIR_READY`; press the MX Keys button for the original host and confirm that the mouse follows back.
7. Optional: press the physical MX Anywhere 3S Easy-Switch button once. On the destination host run `--diagnostics`. Reverse support is proven only if `reverse_notifications_observed` becomes `true` and the keyboard follows.
8. If any step misses, stop testing and collect the rotating logs with the commands in the README. Preserve the timestamp and target host number.

## Upgrade and uninstall

Running a newer installer upgrades the files in place and recreates the same Scheduled Task. Runtime cache and logs remain under `%LOCALAPPDATA%\LogiPair`.

Uninstall from **Settings → Apps → Installed apps → LogiPair**. The uninstaller stops/removes the `LogiPair` Scheduled Task and removes the program directory. To remove retained diagnostics afterward:

```powershell
Remove-Item -LiteralPath "$env:LOCALAPPDATA\LogiPair" -Recurse
```

Only run that final command if the retained logs/cache are no longer needed.
