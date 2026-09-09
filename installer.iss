; Coin Monitor - Windows installer script (Inno Setup 6)
; Compiled automatically by .github/workflows/build-exe.yml on a clean
; GitHub-hosted Windows machine - you normally never need to run this
; yourself. If you do want to build it locally: install Inno Setup
; (https://jrsoftware.org/isdl.php), build dist\CoinMonitor.exe first
; (see build_exe.bat), then open/compile this file with ISCC or the Inno
; Setup GUI.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif
#define MyAppName "Coin Monitor"
#define MyAppPublisher "Coin Monitor"
#define MyAppExeName "CoinMonitor.exe"

[Setup]
AppId={{B7E2A6B0-7C0B-4C6E-9B7E-COINMONITOR1}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Per-user install under %LOCALAPPDATA% - no admin/UAC prompt needed,
; matches how many everyday consumer apps (chat apps, browsers, etc.)
; install themselves for a single user.
DefaultDirName={localappdata}\Programs\CoinMonitor
DefaultGroupName=Coin Monitor
PrivilegesRequired=lowest
OutputDir=installer_output
OutputBaseFilename=CoinMonitorSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
DisableProgramGroupPage=yes
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "startupicon"; Description: "컴퓨터 켤 때 자동으로 실행"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\CoinMonitor.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Coin Monitor"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,Coin Monitor}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Coin Monitor"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\Coin Monitor"; Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,Coin Monitor}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Intentionally NOT deleting the user's data folder (%APPDATA%\CoinMonitor,
; which holds coin_monitor.db - their collected data, API keys, settings)
; on uninstall - losing that silently would be a nasty surprise. If they
; really want a clean wipe they can delete that folder manually afterward.
