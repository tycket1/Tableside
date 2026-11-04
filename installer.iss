; Inno Setup script for TablesideSetup.exe  (compile with ISCC.exe installer.iss)
#define AppName "Tableside"
#define AppVersion "1.0.0"

[Setup]
AppId={{8D3C2F1E-5B7A-4C9E-9F21-7A1B3C5D7E90}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Tableside
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=TablesideSetup
SetupIconFile=app\tableside.ico
UninstallDisplayIcon={app}\Tableside.exe
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
CloseApplicationsFilter=*.exe,*.dll,*.pyd

[Tasks]
Name: "startup"; Description: "Start Tableside with Windows (so it can offer to record when a meeting starts)"; GroupDescription: "Meetings:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "dist\Tableside\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; The AppUserModelID matches the one the app sets, so the taskbar button pins correctly.
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\Tableside.exe"; AppUserModelID: "Tableside.MeetingNotes"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Tableside.exe"; AppUserModelID: "Tableside.MeetingNotes"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Tableside"; ValueData: """{app}\Tableside.exe"" --background"; Flags: uninsdeletevalue; Tasks: startup

[UninstallRun]
Filename: "{cmd}"; Parameters: "/c taskkill /im Tableside.exe /f"; Flags: runhidden; RunOnceId: "StopTableside"

[Run]
Filename: "{app}\Tableside.exe"; Description: "Open Tableside now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
// Quit any running copy (it often sits hidden in the tray) before files are replaced.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /t /im Tableside.exe', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Sleep(800);
  Result := '';
end;
