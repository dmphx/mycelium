/*
 * Mycelium Spore - Plex Interceptor
 *
 * LD_PRELOAD shared library that intercepts Plex file I/O for Mycelium stub
 * .mkv files and transparently streams real bytes from TorBox CDN via the
 * Mycelium Spore socket server.
 *
 * How it works:
 *   1. On open() of a .mkv file under MYCELIUM_MEDIA_PATH:
 *      - Read sibling .minfo file (token + CDN size)
 *      - Register fd as "virtual"
 *   2. fstat()  -> replace st_size with real CDN size
 *   3. read() / pread() at offset < HEADER_SIZE -> serve from stub file (MKV header)
 *   4. read() / pread() at offset >= HEADER_SIZE -> TCP request to Spore server
 *   5. mmap() on virtual fd -> ENODEV (forces Plex to fall back to read())
 *   6. close() -> deregister virtual fd
 *
 * Build:
 *   gcc -shared -fPIC -O2 -D_GNU_SOURCE -o mycelium_spore.so spore.c -ldl -pthread
 *
 * Inject into Plex:
 *   LD_PRELOAD=/spore/mycelium_spore.so
 *
 * Environment variables:
 *   MYCELIUM_SPORE_HOST  - Mycelium host  (default: mycelium)
 *   MYCELIUM_SPORE_PORT  - Spore TCP port (default: 8089)
 *   MYCELIUM_MEDIA_PATH  - Media root     (default: /data/media)
 */

/* _GNU_SOURCE is passed via -D flag in Makefile */
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <netdb.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

/* ── Configuration ─────────────────────────────────────────────────────────── */
#define MKV_HEADER_SIZE  8192   /* bytes below this offset: served from stub */
#define MAX_FD           4096   /* indexed directly by fd number             */

/* ── Real glibc function pointers ──────────────────────────────────────────── */
static int     (*real_open)  (const char *, int, ...)           = NULL;
static int     (*real_openat)(int, const char *, int, ...)      = NULL;
static ssize_t (*real_read)  (int, void *, size_t)              = NULL;
static ssize_t (*real_pread) (int, void *, size_t, off_t)       = NULL;
static int     (*real_fstat) (int, struct stat *)               = NULL;
static off_t   (*real_lseek) (int, off_t, int)                  = NULL;
static int     (*real_close) (int)                              = NULL;
static void *  (*real_mmap)  (void *, size_t, int, int, int, off_t) = NULL;

/* ── Virtual fd table ───────────────────────────────────────────────────────── */
typedef struct {
    int    active;
    char   token[33];   /* 32 hex chars + NUL */
    off_t  cdn_size;
    off_t  seek_pos;    /* used by read() to track logical position */
} vfd_t;

static vfd_t            vfd_table[MAX_FD];
static pthread_rwlock_t vfd_lock = PTHREAD_RWLOCK_INITIALIZER;

/* Recursion guard: prevents our hooks from re-intercepting themselves */
static __thread int _in_hook = 0;

/* ── Config helpers ─────────────────────────────────────────────────────────── */
static const char *_spore_host(void) {
    const char *h = getenv("MYCELIUM_SPORE_HOST");
    return h ? h : "mycelium";
}
static const char *_spore_port(void) {
    const char *p = getenv("MYCELIUM_SPORE_PORT");
    return p ? p : "8089";
}
static const char *_media_path(void) {
    const char *p = getenv("MYCELIUM_MEDIA_PATH");
    return p ? p : "/data/media";
}

/* ── Helpers ────────────────────────────────────────────────────────────────── */

/* Returns 1 if path is under media root and ends in .mkv */
static int _is_mkv_candidate(const char *path) {
    if (!path) return 0;
    size_t n = strlen(path);
    if (n < 4) return 0;
    if (path[n-4] != '.' ||
        (path[n-3] | 0x20) != 'm' ||
        (path[n-2] | 0x20) != 'k' ||
        (path[n-1] | 0x20) != 'v') return 0;
    const char *mp = _media_path();
    return strncmp(path, mp, strlen(mp)) == 0;
}

/* Read .minfo sidecar (same dir, same stem, .minfo extension).
   Returns 1 on success; fills token_out (>=33 bytes) and size_out. */
