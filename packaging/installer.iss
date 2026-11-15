; Inno Setup script: builds FlowDownloads-Setup.exe from the PyInstaller output.
; iscc /DAppVersion=1.0.0 packaging\installer.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Flow Downloads"
#define AppExe "Flow Downloads.exe"

[Setup]
AppId={{B4E1C0F3-6A2D-4F5B-9C7E-3D8A1F2E6B90}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Flow Downloads
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Installs for the current user: no administrator password needed.
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=FlowDownloads-Setup
SetupIconFile=..\flowdl\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "portuguese"; MessagesFile: "compiler:Languages\Portuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na área de trabalho"; GroupDescription: "Atalhos:"
Name: "autostart"; Description: "Iniciar com o Windows (em segundo plano, quase sem gastar memória)"; GroupDescription: "Arranque:"

[Files]
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\browser-extension\*"; DestDir: "{app}\browser-extension"; Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; Files of the previous version's program (an update must not leave old ones behind).
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Pasta da extensão do navegador"; Filename: "{app}\browser-extension"
Name: "{group}\Desinstalar {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Same entry the app uses for "Configurações → Sistema → Iniciar com o computador".
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "FlowDownloads"; ValueData: """{app}\{#AppExe}"" --background"; Flags: uninsdeletevalue; Tasks: autostart
; The app registers itself so the browser can start it when it is closed (native messaging);
; these only make sure the entries go away when the app is uninstalled.
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\com.flowdownloads.app"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\com.flowdownloads.app"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Microsoft\Edge\NativeMessagingHosts\com.flowdownloads.app"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Chromium\NativeMessagingHosts\com.flowdownloads.app"; Flags: uninsdeletekey dontcreatekey

[Run]
Filename: "{app}\{#AppExe}"; Description: "Abrir o Flow Downloads"; Flags: nowait postinstall skipifsilent
; An update installed by the app itself (/UPDATE=1, silent): start it again in the background.
Filename: "{app}\{#AppExe}"; Parameters: "--background"; Flags: nowait; Check: IsAppUpdate
Filename: "{app}\browser-extension"; Description: "Abrir a pasta da extensão do navegador (para instalar no Brave)"; Flags: postinstall shellexec skipifsilent unchecked

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM ""{#AppExe}"" /F"; Flags: runhidden; RunOnceId: "StopApp"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function IsAppUpdate: Boolean;
begin
  Result := ExpandConstant('{param:UPDATE|0}') = '1';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    { "Start with Windows" may have been switched on in the app (not by this installer). }
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'FlowDownloads');
    { The download list (with site logins), settings and logs: the user decides. }
    if (not UninstallSilent) and DirExists(ExpandConstant('{userappdata}\FlowDownloads')) and
       (MsgBox('Apagar também a lista de downloads, as configurações e os registos do Flow Downloads?' +
               #13#10#13#10 + 'Os ficheiros que baixou ficam onde estão.',
               mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
      DelTree(ExpandConstant('{userappdata}\FlowDownloads'), True, True, True);
  end;
end;
