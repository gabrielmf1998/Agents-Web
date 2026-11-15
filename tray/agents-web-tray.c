/* agents-web-tray: tray icon for Agents Web (StatusNotifierItem + dbusmenu over sd-bus).
 * Starts the server as a child process. Quitting the tray stops the service and any running agent.
 * Build: gcc -O2 -Wall -o agents-web-tray agents-web-tray.c -lsystemd -lm
 * Options: --open opens the page once the service answers. A second launch only opens the page.
 * https://github.com/gabrielmf1998/Agents-Web (MIT license) */
#define _GNU_SOURCE
#include <systemd/sd-bus.h>
#include <systemd/sd-event.h>
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <libgen.h>
#include <math.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static int PORTA = 47830;
#define NOME "io.github.gabrielmf1998.AgentsWeb"
#define WATCHER "org.kde.StatusNotifierWatcher"

enum { INICIANDO, OK, PARADO };
static sd_bus *bus;
static sd_event *ev;
static sd_event_source *src_filho;
static pid_t filho;
static int estado = INICIANDO, tarefas, revisao = 1, saindo, reiniciando;
static int reinicios, abrir_ao_subir;
static time_t janela;
static char base[1024], script[1100], autostart[1100], arqlog[1200], exe[1024], motivo[160], token_xdg[256];
static const char *home;

/* ------------------------------------------------------------ icon */
#define T1 32
#define T2 64
static unsigned char pix1[T1 * T1 * 4], pix2[T2 * T2 * 4];

static void desenha(unsigned char *p, int n) {
    unsigned r, g, b;
    switch (estado) {
    case OK: if (tarefas) { r = 255; g = 255; b = 255; } else { r = 0x6f; g = 0xdb; b = 0x7f; } break;
    case INICIANDO: r = 0xe8; g = 0xc6; b = 0x6a; break;
    default: r = 0xff; g = 0x55; b = 0x55;
    }
    double c = n / 2.0, R = n * 0.47, anel = n * 0.075, miolo = n * (tarefas && estado == OK ? 0.24 : 0.20);
    for (int y = 0; y < n; y++)
        for (int x = 0; x < n; x++) {
            double d = hypot(x + 0.5 - c, y + 0.5 - c);
            double af = fmin(1, fmax(0, R - d + 0.5));           /* black disc */
            double aa = fmin(1, fmax(0, anel / 2 - fabs(d - (R - anel / 2)) + 0.5)); /* white ring */
            double am = fmin(1, fmax(0, miolo - d + 0.5));       /* state dot */
            double cr = 10, cg = 10, cb = 10;
            cr = cr * (1 - aa) + 235 * aa; cg = cg * (1 - aa) + 235 * aa; cb = cb * (1 - aa) + 235 * aa;
            cr = cr * (1 - am) + r * am; cg = cg * (1 - am) + g * am; cb = cb * (1 - am) + b * am;
            unsigned char *q = p + (y * n + x) * 4; /* ARGB32 in network byte order */
            q[0] = (unsigned char)(af * 255); q[1] = (unsigned char)cr; q[2] = (unsigned char)cg; q[3] = (unsigned char)cb;
        }
}

static const char *texto_estado(char *buf, size_t n) {
    if (estado == OK && tarefas)
        snprintf(buf, n, "Service running (pid %d), %d active task%s", filho, tarefas, tarefas == 1 ? "" : "s");
    else if (estado == OK)
        snprintf(buf, n, "Service running (pid %d)", filho);
    else if (estado == INICIANDO)
        snprintf(buf, n, "Service starting or not responding (pid %d)", filho);
    else
        snprintf(buf, n, "Service STOPPED%s%s", motivo[0] ? ": " : "", motivo);
    return buf;
}

static void sinal_sni(const char *nome) {
    sd_bus_emit_signal(bus, "/StatusNotifierItem", "org.kde.StatusNotifierItem", nome, NULL);
}