static int _read_minfo(const char *mkv_path, char *token_out, off_t *size_out) {
    size_t n = strlen(mkv_path);
    char minfo_path[PATH_MAX];
    /* Replace last 4 chars (.mkv) with .minfo */
    if (n < 4 || n + 3 >= PATH_MAX) return 0;
    memcpy(minfo_path, mkv_path, n - 4);
    memcpy(minfo_path + n - 4, ".minfo", 7);

    _in_hook = 1;
    int fd = real_open(minfo_path, O_RDONLY);
    _in_hook = 0;
    if (fd < 0) return 0;

    char buf[256] = {0};
    _in_hook = 1;
    ssize_t r = real_read(fd, buf, sizeof(buf) - 1);
    real_close(fd);
    _in_hook = 0;
    if (r <= 0) return 0;

    token_out[0] = '\0';
    *size_out = 0;

    char *line = buf;
    while (line && *line) {
        if (strncmp(line, "token=", 6) == 0) {
            char *val = line + 6;
            char *end = strchr(val, '\n');
            size_t len = end ? (size_t)(end - val) : strlen(val);
            if (len > 0 && len <= 32) {
                memcpy(token_out, val, len);
                token_out[len] = '\0';
            }
        } else if (strncmp(line, "size=", 5) == 0) {
            *size_out = (off_t)atoll(line + 5);
        }
        line = strchr(line, '\n');
        if (line) line++;
    }
    return token_out[0] != '\0';
}

/* Open TCP connection to Spore server. Returns socket fd or -1. */
static int _spore_connect(void) {
    struct addrinfo hints, *res, *rp;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family   = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;

    if (getaddrinfo(_spore_host(), _spore_port(), &hints, &res) != 0)
        return -1;

    int sock = -1;
    for (rp = res; rp; rp = rp->ai_next) {
        sock = socket(rp->ai_family, rp->ai_socktype, rp->ai_protocol);
        if (sock < 0) continue;
        if (connect(sock, rp->ai_addr, rp->ai_addrlen) == 0) break;
        close(sock);
        sock = -1;
    }
    freeaddrinfo(res);
    return sock;
}

/* Send a range request to the Spore server.
   Protocol: "<token> <offset> <count>\n"  ->  "OK <actual>\n<bytes>"
   Returns bytes written to buf, or -1 on error. */
static ssize_t _spore_read(const char *token, off_t offset,
                            void *buf, size_t count) {
    int sock = _spore_connect();
    if (sock < 0) return -1;

    /* Send request */
    char req[128];
    int req_len = snprintf(req, sizeof(req), "%s %lld %zu\n",
                           token, (long long)offset, count);
    if (write(sock, req, req_len) != req_len) {
        close(sock);
        return -1;
    }

    /* Read response header (terminated by \n) */
    char hdr[64] = {0};
    int  hi = 0;
    while (hi < 63) {
        char c;
        if (read(sock, &c, 1) != 1) { close(sock); return -1; }
        hdr[hi++] = c;
        if (c == '\n') break;
    }

    if (strncmp(hdr, "OK ", 3) != 0) {
        close(sock);
        return -1;
    }
    ssize_t actual = (ssize_t)atoll(hdr + 3);
    if (actual <= 0) { close(sock); return actual; }
    if (actual > (ssize_t)count) actual = (ssize_t)count;

    /* Read payload */
    ssize_t received = 0;
    while (received < actual) {
        ssize_t n = read(sock, (char *)buf + received,
                         (size_t)(actual - received));
        if (n <= 0) break;
        received += n;
    }
    close(sock);
    return received;
}

/* ── Library constructor ────────────────────────────────────────────────────── */
__attribute__((constructor))
static void _spore_init(void) {
    real_open   = dlsym(RTLD_NEXT, "open");
    real_openat = dlsym(RTLD_NEXT, "openat");
    real_read   = dlsym(RTLD_NEXT, "read");
    real_pread  = dlsym(RTLD_NEXT, "pread");
    real_fstat  = dlsym(RTLD_NEXT, "fstat");
    real_lseek  = dlsym(RTLD_NEXT, "lseek");
    real_close  = dlsym(RTLD_NEXT, "close");
    real_mmap   = dlsym(RTLD_NEXT, "mmap");
    memset(vfd_table, 0, sizeof(vfd_table));
}

