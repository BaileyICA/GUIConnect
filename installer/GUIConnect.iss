; Per-user installer: no admin needed, so config.yaml, pki\ and the log stay writable next to the exe.
; Build with build-installer.ps1 (needs dist\GUIConnect.exe, which bundles Python and every library).
#define AppName "GUIConnect"
#define AppVersion "1.0.0"

[Setup]
AppId={{6B0E4C1A-3F52-4D7E-9A1B-5C8D2E7F0A41}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=GUIConnect-Setup
SetupIconFile=..\kepclone\kepclone.ico
UninstallDisplayIcon={app}\GUIConnect.exe
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes
WizardStyle=modern

[Tasks]
Name: desktopicon; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: startmenuicon; Description: "Create a &Start menu shortcut"; GroupDescription: "Shortcuts:"
Name: firewall; Description: "Allow OPC UA clients through Windows Firewall (TCP 4840, asks for admin)"; GroupDescription: "Network:"; Flags: unchecked

[Files]
Source: "..\dist\GUIConnect.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config.example.yaml"; DestDir: "{app}"; Flags: ignoreversion
; Starter project; never overwritten on upgrade, never removed on uninstall.
Source: "..\config.example.yaml"; DestDir: "{app}"; DestName: "config.yaml"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\GUIConnect.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\GUIConnect.exe"; WorkingDir: "{app}"; Tasks: startmenuicon

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#AppName} OPC UA"" dir=in action=allow protocol=TCP localport=4840 program=""{app}\GUIConnect.exe"""; Verb: runas; Flags: runhidden shellexec waituntilterminated; Tasks: firewall
Filename: "{app}\GUIConnect.exe"; WorkingDir: "{app}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
