/* AgentsWeb.exe: tray icon for Agents Web on Windows 10 and 11.
 * Starts the server with the Python that ships next to it, inside a job object: quitting the tray (or the tray
 * dying in any way) stops the server and every agent it started. Same menu and colors as the Linux tray.
 * Build: x86_64-w64-mingw32-gcc -O2 -municode -mwindows -o AgentsWeb.exe agents-web-tray.c res.o \
 *        -lws2_32 -lshell32 -luser32 -lgdi32 -ladvapi32
 * Options: --open opens the page once the service answers (a second launch only opens the page),
 *          --quit stops a running tray and waits for it, --version prints the version.
 * https://github.com/gabrielmf1998/Agents-Web (MIT license) */
#define WIN32_LEAN_AND_MEAN
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <shellapi.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#define CLASSE L"AgentsWebTray"
#define WM_BANDEJA (WM_APP + 1)
#define RUN_KEY L"Software\\Microsoft\\Windows\\CurrentVersion\\Run"
#define RUN_VALOR L"Agents Web"

enum { INICIANDO, OK, PARADO };
enum { M_ABRIR = 1, M_CEL, M_REINICIA, M_AUTO, M_SAIR };

static int PORTA = 47830;
static HWND jan;
static HANDLE job, filho;
static DWORD pid_filho;
static NOTIFYICONDATAW nid;
static UINT msg_barra;
static HICON icone;
static int estado = -1, tarefas, saindo, reinicios, abrir_ao_subir, reiniciando;
static ULONGLONG janela;
static wchar_t base[MAX_PATH], pythonw[MAX_PATH], script[MAX_PATH], arqlog[MAX_PATH], exe[MAX_PATH],
    dados[MAX_PATH], motivo[160];

/* ------------------------------------------------------------ icon (the same drawing as on Linux) */
#define T 32
static HICON desenha(void) {
    unsigned r, g, b;
    switch (estado) {
    case OK: if (tarefas) { r = 255; g = 255; b = 255; } else { r = 0x6f; g = 0xdb; b = 0x7f; } break;
    case INICIANDO: r = 0xe8; g = 0xc6; b = 0x6a; break;
    default: r = 0xff; g = 0x55; b = 0x55;
    }
    BITMAPV5HEADER bi = {0};
    bi.bV5Size = sizeof bi; bi.bV5Width = T; bi.bV5Height = -T; bi.bV5Planes = 1; bi.bV5BitCount = 32;
    bi.bV5Compression = BI_BITFIELDS;
    bi.bV5RedMask = 0x00FF0000; bi.bV5GreenMask = 0x0000FF00; bi.bV5BlueMask = 0x000000FF; bi.bV5AlphaMask = 0xFF000000;
    HDC dc = GetDC(NULL);
    unsigned char *p = NULL;
    HBITMAP cor = CreateDIBSection(dc, (BITMAPINFO *)&bi, DIB_RGB_COLORS, (void **)&p, NULL, 0);
    ReleaseDC(NULL, dc);
    if (!cor) return NULL;
    double c = T / 2.0, R = T * 0.47, anel = T * 0.075, miolo = T * (tarefas && estado == OK ? 0.24 : 0.20);
    for (int y = 0; y < T; y++)
        for (int x = 0; x < T; x++) {
            double d = hypot(x + 0.5 - c, y + 0.5 - c);
            double af = fmin(1, fmax(0, R - d + 0.5));
            double aa = fmin(1, fmax(0, anel / 2 - fabs(d - (R - anel / 2)) + 0.5));
            double am = fmin(1, fmax(0, miolo - d + 0.5));
            double cr = 10, cg = 10, cb = 10;
            cr = cr * (1 - aa) + 235 * aa; cg = cg * (1 - aa) + 235 * aa; cb = cb * (1 - aa) + 235 * aa;
            cr = cr * (1 - am) + r * am; cg = cg * (1 - am) + g * am; cb = cb * (1 - am) + b * am;
            unsigned char *q = p + (y * T + x) * 4; /* BGRA, premultiplied */
            q[0] = (unsigned char)(cb * af); q[1] = (unsigned char)(cg * af); q[2] = (unsigned char)(cr * af);
            q[3] = (unsigned char)(af * 255);
        }
    HBITMAP masc = CreateBitmap(T, T, 1, 1, NULL);
    ICONINFO ii = {TRUE, 0, 0, masc, cor};
    HICON h = CreateIconIndirect(&ii);
    DeleteObject(cor); DeleteObject(masc);
    return h;
}