/* ── Intercepted: open() ────────────────────────────────────────────────────── */
int open(const char *path, int flags, ...) {
    va_list ap;
    va_start(ap, flags);
    mode_t mode = (flags & O_CREAT) ? va_arg(ap, mode_t) : 0;
    va_end(ap);

    if (_in_hook || !real_open)
        return real_open ? real_open(path, flags, mode) : -1;

    int fd = real_open(path, flags, mode);
    if (fd < 0 || fd >= MAX_FD) return fd;

    if (_is_mkv_candidate(path)) {
        char token[33] = {0};
        off_t cdn_size = 0;
        _in_hook = 1;
        int ok = _read_minfo(path, token, &cdn_size);
        _in_hook = 0;
        if (ok) {
            pthread_rwlock_wrlock(&vfd_lock);
            vfd_table[fd].active   = 1;
            vfd_table[fd].cdn_size = cdn_size;
            vfd_table[fd].seek_pos = 0;
            strncpy(vfd_table[fd].token, token, 32);
            vfd_table[fd].token[32] = '\0';
            pthread_rwlock_unlock(&vfd_lock);
        }
    }
    return fd;
}

/* ── Intercepted: openat() ──────────────────────────────────────────────────── */
int openat(int dirfd, const char *path, int flags, ...) {
    va_list ap;
    va_start(ap, flags);
    mode_t mode = (flags & O_CREAT) ? va_arg(ap, mode_t) : 0;
    va_end(ap);

    if (!real_openat) return -1;

    int fd = real_openat(dirfd, path, flags, mode);
    if (fd < 0 || fd >= MAX_FD || _in_hook) return fd;

    /* Only handle absolute paths for now */
    if (path && path[0] == '/' && _is_mkv_candidate(path)) {
        char token[33] = {0};
        off_t cdn_size = 0;
        _in_hook = 1;
        int ok = _read_minfo(path, token, &cdn_size);
        _in_hook = 0;
        if (ok) {
            pthread_rwlock_wrlock(&vfd_lock);
            vfd_table[fd].active   = 1;
            vfd_table[fd].cdn_size = cdn_size;
            vfd_table[fd].seek_pos = 0;
            strncpy(vfd_table[fd].token, token, 32);
            vfd_table[fd].token[32] = '\0';
            pthread_rwlock_unlock(&vfd_lock);
        }
    }
    return fd;
}

/* ── Intercepted: read() ────────────────────────────────────────────────────── */
ssize_t read(int fd, void *buf, size_t count) {
    if (_in_hook || !real_read || fd < 0 || fd >= MAX_FD)
        return real_read ? real_read(fd, buf, count) : -1;

    pthread_rwlock_rdlock(&vfd_lock);
    int active = vfd_table[fd].active;
    off_t pos  = vfd_table[fd].seek_pos;
    char token[33];
    strncpy(token, vfd_table[fd].token, 33);
    pthread_rwlock_unlock(&vfd_lock);

    if (!active) return real_read(fd, buf, count);

    ssize_t r;
    if (pos < MKV_HEADER_SIZE) {
        _in_hook = 1;
        r = real_pread(fd, buf, count, pos);
        _in_hook = 0;
    } else {
        r = _spore_read(token, pos, buf, count);
        if (r < 0) { errno = EIO; return -1; }
    }
    if (r > 0) {
        pthread_rwlock_wrlock(&vfd_lock);
        if (vfd_table[fd].active) vfd_table[fd].seek_pos += r;
        pthread_rwlock_unlock(&vfd_lock);
    }
    return r;
}

/* ── Intercepted: pread() ───────────────────────────────────────────────────── */
ssize_t pread(int fd, void *buf, size_t count, off_t offset) {
    if (_in_hook || !real_pread || fd < 0 || fd >= MAX_FD)
        return real_pread ? real_pread(fd, buf, count, offset) : -1;

    pthread_rwlock_rdlock(&vfd_lock);
    int active = vfd_table[fd].active;
    char token[33];
    strncpy(token, vfd_table[fd].token, 33);
    pthread_rwlock_unlock(&vfd_lock);

    if (!active) return real_pread(fd, buf, count, offset);

    if (offset < MKV_HEADER_SIZE) {
        _in_hook = 1;
        ssize_t r = real_pread(fd, buf, count, offset);
        _in_hook = 0;
        return r;
    }
    ssize_t r = _spore_read(token, offset, buf, count);
    if (r < 0) { errno = EIO; return -1; }
    return r;
}

