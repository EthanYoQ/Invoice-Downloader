#define AppName "InvoiceFlowAI"
#define AppPublisher "HBU Digital"

#ifndef AppVersion
  #define AppVersion "0.0.0.0"
#endif

#ifndef SourceDir
  #error "SourceDir define is required."
#endif

#ifndef OutputDir
  #error "OutputDir define is required."
#endif

#ifndef OutputBaseName
  #define OutputBaseName "InvoiceFlowAI-Setup-unsigned"
#endif

[Setup]
AppId={{9F7B3171-49F1-4B5D-9D7D-788DC39EBA6D}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\InvoiceFlow AI
DefaultGroupName={#AppName}
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseName}
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
PrivilegesRequired=admin
UninstallDisplayIcon={app}\InvoiceFlowAI.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\InvoiceFlowAI.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\InvoiceFlowAI.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\InvoiceFlowAI.exe"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