static void texto_estado(wchar_t *buf, size_t n) {
    if (estado == OK && tarefas)
        _snwprintf(buf, n, L"Agents Web: running, %d active task%s", tarefas, tarefas == 1 ? L"" : L"s");
    else if (estado == OK) _snwprintf(buf, n, L"Agents Web: running");
    else if (estado == INICIANDO) _snwprintf(buf, n, L"Agents Web: starting");
    else _snwprintf(buf, n, L"Agents Web: STOPPED%ls%ls", motivo[0] ? L", " : L"", motivo);
    buf[n - 1] = 0;
}

static void muda(int novo, int ntar) {
    if (novo == estado && ntar == tarefas) return;
    estado = novo; tarefas = ntar;
    HICON velho = icone;
    icone = desenha();
    nid.hIcon = icone;
    texto_estado(nid.szTip, sizeof nid.szTip / sizeof nid.szTip[0]);
    Shell_NotifyIconW(NIM_MODIFY, &nid);
    if (velho) DestroyIcon(velho);
}

static void abre(const wchar_t *sufixo) {
    wchar_t url[128];
    _snwprintf(url, 128, L"http://127.0.0.1:%d/%ls", PORTA, sufixo);
    url[127] = 0;
    ShellExecuteW(NULL, L"open", url, NULL, NULL, SW_SHOWNORMAL);
}

/* ------------------------------------------------------------ the server */
static void inicia(void) {
    wchar_t velho[MAX_PATH + 4];
    _snwprintf(velho, MAX_PATH + 4, L"%ls.1", arqlog);
    velho[MAX_PATH + 3] = 0;
    MoveFileExW(arqlog, velho, MOVEFILE_REPLACE_EXISTING); /* keep the log of the previous run (a crash) */
    SECURITY_ATTRIBUTES sa = {sizeof sa, NULL, TRUE};
    HANDLE log = CreateFileW(arqlog, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa, CREATE_ALWAYS,
                             FILE_ATTRIBUTE_NORMAL, NULL);
    HANDLE nada = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa, OPEN_EXISTING, 0, NULL);
    STARTUPINFOW si = {0};
    si.cb = sizeof si;
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = nada; si.hStdOutput = log; si.hStdError = log;
    PROCESS_INFORMATION pi;
    wchar_t cmd[3 * MAX_PATH];
    _snwprintf(cmd, 3 * MAX_PATH, L"\"%ls\" -X utf8 -u \"%ls\"", pythonw, script);
    cmd[3 * MAX_PATH - 1] = 0;
    wchar_t casa[MAX_PATH];
    if (!GetEnvironmentVariableW(L"USERPROFILE", casa, MAX_PATH)) wcscpy(casa, base);
    BOOL ok = CreateProcessW(pythonw, cmd, NULL, NULL, TRUE, CREATE_SUSPENDED | CREATE_NO_WINDOW, NULL, casa, &si, &pi);
    if (log != INVALID_HANDLE_VALUE) CloseHandle(log);
    if (nada != INVALID_HANDLE_VALUE) CloseHandle(nada);
    if (!ok) {
        _snwprintf(motivo, 160, L"could not start Python (error %lu)", GetLastError());
        muda(PARADO, 0);
        return;
    }
    AssignProcessToJobObject(job, pi.hProcess);
    ResumeThread(pi.hThread);
    CloseHandle(pi.hThread);
    filho = pi.hProcess;
    pid_filho = pi.dwProcessId;
    motivo[0] = 0;
    estado = -1;
    muda(INICIANDO, 0);
}