/* pread64 is the same as pread on 64-bit Linux; provide wrapper for safety */
ssize_t pread64(int fd, void *buf, size_t count, off64_t offset) {
    return pread(fd, buf, count, (off_t)offset);
}

/* ── Intercepted: fstat() ───────────────────────────────────────────────────── */
int fstat(int fd, struct stat *st) {
    if (!real_fstat) return -1;
    int r = real_fstat(fd, st);
    if (r != 0 || fd < 0 || fd >= MAX_FD) return r;

    pthread_rwlock_rdlock(&vfd_lock);
    int active = vfd_table[fd].active;
    off_t cdn_size = vfd_table[fd].cdn_size;
    pthread_rwlock_unlock(&vfd_lock);

    if (active && cdn_size > 0)
        st->st_size = cdn_size;
    return 0;
}

/* fstat64 on 64-bit Linux uses struct stat64 */
int fstat64(int fd, struct stat64 *st) {
    if (!real_fstat) return -1;
    /* Cast: on 64-bit glibc stat == stat64; _FILE_OFFSET_BITS=64 ensures this. */
    int r = real_fstat(fd, (struct stat *)st);
    if (r != 0 || fd < 0 || fd >= MAX_FD) return r;

    pthread_rwlock_rdlock(&vfd_lock);
    int active = vfd_table[fd].active;
    off_t cdn_size = vfd_table[fd].cdn_size;
    pthread_rwlock_unlock(&vfd_lock);

    if (active && cdn_size > 0)
        st->st_size = cdn_size;
    return 0;
}

/* ── Intercepted: lseek() ───────────────────────────────────────────────────── */
off_t lseek(int fd, off_t offset, int whence) {
    if (!real_lseek || fd < 0 || fd >= MAX_FD)
        return real_lseek ? real_lseek(fd, offset, whence) : -1;

    pthread_rwlock_rdlock(&vfd_lock);
    int active = vfd_table[fd].active;
    off_t pos  = vfd_table[fd].seek_pos;
    off_t size = vfd_table[fd].cdn_size;
    pthread_rwlock_unlock(&vfd_lock);

    if (!active) return real_lseek(fd, offset, whence);

    off_t new_pos;
    switch (whence) {
        case SEEK_SET: new_pos = offset;         break;
        case SEEK_CUR: new_pos = pos + offset;   break;
        case SEEK_END: new_pos = size + offset;  break;
        default: errno = EINVAL; return (off_t)-1;
    }
    if (new_pos < 0) { errno = EINVAL; return (off_t)-1; }

    pthread_rwlock_wrlock(&vfd_lock);
    if (vfd_table[fd].active) vfd_table[fd].seek_pos = new_pos;
    pthread_rwlock_unlock(&vfd_lock);
    return new_pos;
}

off64_t lseek64(int fd, off64_t offset, int whence) {
    return (off64_t)lseek(fd, (off_t)offset, whence);
}

/* ── Intercepted: close() ───────────────────────────────────────────────────── */
int close(int fd) {
    if (fd >= 0 && fd < MAX_FD) {
        pthread_rwlock_wrlock(&vfd_lock);
        vfd_table[fd].active = 0;
        pthread_rwlock_unlock(&vfd_lock);
    }
    return real_close ? real_close(fd) : -1;
}

/* ── Intercepted: mmap() ────────────────────────────────────────────────────── */
void *mmap(void *addr, size_t length, int prot, int flags, int fd, off_t offset) {
    if (!real_mmap)
        return MAP_FAILED;

    if (fd >= 0 && fd < MAX_FD) {
        pthread_rwlock_rdlock(&vfd_lock);
        int active = vfd_table[fd].active;
        pthread_rwlock_unlock(&vfd_lock);
        if (active) {
            /* Return ENODEV so Plex falls back to read()-based I/O */
            errno = ENODEV;
            return MAP_FAILED;
        }
    }
    return real_mmap(addr, length, prot, flags, fd, offset);
}
