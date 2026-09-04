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
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\dist\LogiPair.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\scripts\windows\install-task.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\scripts\windows\remove-task.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\LogiPair Status"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--status"
Name: "{group}\LogiPair Diagnostics"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--diagnostics"

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\install-task.ps1"" -ExePath ""{app}\{#MyAppExeName}"""; Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\remove-task.ps1"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveLogiPairTask"
