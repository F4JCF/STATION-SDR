; Installateur Windows de STATION-SDR (Inno Setup 6).
; Installation par utilisateur, sans droits d'administrateur : la mise à jour
; automatique peut ainsi remplacer STATION-SDR.exe dans son dossier.
; Les réglages restent dans %APPDATA%\STATION-SDR (non touchés).
#ifndef MyVersion
  #define MyVersion "0.0.0"
#endif

[Setup]
AppId={{D88D7FC7-2B65-4F3E-8DB0-514082E0DA39}
AppName=STATION-SDR
AppVersion={#MyVersion}
AppPublisher=F4JCF
AppPublisherURL=https://github.com/F4JCF/STATION-SDR
DefaultDirName={localappdata}\Programs\STATION-SDR
DefaultGroupName=STATION-SDR
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=STATION-SDR-Setup-{#MyVersion}
SetupIconFile=station_sdr\assets\icon.ico
UninstallDisplayIcon={app}\STATION-SDR.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\STATION-SDR.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\STATION-SDR"; Filename: "{app}\STATION-SDR.exe"
Name: "{group}\{cm:UninstallProgram,STATION-SDR}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\STATION-SDR"; Filename: "{app}\STATION-SDR.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\STATION-SDR.exe"; Description: "{cm:LaunchProgram,STATION-SDR}"; Flags: nowait postinstall skipifsilent
