# LogiPair

LogiPair is a Windows 11-only background utility for one setup: **Logitech MX Keys + MX Anywhere 3S**.
An Easy-Switch notification from one device is forwarded only to the other device, so the two act as a logical pair.

This is a focused fork/rewrite of [CleverSwitch](https://github.com/MikalaiBarysevich/CleverSwitch), licensed under GPL-3.0-or-later. It retains the useful HID++ protocol knowledge while replacing the generic pub/sub and cross-platform runtime with a small Windows actor model.

## Reliability contract

- A `TransportActor` is the only thread allowed to open, read, write, reconnect, or close its HID handles.
- Native hidapi operations, including enumeration, are additionally serialized process-wide.
- `PairController` handles one switch transaction at a time and never writes `CHANGE_HOST` back to the source.
- A valid event remains a memory-only, latest-wins pending intent for up to three seconds while its peer reconnects.
- `switch_capable` is reached as soon as the live target, slot, and cached/verified `CHANGE_HOST` index are available; `READY` remains stricter.
- `READY` means critical feature discovery succeeded and all MX Keys Easy-Switch CIDs received reporting ACKs.
- Bluetooth writes use hidapi 0.15 `hid_send_output_report` (`HidD_SetOutputReport`); receiver writes use `hid_write`.
- `CHANGE_HOST` is fire-and-forget. LogiPair records the completed write and does not wait for a response from a device that is leaving the host.
- Power resume, HID arrival/removal, path changes, malformed packets, transport loss, and reporting-flag removal all converge through idempotent recovery.
- Shutdown waits for every HID owner to close its handles. If an actor cannot stop, LogiPair deliberately skips `hid_exit`.

The recovery backoff is `100 ms, 250 ms, 500 ms, 1 s, 2 s, 5 s, 10 s, 30 s` (capped).

## Supported behavior

| Path | Status |
|---|---|
| MX Keys Easy-Switch → MX Anywhere 3S `CHANGE_HOST` | Implemented and simulated by automated tests |
| MX Anywhere 3S notification → MX Keys `CHANGE_HOST` | Implemented only when a real x1814 notification is observed |
| Bluetooth direct | Implemented; uses output reports |
| Bolt/Unifying receiver | Implemented; short and long collections share one actor/owner |
| Logi Options+ in parallel | Designed for non-exclusive HID access and throttled re-arm |

The reverse mouse → keyboard path is intentionally capability-driven. LogiPair does not pretend that the physical Easy-Switch button on every MX Anywhere 3S firmware emits a usable notification. `--diagnostics` changes `reverse_notifications_observed` to `true` only after such a packet is actually seen.

## Install

Run `LogiPair-Setup-1.0.0.exe`. The per-user installer:

- installs to `%LOCALAPPDATA%\Programs\LogiPair`;
- creates the hidden per-user Scheduled Task `LogiPair` at logon;
- configures restart-on-failure and starts the task immediately;
- does not require elevation and does not change `PATH`;
- removes the Scheduled Task during uninstall.

Runtime data remains in `%LOCALAPPDATA%\LogiPair` so logs survive upgrades. See [Installation.md](docs/Installation.md) for verification and removal.

Do not run another Easy-Switch forwarder (CleverSwitch or OpenLogi `host_switch_targets`) at the same time. Logi Options+ may remain running.

## Diagnostics

```powershell
& "$env:LOCALAPPDATA\Programs\LogiPair\LogiPair.exe" --status
& "$env:LOCALAPPDATA\Programs\LogiPair\LogiPair.exe" --diagnostics
& "$env:LOCALAPPDATA\Programs\LogiPair\LogiPair.exe" --version
```

Logs rotate as five files of up to 3 MB at `%LOCALAPPDATA%\LogiPair\logs\logipair.log`.

```powershell
Get-Content "$env:LOCALAPPDATA\LogiPair\logs\logipair.log" -Tail 250
Get-ChildItem "$env:LOCALAPPDATA\LogiPair\logs\logipair.log*" |
  Sort-Object LastWriteTime |
  Compress-Archive -DestinationPath "$env:USERPROFILE\Desktop\LogiPair-logs.zip" -Force
```

Each switch line includes source, zero-based target, peer, event-to-enqueue latency, event-to-write latency, and write result. Initialization logs also expose `connected_to_switch_capable_ms` and `connected_to_ready_ms`.

## Build and test

Requirements: Windows 11, Python 3.10+, PowerShell, and Inno Setup 6.

```powershell
& .\scripts\windows\build.ps1
```

The script creates an isolated venv, installs pinned build tools, downloads the official hidapi 0.15.0 Windows archive, verifies SHA-256 `D18C43EC9506A2F6D7FAA9C7E0A342C4B64FBAE521B71B5D4AC0777FD24DDA93`, runs lint/tests, builds the one-file EXE, smoke-tests it, and compiles the Inno installer. Outputs are written to `dist\` with `build-manifest.json` hashes.

The automated suite covers source exclusion, latest-wins pending intents and expiry, reverse routing, duplicate suppression, single-owner I/O, shutdown ordering, disconnect during switch, failed peer writes and bounded recovery, cached P0 preemption, READY-after-ACK, external flag removal, invalid cache recovery, malformed packets, receiver ownership, and 2,000 serialized switch transactions.
