; Agents Web installer for Windows 10 and 11 (NSIS 3). Installs for the current user, no administrator rights.
; makensis -DVERSION=2.0.0 -DSTAGE=<folder with AgentsWeb.exe, app\ and python\> -DOUT=<file.exe> installer.nsi
; Silent: /S. /UPDATE (used by the updater) and /START start Agents Web when done; /S alone does not.
; https://github.com/gabrielmf1998/Agents-Web (MIT license)
Unicode true
SetCompressor /SOLID lzma
RequestExecutionLevel user
ManifestDPIAware true

!define APPNAME "Agents Web"
!define REGKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\AgentsWeb"
!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

Name "${APPNAME}"
OutFile "${OUT}"
InstallDir "$LOCALAPPDATA\Programs\Agents Web"
InstallDirRegKey HKCU "${REGKEY}" "InstallLocation"
BrandingText "${APPNAME} ${VERSION}"
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "FileDescription" "${APPNAME} installer"
VIAddVersionKey "LegalCopyright" "MIT license"

!define MUI_ICON "${STAGE}\agents-web.ico"
!define MUI_UNICON "${STAGE}\agents-web.ico"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\AgentsWeb.exe"
!define MUI_FINISHPAGE_RUN_PARAMETERS "--open"
!define MUI_FINISHPAGE_RUN_TEXT "Start Agents Web"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "Agents Web"
  ; a running copy is stopped first (it stops its agents too), then started again by /UPDATE or /START
  ${If} ${FileExists} "$INSTDIR\AgentsWeb.exe"
    ExecWait '"$INSTDIR\AgentsWeb.exe" --quit'
  ${EndIf}
  SetOutPath "$INSTDIR"
  ; the old app and Python go away first, so files removed in a new version do not linger. A tray or server
  ; that is still exiting keeps its files locked for a moment, so try for up to 15 seconds.
  StrCpy $1 0
  ${Do}
    ClearErrors
    Delete "$INSTDIR\AgentsWeb.exe"
    RMDir /r "$INSTDIR\app"
    RMDir /r "$INSTDIR\python"
    ${IfNot} ${FileExists} "$INSTDIR\AgentsWeb.exe"
    ${AndIfNot} ${FileExists} "$INSTDIR\python\*.*"
      ${ExitDo}
    ${EndIf}
    IntOp $1 $1 + 1
    ${If} $1 >= 30
      MessageBox MB_ICONSTOP "Agents Web is still running and its files are in use. Quit it from the tray and run the installer again." /SD IDOK
      Abort
    ${EndIf}
    Sleep 500
  ${Loop}
  File "${STAGE}\AgentsWeb.exe"
  File "${STAGE}\agents-web.ico"
  File "${STAGE}\LICENSE.txt"
  SetOutPath "$INSTDIR\app"
  File /r "${STAGE}\app\*.*"
  SetOutPath "$INSTDIR\python"
  File /r "${STAGE}\python\*.*"
  SetOutPath "$INSTDIR"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortCut "$SMPROGRAMS\Agents Web.lnk" "$INSTDIR\AgentsWeb.exe" "--open" "$INSTDIR\agents-web.ico" 0 \
    SW_SHOWNORMAL "" "Use the Claude Code and Antigravity CLIs from your browser or phone"
  ; a start on login set in the tray menu points to this folder
  ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Agents Web"
  ${If} $0 != ""
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Agents Web" '"$INSTDIR\AgentsWeb.exe"'
  ${EndIf}

  WriteRegStr HKCU "${REGKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${REGKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${REGKEY}" "Publisher" "Agents Web"
  WriteRegStr HKCU "${REGKEY}" "URLInfoAbout" "https://github.com/gabrielmf1998/Agents-Web"
  WriteRegStr HKCU "${REGKEY}" "DisplayIcon" "$INSTDIR\agents-web.ico"
  WriteRegStr HKCU "${REGKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${REGKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${REGKEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${REGKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${REGKEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${REGKEY}" "EstimatedSize" "$0"

  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/UPDATE" $R1
  ${IfNot} ${Errors}
    Exec '"$INSTDIR\AgentsWeb.exe"'
  ${Else}
    ClearErrors
    ${GetOptions} $R0 "/START" $R1
    ${IfNot} ${Errors}
      Exec '"$INSTDIR\AgentsWeb.exe" --open'
    ${EndIf}
  ${EndIf}
SectionEnd

Section "Uninstall"
  ExecWait '"$INSTDIR\AgentsWeb.exe" --quit'
  Sleep 1000
  Delete "$SMPROGRAMS\Agents Web.lnk"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Agents Web"
  DeleteRegKey HKCU "${REGKEY}"
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\python"
  Delete "$INSTDIR\AgentsWeb.exe"
  Delete "$INSTDIR\agents-web.ico"
  Delete "$INSTDIR\LICENSE.txt"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  ; chats and settings stay unless asked (/PURGE when silent)
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/PURGE" $R1
  ${If} ${Errors}
    ${IfNot} ${Silent}
      MessageBox MB_YESNO|MB_ICONQUESTION "Also delete your chats, uploads and settings?$\n$\n$APPDATA\agents-web$\n$LOCALAPPDATA\agents-web" IDNO keep
    ${Else}
      Goto keep
    ${EndIf}
  ${EndIf}
  RMDir /r "$APPDATA\agents-web"
  RMDir /r "$LOCALAPPDATA\agents-web"
  keep:
SectionEnd