static void mata_servico(void) {
    if (!filho) return;
    /* the server and the agents it started are all in the job */
    TerminateJobObject(job, 1);
    WaitForSingleObject(filho, 5000);
    CloseHandle(filho);
    filho = NULL;
}

static void reinicia(void) {
    reiniciando = 1; mata_servico(); reiniciando = 0;
    reinicios = 0; inicia();
}

static void sair(void);

/* the server downloaded an installer and exited with code 75: run it from here, outside the job, then quit
 * (the installer starts the new tray when it is done) */
static int roda_instalador(void) {
    wchar_t arq[MAX_PATH + 32], caminho[MAX_PATH], cmd[MAX_PATH + 32];
    char buf[MAX_PATH * 3];
    _snwprintf(arq, MAX_PATH + 32, L"%ls\\updates\\setup.txt", dados);
    arq[MAX_PATH + 31] = 0;
    HANDLE f = CreateFileW(arq, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    if (f == INVALID_HANDLE_VALUE) return 0;
    DWORD n = 0;
    BOOL lido = ReadFile(f, buf, sizeof buf - 1, &n, NULL);
    CloseHandle(f);
    DeleteFileW(arq);
    if (!lido || !n) return 0;
    buf[n] = 0;
    if (!MultiByteToWideChar(CP_UTF8, 0, buf, -1, caminho, MAX_PATH)) return 0;
    size_t t = wcslen(caminho);
    if (t < 5 || _wcsicmp(caminho + t - 4, L".exe")) return 0;
    _snwprintf(cmd, MAX_PATH + 32, L"\"%ls\" /S /UPDATE", caminho);
    cmd[MAX_PATH + 31] = 0;
    STARTUPINFOW si = {0};
    si.cb = sizeof si;
    PROCESS_INFORMATION pi;
    if (!CreateProcessW(caminho, cmd, NULL, NULL, FALSE, CREATE_BREAKAWAY_FROM_JOB, NULL, NULL, &si, &pi)
        && !CreateProcessW(caminho, cmd, NULL, NULL, FALSE, 0, NULL, NULL, &si, &pi))
        return 0;
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 1;
}

static void vigia_filho(void) {
    if (!filho || WaitForSingleObject(filho, 0) != WAIT_OBJECT_0) return;
    DWORD codigo = 0;
    GetExitCodeProcess(filho, &codigo);
    CloseHandle(filho);
    filho = NULL;
    if (codigo == 75 && !saindo && roda_instalador()) { sair(); return; }
    _snwprintf(motivo, 160, L"exited with code %lu (see server.log)", codigo);
    muda(PARADO, 0);
    if (saindo || reiniciando) return;
    ULONGLONG agora = GetTickCount64();
    if (agora - janela > 60000) { janela = agora; reinicios = 0; }
    if (++reinicios <= 3) inicia(); /* retry 3 times per minute, then stay red */
}

/* ------------------------------------------------------------ health */
static void checa(void) {
    if (!filho) return;
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) return;
    DWORD tempo = 1500;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&tempo, sizeof tempo);
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, (const char *)&tempo, sizeof tempo);
    struct sockaddr_in a = {0};
    a.sin_family = AF_INET; a.sin_port = htons((u_short)PORTA); a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    char buf[2048]; int n = 0, ok = 0, ntar = 0;
    if (connect(s, (struct sockaddr *)&a, sizeof a) == 0) {
        const char *req = "GET /saude HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n";
        if (send(s, req, (int)strlen(req), 0) > 0) {
            int r;
            while (n < (int)sizeof buf - 1 && (r = recv(s, buf + n, sizeof buf - 1 - n, 0)) > 0) n += r;
            buf[n] = 0;
            char *corpo = strstr(buf, "\r\n\r\n");
            if (strstr(buf, " 200 ") && corpo && sscanf(corpo + 4, "ok %d", &ntar) == 1) ok = 1;
        }
    }
    closesocket(s);
    if (ok) {
        muda(OK, ntar);
        if (abrir_ao_subir) { abrir_ao_subir = 0; abre(L""); }
    } else if (estado == OK) muda(INICIANDO, 0);
}

