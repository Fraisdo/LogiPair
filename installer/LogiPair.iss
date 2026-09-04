#define MyAppName "LogiPair"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Fraisdo"
#define MyAppExeName "LogiPair.exe"

[Setup]
AppId={{9AB77F4E-EB0A-4D56-99FD-C9E29F9CC830}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=LogiPair-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\logipair.ico
CloseApplications=yes
RestartApplications=no
; The executable carries the icon as a resource, so Add/Remove Programs picks it up.
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\dist\LogiPair.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\scripts\windows\install-task.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\scripts\windows\remove-task.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
; Shipped alongside the executable so a future tray icon can reuse the same asset.
Source: "..\assets\logipair.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\LogiPair Status"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--status"; IconFilename: "{app}\logipair.ico"
Name: "{group}\LogiPair Diagnostics"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--diagnostics"; IconFilename: "{app}\logipair.ico"

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\install-task.ps1"" -ExePath ""{app}\{#MyAppExeName}"""; Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\remove-task.ps1"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveLogiPairTask"
