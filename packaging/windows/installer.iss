; Inno Setup script: wraps the PyInstaller build (dist\Mission Control\)
; into a per-user installer that needs no administrator rights.
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "Mission Control"
#define AppExe "Mission Control.exe"

[Setup]
AppId={{B4E7B7A2-9C3E-4D5F-8A1B-2C3D4E5F6A7B}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=hicka
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
DisableDirPage=yes
; paths (dist\, assets\, OutputDir) resolve from the repo root, two up from this .iss
SourceDir=..\..
OutputDir=.
OutputBaseFilename=MissionControl-Setup
SetupIconFile=assets\app.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
WizardStyle=modern

[Files]
Source: "dist\Mission Control\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