/* ------------------------------------------------------------ start on login */
static int no_login(void) {
    HKEY k;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, RUN_KEY, 0, KEY_READ, &k) != ERROR_SUCCESS) return 0;
    int tem = RegQueryValueExW(k, RUN_VALOR, NULL, NULL, NULL, NULL) == ERROR_SUCCESS;
    RegCloseKey(k);
    return tem;
}

static void alterna_login(void) {
    HKEY k;
    if (RegCreateKeyExW(HKEY_CURRENT_USER, RUN_KEY, 0, NULL, 0, KEY_WRITE, NULL, &k, NULL) != ERROR_SUCCESS) return;
    if (no_login()) RegDeleteValueW(k, RUN_VALOR);
    else {
        wchar_t v[MAX_PATH + 3];
        _snwprintf(v, MAX_PATH + 3, L"\"%ls\"", exe);
        v[MAX_PATH + 2] = 0;
        RegSetValueExW(k, RUN_VALOR, 0, REG_SZ, (const BYTE *)v, (DWORD)((wcslen(v) + 1) * sizeof(wchar_t)));
    }
    RegCloseKey(k);
}

/* ------------------------------------------------------------ window, icon and menu */
static void adiciona_icone(void) {
    Shell_NotifyIconW(NIM_ADD, &nid);
    nid.uVersion = NOTIFYICON_VERSION_4;
    Shell_NotifyIconW(NIM_SETVERSION, &nid);
}

static void sair(void) {
    saindo = 1;
    KillTimer(jan, 1);
    mata_servico();
    Shell_NotifyIconW(NIM_DELETE, &nid);
    PostQuitMessage(0);
}

static void menu(void) {
    HMENU m = CreatePopupMenu();
    wchar_t st[128];
    texto_estado(st, 128);
    AppendMenuW(m, MF_STRING | MF_GRAYED, 0, st);
    AppendMenuW(m, MF_SEPARATOR, 0, NULL);
    AppendMenuW(m, MF_STRING, M_ABRIR, L"Open Agents");
    AppendMenuW(m, MF_STRING | (estado == OK ? 0 : MF_GRAYED), M_CEL, L"Pair phone (QR code)");
    AppendMenuW(m, MF_STRING, M_REINICIA, L"Restart service");
    AppendMenuW(m, MF_SEPARATOR, 0, NULL);
    AppendMenuW(m, MF_STRING | (no_login() ? MF_CHECKED : 0), M_AUTO, L"Start on login");
    AppendMenuW(m, MF_SEPARATOR, 0, NULL);
    AppendMenuW(m, MF_STRING, M_SAIR, L"Quit (stops service and agents)");
    SetMenuDefaultItem(m, M_ABRIR, FALSE);
    POINT pt;
    GetCursorPos(&pt);
    SetForegroundWindow(jan);
    int id = TrackPopupMenu(m, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY, pt.x, pt.y, 0, jan, NULL);
    PostMessageW(jan, WM_NULL, 0, 0);
    DestroyMenu(m);
    switch (id) {
    case M_ABRIR: abre(L""); break;
    case M_CEL: abre(L"#celular"); break;
    case M_REINICIA: reinicia(); break;
    case M_AUTO: alterna_login(); break;
    case M_SAIR: sair(); break;
    }
}

static LRESULT CALLBACK proc(HWND h, UINT m, WPARAM w, LPARAM l) {
    if (m == msg_barra && msg_barra) { adiciona_icone(); return 0; } /* Explorer restarted */
    switch (m) {
    case WM_BANDEJA:
        switch (LOWORD(l)) {
        case NIN_SELECT: case NIN_KEYSELECT: case WM_LBUTTONUP: abre(L""); break;
        case WM_CONTEXTMENU: case WM_RBUTTONUP: menu(); break;
        }
        return 0;
    case WM_TIMER:
        vigia_filho();
        checa();
        return 0;
    case WM_CLOSE: case WM_ENDSESSION:
        sair();
        return 0;
    }
    return DefWindowProcW(h, m, w, l);
}