static void muda(int novo, int ntar) {
    if (novo == estado && ntar == tarefas) return;
    estado = novo; tarefas = ntar;
    desenha(pix1, T1); desenha(pix2, T2);
    sinal_sni("NewIcon"); sinal_sni("NewToolTip"); sinal_sni("NewStatus");
    revisao++;
    sd_bus_emit_signal(bus, "/MenuBar", "com.canonical.dbusmenu", "LayoutUpdated", "ui", revisao, 0);
}

/* ------------------------------------------------------------ processes */
static void solta(char *const argv[]) { /* double fork: no zombie, no tie to the tray */
    pid_t p = fork();
    if (p == 0) {
        setsid();
        sigset_t s; sigemptyset(&s); sigprocmask(SIG_SETMASK, &s, NULL);
        if (fork() == 0) {
            if (token_xdg[0]) setenv("XDG_ACTIVATION_TOKEN", token_xdg, 1);
            int fd = open("/dev/null", O_RDWR); dup2(fd, 0); dup2(fd, 1); dup2(fd, 2);
            execvp(argv[0], argv); _exit(127);
        }
        _exit(0);
    }
    if (p > 0) waitpid(p, NULL, 0);
    token_xdg[0] = 0;
}

static void abre(const char *sufixo) {
    char url[128];
    snprintf(url, sizeof url, "http://127.0.0.1:%d/%s", PORTA, sufixo);
    char *argv[] = {"xdg-open", url, NULL};
    solta(argv);
}

static int fim_filho(sd_event_source *s, const siginfo_t *si, void *u);

