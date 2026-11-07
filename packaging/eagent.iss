; EAgent Studio 安装包配置（Inno Setup 6）
; 先执行 build.ps1 生成 dist\EAgentStudio，再编译本文件。

#define AppName "EAgent Studio"
#define AppVersion "0.2.0"
#define AppExeName "EAgentStudio.exe"
#define AppPublisher "EAgent"

[Setup]
AppId={{8F5B2F0C-6A0E-4E6B-9F3B-2C7D1A5E9B41}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
SetupIconFile=..\eagent_bridge\ui\favicon.ico
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=EAgentStudio-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\{#AppExeName}
SetupLogging=yes

[Files]
Source: "..\dist\EAgentStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{userdocs}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{userdocs}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动 {#AppName}"; WorkingDir: "{userdocs}"; Flags: nowait postinstall skipifsilent