static int para_outro(void) {
    HWND outro = FindWindowW(CLASSE, NULL);
    if (!outro) return 0;
    DWORD pid = 0;
    GetWindowThreadProcessId(outro, &pid);
    HANDLE p = OpenProcess(SYNCHRONIZE, FALSE, pid);
    PostMessageW(outro, WM_CLOSE, 0, 0);
    if (p) { WaitForSingleObject(p, 15000); CloseHandle(p); }
    return 1;
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE ant, PWSTR linha, int mostra) {
    (void)ant; (void)mostra;
    int argc = 0;
    wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    int sair_outro = 0;
    for (int i = 1; i < argc; i++) {
        if (!wcscmp(argv[i], L"--open")) abrir_ao_subir = 1;
        else if (!wcscmp(argv[i], L"--quit")) sair_outro = 1;
        else if (!wcscmp(argv[i], L"--version")) {
            if (AttachConsole(ATTACH_PARENT_PROCESS)) { DWORD e; WriteConsoleA(GetStdHandle(STD_OUTPUT_HANDLE), "agents-web-tray 2\n", 18, &e, NULL); }
            return 0;
        }
    }
    (void)linha;
    wchar_t pe[16];
    if (GetEnvironmentVariableW(L"AGENTS_WEB_PORT", pe, 16)) {
        int v = _wtoi(pe);
        if (v > 0 && v < 65536) PORTA = v;
    }

    HANDLE mutex = CreateMutexW(NULL, TRUE, L"Local\\AgentsWebTray");
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        if (sair_outro) { para_outro(); return 0; }
        abre(L"");
        return 0;
    }
    if (sair_outro) return 0;

    GetModuleFileNameW(NULL, exe, MAX_PATH);
    wcscpy(base, exe);
    wchar_t *barra = wcsrchr(base, L'\\');
    if (barra) *barra = 0;
    _snwprintf(pythonw, MAX_PATH, L"%ls\\python\\pythonw.exe", base);
    _snwprintf(script, MAX_PATH, L"%ls\\app\\server.py", base);
    wchar_t local[MAX_PATH];
    if (!GetEnvironmentVariableW(L"LOCALAPPDATA", local, MAX_PATH)) wcscpy(local, base);
    _snwprintf(dados, MAX_PATH, L"%ls\\agents-web", local);
    CreateDirectoryW(dados, NULL);
    _snwprintf(arqlog, MAX_PATH, L"%ls\\server.log", dados);

    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
    job = CreateJobObjectW(NULL, NULL);
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION lim = {0};
    /* the updater leaves the job (CREATE_BREAKAWAY_FROM_JOB) so it can stop this tray and start the new one */
    lim.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK;
    SetInformationJobObject(job, JobObjectExtendedLimitInformation, &lim, sizeof lim);

    WNDCLASSW wc = {0};
    wc.lpfnWndProc = proc; wc.hInstance = inst; wc.lpszClassName = CLASSE;
    RegisterClassW(&wc);
    jan = CreateWindowW(CLASSE, L"Agents Web", 0, 0, 0, 0, 0, NULL, NULL, inst, NULL);
    msg_barra = RegisterWindowMessageW(L"TaskbarCreated");

    nid.cbSize = sizeof nid;
    nid.hWnd = jan;
    nid.uID = 1;
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP;
    nid.uCallbackMessage = WM_BANDEJA;
    estado = INICIANDO;
    icone = desenha();
    nid.hIcon = icone;
    texto_estado(nid.szTip, sizeof nid.szTip / sizeof nid.szTip[0]);
    adiciona_icone();

    SetEnvironmentVariableW(L"AGENTS_WEB_TRAY", L"2"); /* the server hands updates to the tray (see roda_instalador) */
    inicia();
    SetTimer(jan, 1, 2000, NULL);
    MSG msg;
    while (GetMessageW(&msg, NULL, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    if (!saindo) { mata_servico(); Shell_NotifyIconW(NIM_DELETE, &nid); }
    CloseHandle(job);
    ReleaseMutex(mutex);
    LocalFree(argv);
    return 0;
}