static void inicia(void) {
    pid_t pai = getpid();
    pid_t p = fork();
    if (p < 0) { snprintf(motivo, sizeof motivo, "fork failed"); muda(PARADO, 0); return; }
    if (p == 0) {
        setpgid(0, 0);
        prctl(PR_SET_PDEATHSIG, SIGTERM); /* tray died in any way: the service dies with it */
        if (getppid() != pai) _exit(1);
        sigset_t s; sigemptyset(&s); sigprocmask(SIG_SETMASK, &s, NULL);
        if (chdir(home)) {}
        int fd = open("/dev/null", O_RDONLY); dup2(fd, 0);
        int lf = open(arqlog, O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (lf >= 0) { dup2(lf, 1); dup2(lf, 2); }
        execl("/usr/bin/python3", "python3", "-u", script, (char *)NULL);
        _exit(127);
    }
    setpgid(p, p);
    filho = p;
    motivo[0] = 0;
    estado = -1; muda(INICIANDO, 0);
    sd_event_source_unref(src_filho);
    sd_event_add_child(ev, &src_filho, p, WEXITED, fim_filho, NULL);
}

static void mata_servico(void) {
    if (!filho) return;
    kill(-filho, SIGTERM);
    for (int i = 0; i < 100; i++) { /* up to 5 s */
        if (waitpid(filho, NULL, WNOHANG) == filho) { filho = 0; break; }
        usleep(50000);
    }
    if (filho) { kill(-filho, SIGKILL); waitpid(filho, NULL, 0); filho = 0; }
    sd_event_source_unref(src_filho); src_filho = NULL;
}

static int fim_filho(sd_event_source *s, const siginfo_t *si, void *u) {
    (void)s; (void)u;
    if (si->si_pid != filho) return 0;
    filho = 0;
    if (si->si_code == CLD_EXITED) snprintf(motivo, sizeof motivo, "exited with code %d (see server.log)", si->si_status);
    else snprintf(motivo, sizeof motivo, "killed by signal %d", si->si_status);
    muda(PARADO, 0);
    if (saindo || reiniciando) return 0;
    time_t agora = time(NULL);
    if (agora - janela > 60) { janela = agora; reinicios = 0; }
    if (++reinicios <= 3) inicia(); /* retry 3 times per minute, then stay red */
    return 0;
}

static void reinicia(void) {
    reiniciando = 1; mata_servico(); reiniciando = 0;
    reinicios = 0; inicia();
}

/* ------------------------------------------------------------ health */
static void checa(void) {
    if (!filho) return;
    int s = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (s < 0) return;
    struct timeval tv = {1, 500000};
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    struct sockaddr_in a = {.sin_family = AF_INET, .sin_port = htons(PORTA)};
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    char buf[2048]; int n = 0, ok = 0, ntar = 0;
    if (connect(s, (void *)&a, sizeof a) == 0) {
        const char *req = "GET /saude HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n";
        if (write(s, req, strlen(req)) > 0) {
            int r;
            while (n < (int)sizeof buf - 1 && (r = read(s, buf + n, sizeof buf - 1 - n)) > 0) n += r;
            buf[n] = 0;
            char *corpo = strstr(buf, "\r\n\r\n");
            if (strstr(buf, " 200 ") && corpo && sscanf(corpo + 4, "ok %d", &ntar) == 1) ok = 1;
        }
    }
    close(s);
    if (ok) {
        muda(OK, ntar);
        if (abrir_ao_subir) { abrir_ao_subir = 0; abre(""); }
    }
    else if (estado == OK) muda(INICIANDO, 0);
}

static int tique(sd_event_source *s, uint64_t t, void *u) {
    (void)t; (void)u;
    checa();
    sd_event_source_set_time_relative(s, 2000000);
    sd_event_source_set_enabled(s, SD_EVENT_ONESHOT);
    return 0;
}

/* ------------------------------------------------------------ autostart */
static int no_autostart(void) { struct stat st; return stat(autostart, &st) == 0; }

static void alterna_autostart(void) {
    if (no_autostart()) { unlink(autostart); return; }
    char d[1100]; snprintf(d, sizeof d, "%s/.config/autostart", home); mkdir(d, 0755);
    FILE *f = fopen(autostart, "w");
    if (!f) return;
    fprintf(f, "[Desktop Entry]\nType=Application\nName=Agents\nComment=Tray icon and service for Agents\n"
               "Exec=%s\nIcon=agents-web\nTerminal=false\nX-KDE-autostart-phase=2\n", exe);
    fclose(f);
}

/* ------------------------------------------------------------ StatusNotifierItem */
static int p_s(sd_bus *b, const char *path, const char *itf, const char *prop, sd_bus_message *r, void *u, sd_bus_error *e) {
    (void)b; (void)path; (void)itf; (void)u; (void)e;
    if (!strcmp(prop, "Category")) return sd_bus_message_append(r, "s", "ApplicationStatus");
    if (!strcmp(prop, "Id")) return sd_bus_message_append(r, "s", "agents-web");
    if (!strcmp(prop, "Title")) return sd_bus_message_append(r, "s", "Agents");
    if (!strcmp(prop, "Status")) return sd_bus_message_append(r, "s", estado == PARADO ? "NeedsAttention" : "Active");
    return sd_bus_message_append(r, "s", "");
}
static int p_i(sd_bus *b, const char *path, const char *itf, const char *prop, sd_bus_message *r, void *u, sd_bus_error *e) {
    (void)b; (void)path; (void)itf; (void)prop; (void)u; (void)e;
    return sd_bus_message_append(r, "i", 0);
}
static int p_b(sd_bus *b, const char *path, const char *itf, const char *prop, sd_bus_message *r, void *u, sd_bus_error *e) {
    (void)b; (void)path; (void)itf; (void)prop; (void)u; (void)e;
    return sd_bus_message_append(r, "b", 0);
}
static int p_menu(sd_bus *b, const char *path, const char *itf, const char *prop, sd_bus_message *r, void *u, sd_bus_error *e) {
    (void)b; (void)path; (void)itf; (void)prop; (void)u; (void)e;
    return sd_bus_message_append(r, "o", "/MenuBar");
}
static int poe_pix(sd_bus_message *r) {
    sd_bus_message_open_container(r, 'a', "(iiay)");
    sd_bus_message_open_container(r, 'r', "iiay");
    sd_bus_message_append(r, "ii", T1, T1);
    sd_bus_message_append_array(r, 'y', pix1, sizeof pix1);
    sd_bus_message_close_container(r);
    sd_bus_message_open_container(r, 'r', "iiay");
    sd_bus_message_append(r, "ii", T2, T2);
    sd_bus_message_append_array(r, 'y', pix2, sizeof pix2);
    sd_bus_message_close_container(r);
    return sd_bus_message_close_container(r);
}
static int p_pix(sd_bus *b, const char *path, const char *itf, const char *prop, sd_bus_message *r, void *u, sd_bus_error *e) {
    (void)b; (void)path; (void)itf; (void)u; (void)e;
    if (!strcmp(prop, "IconPixmap")) return poe_pix(r);
    return sd_bus_message_append(r, "a(iiay)", 0);
}
static int p_tip(sd_bus *b, const char *path, const char *itf, const char *prop, sd_bus_message *r, void *u, sd_bus_error *e) {
    (void)b; (void)path; (void)itf; (void)prop; (void)u; (void)e;
    char t[256];
    sd_bus_message_open_container(r, 'r', "sa(iiay)ss");
    sd_bus_message_append(r, "s", "");
    sd_bus_message_append(r, "a(iiay)", 0);
    sd_bus_message_append(r, "ss", "Agents", texto_estado(t, sizeof t));
    return sd_bus_message_close_container(r);
}
static int m_activate(sd_bus_message *m, void *u, sd_bus_error *e) {
    (void)u; (void)e;
    abre("");
    return sd_bus_reply_method_return(m, NULL);
}
static int m_nada(sd_bus_message *m, void *u, sd_bus_error *e) { (void)u; (void)e; return sd_bus_reply_method_return(m, NULL); }
static int m_token(sd_bus_message *m, void *u, sd_bus_error *e) {
    (void)u; (void)e;
    const char *t = "";
    sd_bus_message_read(m, "s", &t);
    snprintf(token_xdg, sizeof token_xdg, "%s", t);
    return sd_bus_reply_method_return(m, NULL);
}

static const sd_bus_vtable vt_sni[] = {
    SD_BUS_VTABLE_START(0),
    SD_BUS_PROPERTY("Category", "s", p_s, 0, 0),
    SD_BUS_PROPERTY("Id", "s", p_s, 0, 0),
    SD_BUS_PROPERTY("Title", "s", p_s, 0, 0),
    SD_BUS_PROPERTY("Status", "s", p_s, 0, 0),
    SD_BUS_PROPERTY("WindowId", "i", p_i, 0, 0),
    SD_BUS_PROPERTY("IconName", "s", p_s, 0, 0),
    SD_BUS_PROPERTY("IconThemePath", "s", p_s, 0, 0),
    SD_BUS_PROPERTY("IconPixmap", "a(iiay)", p_pix, 0, 0),
    SD_BUS_PROPERTY("OverlayIconName", "s", p_s, 0, 0),
    SD_BUS_PROPERTY("OverlayIconPixmap", "a(iiay)", p_pix, 0, 0),
    SD_BUS_PROPERTY("AttentionIconName", "s", p_s, 0, 0),
    SD_BUS_PROPERTY("AttentionIconPixmap", "a(iiay)", p_pix, 0, 0),
    SD_BUS_PROPERTY("AttentionMovieName", "s", p_s, 0, 0),
    SD_BUS_PROPERTY("ToolTip", "(sa(iiay)ss)", p_tip, 0, 0),
    SD_BUS_PROPERTY("ItemIsMenu", "b", p_b, 0, 0),
    SD_BUS_PROPERTY("Menu", "o", p_menu, 0, 0),
    SD_BUS_METHOD("Activate", "ii", "", m_activate, 0),
    SD_BUS_METHOD("SecondaryActivate", "ii", "", m_activate, 0),
    SD_BUS_METHOD("ContextMenu", "ii", "", m_nada, 0),
    SD_BUS_METHOD("Scroll", "is", "", m_nada, 0),
    SD_BUS_METHOD("ProvideXdgActivationToken", "s", "", m_token, 0),
    SD_BUS_SIGNAL("NewTitle", "", 0),
    SD_BUS_SIGNAL("NewIcon", "", 0),
    SD_BUS_SIGNAL("NewAttentionIcon", "", 0),
    SD_BUS_SIGNAL("NewOverlayIcon", "", 0),
    SD_BUS_SIGNAL("NewToolTip", "", 0),
    SD_BUS_SIGNAL("NewStatus", "s", 0),
    SD_BUS_VTABLE_END};

/* ------------------------------------------------------------ dbusmenu */
enum { M_RAIZ, M_ABRIR, M_ESTADO, M_SEP1, M_CEL, M_REINICIA, M_AUTO, M_SEP2, M_SAIR, M_N };

static void kv_s(sd_bus_message *m, const char *k, const char *v) {
    sd_bus_message_open_container(m, 'e', "sv"); sd_bus_message_append(m, "s", k);
    sd_bus_message_append(m, "v", "s", v); sd_bus_message_close_container(m);
}
static void kv_b(sd_bus_message *m, const char *k, int v) {
    sd_bus_message_open_container(m, 'e', "sv"); sd_bus_message_append(m, "s", k);
    sd_bus_message_append(m, "v", "b", v); sd_bus_message_close_container(m);
}
static void kv_i(sd_bus_message *m, const char *k, int v) {
    sd_bus_message_open_container(m, 'e', "sv"); sd_bus_message_append(m, "s", k);
    sd_bus_message_append(m, "v", "i", v); sd_bus_message_close_container(m);
}

static void props(sd_bus_message *m, int id) {
    char t[256];
    sd_bus_message_open_container(m, 'a', "{sv}");
    switch (id) {
    case M_RAIZ: kv_s(m, "children-display", "submenu"); break;
    case M_ABRIR: kv_s(m, "label", "Open Agents"); kv_s(m, "icon-name", "internet-web-browser"); break;
    case M_ESTADO: kv_s(m, "label", texto_estado(t, sizeof t)); kv_b(m, "enabled", 0); break;
    case M_SEP1: case M_SEP2: kv_s(m, "type", "separator"); break;
    case M_CEL: kv_s(m, "label", "Pair phone (QR code)"); kv_s(m, "icon-name", "smartphone"); kv_b(m, "enabled", estado == OK); break;
    case M_REINICIA: kv_s(m, "label", filho ? "Restart service" : "Start service"); kv_s(m, "icon-name", "view-refresh"); break;
    case M_AUTO: kv_s(m, "label", "Start on login"); kv_s(m, "toggle-type", "checkmark"); kv_i(m, "toggle-state", no_autostart()); break;
    case M_SAIR: kv_s(m, "label", "Quit (stops service and agents)"); kv_s(m, "icon-name", "application-exit"); break;
    }
    sd_bus_message_close_container(m);
}

static void item(sd_bus_message *m, int id, int prof) {
    sd_bus_message_open_container(m, 'r', "ia{sv}av");
    sd_bus_message_append(m, "i", id);
    props(m, id);
    sd_bus_message_open_container(m, 'a', "v");
    if (id == M_RAIZ && prof != 0)
        for (int i = 1; i < M_N; i++) {
            sd_bus_message_open_container(m, 'v', "(ia{sv}av)");
            item(m, i, prof - 1);
            sd_bus_message_close_container(m);
        }
    sd_bus_message_close_container(m);
    sd_bus_message_close_container(m);
}

static int m_layout(sd_bus_message *m, void *u, sd_bus_error *e) {
    (void)u; (void)e;
    int pai = 0, prof = -1;
    sd_bus_message_read(m, "ii", &pai, &prof);
    sd_bus_message *r;
    sd_bus_message_new_method_return(m, &r);
    sd_bus_message_append(r, "u", revisao);
    item(r, (pai >= 0 && pai < M_N) ? pai : 0, prof);
    int k = sd_bus_send(NULL, r, NULL);
    sd_bus_message_unref(r);
    return k;
}

static int m_grupo(sd_bus_message *m, void *u, sd_bus_error *e) {
    (void)u; (void)e;
    const int32_t *ids = NULL; size_t tam = 0;
    sd_bus_message_read_array(m, 'i', (const void **)&ids, &tam);
    sd_bus_message *r;
    sd_bus_message_new_method_return(m, &r);
    sd_bus_message_open_container(r, 'a', "(ia{sv})");
    for (size_t i = 0; i < tam / 4; i++) {
        if (ids[i] < 0 || ids[i] >= M_N) continue;
        sd_bus_message_open_container(r, 'r', "ia{sv}");
        sd_bus_message_append(r, "i", ids[i]);
        props(r, ids[i]);
        sd_bus_message_close_container(r);
    }
    sd_bus_message_close_container(r);
    int k = sd_bus_send(NULL, r, NULL);
    sd_bus_message_unref(r);
    return k;
}

static void sair(void);

static void acao(int id) {
    switch (id) {
    case M_ABRIR: abre(""); break;
    case M_CEL: abre("#celular"); break;
    case M_REINICIA: reinicia(); break;
    case M_AUTO: alterna_autostart(); revisao++;
        sd_bus_emit_signal(bus, "/MenuBar", "com.canonical.dbusmenu", "LayoutUpdated", "ui", revisao, 0); break;
    case M_SAIR: sair(); break;
    }
}

static int m_evento(sd_bus_message *m, void *u, sd_bus_error *e) {
    (void)u; (void)e;
    int id = 0; const char *tipo = "";
    sd_bus_message_read(m, "is", &id, &tipo);
    int k = sd_bus_reply_method_return(m, NULL);
    if (!strcmp(tipo, "clicked")) acao(id);
    return k;
}

static int m_eventos(sd_bus_message *m, void *u, sd_bus_error *e) {
    (void)u; (void)e;
    int lista[16], n = 0;
    if (sd_bus_message_enter_container(m, 'a', "(isvu)") > 0) {
        while (sd_bus_message_enter_container(m, 'r', "isvu") > 0) {
            int id = 0; const char *tipo = "";
            sd_bus_message_read(m, "is", &id, &tipo);
            sd_bus_message_skip(m, "vu");
            sd_bus_message_exit_container(m);
            if (!strcmp(tipo, "clicked") && n < 16) lista[n++] = id;
        }
        sd_bus_message_exit_container(m);
    }
    int k = sd_bus_reply_method_return(m, "ai", 0);
    for (int i = 0; i < n; i++) acao(lista[i]);
    return k;
}

static int m_antes(sd_bus_message *m, void *u, sd_bus_error *e) { (void)u; (void)e; return sd_bus_reply_method_return(m, "b", 0); }
static int m_antes_g(sd_bus_message *m, void *u, sd_bus_error *e) { (void)u; (void)e; return sd_bus_reply_method_return(m, "aiai", 0, 0); }
static int m_prop(sd_bus_message *m, void *u, sd_bus_error *e) { (void)u; (void)e; return sd_bus_reply_method_return(m, "v", "s", ""); }
static int mp_ver(sd_bus *b, const char *path, const char *itf, const char *prop, sd_bus_message *r, void *u, sd_bus_error *e) {
    (void)b; (void)path; (void)itf; (void)u; (void)e;
    if (!strcmp(prop, "Version")) return sd_bus_message_append(r, "u", 3);
    if (!strcmp(prop, "TextDirection")) return sd_bus_message_append(r, "s", "ltr");
    if (!strcmp(prop, "Status")) return sd_bus_message_append(r, "s", "normal");
    return sd_bus_message_append(r, "as", 0);
}

static const sd_bus_vtable vt_menu[] = {
    SD_BUS_VTABLE_START(0),
    SD_BUS_PROPERTY("Version", "u", mp_ver, 0, 0),
    SD_BUS_PROPERTY("TextDirection", "s", mp_ver, 0, 0),
    SD_BUS_PROPERTY("Status", "s", mp_ver, 0, 0),
    SD_BUS_PROPERTY("IconThemePath", "as", mp_ver, 0, 0),
    SD_BUS_METHOD("GetLayout", "iias", "u(ia{sv}av)", m_layout, 0),
    SD_BUS_METHOD("GetGroupProperties", "aias", "a(ia{sv})", m_grupo, 0),
    SD_BUS_METHOD("GetProperty", "is", "v", m_prop, 0),
    SD_BUS_METHOD("Event", "isvu", "", m_evento, 0),
    SD_BUS_METHOD("EventGroup", "a(isvu)", "ai", m_eventos, 0),
    SD_BUS_METHOD("AboutToShow", "i", "b", m_antes, 0),
    SD_BUS_METHOD("AboutToShowGroup", "ai", "aiai", m_antes_g, 0),
    SD_BUS_SIGNAL("ItemsPropertiesUpdated", "a(ia{sv})a(ias)", 0),
    SD_BUS_SIGNAL("LayoutUpdated", "ui", 0),
    SD_BUS_SIGNAL("ItemActivationRequested", "iu", 0),
    SD_BUS_VTABLE_END};

/* ------------------------------------------------------------ registration */
static void registra(void) {
    sd_bus_call_method_async(bus, NULL, WATCHER, "/StatusNotifierWatcher", WATCHER,
                             "RegisterStatusNotifierItem", NULL, NULL, "s", NOME);
}
static int dono_mudou(sd_bus_message *m, void *u, sd_bus_error *e) {
    (void)u; (void)e;
    const char *n, *velho, *novo;
    if (sd_bus_message_read(m, "sss", &n, &velho, &novo) >= 0 && !strcmp(n, WATCHER) && novo[0]) registra();
    return 0;
}

static void sair(void) {
    saindo = 1;
    mata_servico();
    sd_bus_flush(bus);
    exit(0);
}
static int sinal(sd_event_source *s, const struct signalfd_siginfo *si, void *u) { (void)s; (void)si; (void)u; sair(); return 0; }

int main(int argc, char **argv) {
    home = getenv("HOME");
    if (!home) return 1;
    for (int i = 1; i < argc; i++) if (!strcmp(argv[i], "--open")) abrir_ao_subir = 1;
    const char *pe = getenv("AGENTS_WEB_PORT");
    if (pe && atoi(pe) > 0 && atoi(pe) < 65536) PORTA = atoi(pe);
    ssize_t n = readlink("/proc/self/exe", exe, sizeof exe - 1);
    if (n <= 0) return 1;
    exe[n] = 0;
    snprintf(base, sizeof base, "%s", exe);
    dirname(base);
    snprintf(script, sizeof script, "%s/server.py", base);
    snprintf(autostart, sizeof autostart, "%s/.config/autostart/agents-web.desktop", home);
    char d[1100]; snprintf(d, sizeof d, "%s/.local/share/agents-web", home); mkdir(d, 0700);
    snprintf(arqlog, sizeof arqlog, "%s/server.log", d);

    if (sd_bus_open_user(&bus) < 0) { fprintf(stderr, "no D-Bus session\n"); return 1; }
    int r = sd_bus_request_name(bus, NOME, 0);
    if (r < 0) {
        fprintf(stderr, "agents-web-tray is already running, opening the page\n");
        abre("");
        return 0;
    }
    sigset_t ss; sigemptyset(&ss);
    sigaddset(&ss, SIGCHLD); sigaddset(&ss, SIGTERM); sigaddset(&ss, SIGINT); sigaddset(&ss, SIGHUP);
    sigprocmask(SIG_BLOCK, &ss, NULL);
    signal(SIGPIPE, SIG_IGN);

    sd_event_default(&ev);
    sd_event_add_signal(ev, NULL, SIGTERM, sinal, NULL);
    sd_event_add_signal(ev, NULL, SIGINT, sinal, NULL);
    sd_event_add_signal(ev, NULL, SIGHUP, sinal, NULL);
    sd_bus_add_object_vtable(bus, NULL, "/StatusNotifierItem", "org.kde.StatusNotifierItem", vt_sni, NULL);
    sd_bus_add_object_vtable(bus, NULL, "/MenuBar", "com.canonical.dbusmenu", vt_menu, NULL);
    sd_bus_match_signal(bus, NULL, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                        "NameOwnerChanged", dono_mudou, NULL);
    sd_bus_attach_event(bus, ev, 0);
    sd_bus_set_exit_on_disconnect(bus, 1);

    estado = -1;
    inicia();
    registra();
    sd_event_source *t;
    sd_event_add_time_relative(ev, &t, CLOCK_MONOTONIC, 700000, 0, tique, NULL);
    sd_event_source_set_enabled(t, SD_EVENT_ONESHOT);

    sd_event_loop(ev); /* D-Bus session gone (logout): same path as Quit */
    sair();
    return 0;
}
