#define _GNU_SOURCE

#include <errno.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <linux/openat2.h>
#include <linux/stat.h>
#include <limits.h>
#include <pthread.h>
#include <sqlite3.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/xattr.h>
#include <unistd.h>

#ifndef RENAME_NOREPLACE
#define RENAME_NOREPLACE (1U << 0)
#endif
#ifndef RENAME_EXCHANGE
#define RENAME_EXCHANGE (1U << 1)
#endif
#ifndef F_OFD_GETLK
#define F_OFD_GETLK 36
#define F_OFD_SETLK 37
#endif
#ifndef STATX_MNT_ID
#define STATX_MNT_ID 0x00001000U
#endif

#define BOTS5_PENDING_BYTE ((off_t)0x40000000)
#define BOTS5_RESERVED_BYTE (BOTS5_PENDING_BYTE + 1)
#define BOTS5_SHARED_FIRST (BOTS5_PENDING_BYTE + 2)
#define BOTS5_SHARED_SIZE 510
#define BOTS5_CLOSE_HELD 0
#define BOTS5_CLOSE_RELEASED 1
#define BOTS5_CLOSE_UNKNOWN 2

#define BOTS5_FILE_OTHER 0
#define BOTS5_FILE_MAIN 1
#define BOTS5_FILE_MAIN_JOURNAL 2
#define BOTS5_FILE_WAL 3
#define BOTS5_FILE_TEMP 4

#define BOTS5_TRACE_MAIN_WRITE 1
#define BOTS5_TRACE_MAIN_SYNC 2
#define BOTS5_TRACE_JOURNAL_SYNC 3
#define BOTS5_TRACE_WAL_SYNC 4
#define BOTS5_TRACE_CREATE_PARENT_SYNC 5
#define BOTS5_TRACE_JOURNAL_DELETE 6
#define BOTS5_TRACE_WAL_DELETE 7
#define BOTS5_TRACE_DELETE_PARENT_SYNC 8
#define BOTS5_TRACE_DELETE_PROOF_CLOSE 9
#define BOTS5_TRACE_JOURNAL_OPEN_CREATE 10
#define BOTS5_TRACE_WAL_OPEN_CREATE 11
#define BOTS5_TRACE_INVALID_SYNC 12
#define BOTS5_TRACE_CAPACITY 256

#define BOTS5_CLEANUP_XOPEN_REJECT 1
#define BOTS5_CLEANUP_XDELETE_REJECT 2
#define BOTS5_CLEANUP_AUXILIARY 3
#define BOTS5_CLEANUP_TEMP 4
#define BOTS5_CLEANUP_XDELETE_PROOF 5
#define BOTS5_CLEANUP_REGISTER_DB_DIR 6
#define BOTS5_CLEANUP_REGISTER_MAIN 7
#define BOTS5_CLEANUP_REGISTER_TEMP 8

typedef struct BotsVfs BotsVfs;

typedef struct BotsFile {
    sqlite3_file base;
    BotsVfs *owner;
    int fd;
    int lock_level;
    int delete_on_close;
    int is_main;
    int file_class;
    int open_flags;
    int needs_parent_sync;
    void *shm;
    size_t shm_size;
    struct BotsFile *next_open;
} BotsFile;

struct BotsVfs {
    sqlite3_vfs base;
    sqlite3_vfs *delegate;
    char *name;
    char *synthetic;
    char *main_leaf;
    char *journal_leaf;
    int db_dir_fd;
    int main_claim_fd;
    int temp_dir_fd;
    unsigned long long mount_id;
    uid_t euid;
    pid_t owner_pid;
    int intake_wal;
    int open_count;
    int registered;
    int test_close_fault_slot;
    int test_close_fault_mode;
    int test_io_fault_event;
    int test_cleanup_close_site;
    int test_cleanup_close_mode;
    int test_last_cleanup_fd;
    int trace[BOTS5_TRACE_CAPACITY];
    int trace_count;
    atomic_uint unknown_close_generation;
    BotsFile *files;
    pthread_mutex_t mutex;
    BotsVfs *next;
};

static BotsVfs *g_vfs_list = NULL;
static pthread_mutex_t g_vfs_mutex = PTHREAD_MUTEX_INITIALIZER;
static _Thread_local char g_error[256];
#define BOTS5_THREAD_VFS_SLOTS 64
#define BOTS5_VFS_NAME_CAPACITY 64
typedef struct ThreadUnknownClose {
    char name[BOTS5_VFS_NAME_CAPACITY];
    unsigned int generation;
} ThreadUnknownClose;
static _Thread_local ThreadUnknownClose
    g_thread_unknown_close[BOTS5_THREAD_VFS_SLOTS];
static _Thread_local unsigned int g_thread_unknown_close_next = 0U;
static int g_test_registration_cleanup_slot = 0;
static int g_test_registration_cleanup_mode = 0;

static int set_error(const char *message) {
    snprintf(g_error, sizeof(g_error), "%s", message ? message : "native helper failure");
    return -1;
}

const char *bots5_last_error(void) { return g_error; }

int bots5_runtime_sqlite_matches(void) {
    void *resolved = dlsym(RTLD_DEFAULT, "sqlite3_vfs_find");
    if (resolved == NULL || resolved != (void *)(uintptr_t)&sqlite3_vfs_find) {
        return set_error("rooted VFS is not linked to Python's active SQLite instance");
    }
    return 0;
}

static int owner_ok(BotsVfs *vfs) {
    return vfs != NULL && vfs->owner_pid == getpid();
}

static int cleanup_fault_armed(BotsVfs *vfs, int site) {
    return vfs != NULL && vfs->test_cleanup_close_site == site;
}

static void record_unknown_close(BotsVfs *vfs) {
    if (vfs != NULL) {
        unsigned int i;
        ThreadUnknownClose *entry = NULL;
        (void)atomic_fetch_add_explicit(
            &vfs->unknown_close_generation, 1U, memory_order_release
        );
        for (i = 0U; i < BOTS5_THREAD_VFS_SLOTS; i++) {
            if (g_thread_unknown_close[i].name[0] != '\0' &&
                strcmp(g_thread_unknown_close[i].name, vfs->name) == 0) {
                entry = &g_thread_unknown_close[i];
                break;
            }
        }
        if (entry == NULL) {
            entry = &g_thread_unknown_close[
                g_thread_unknown_close_next++ % BOTS5_THREAD_VFS_SLOTS
            ];
            (void)snprintf(entry->name, sizeof(entry->name), "%s", vfs->name);
            entry->generation = 0U;
        }
        entry->generation++;
    }
}

static int close_untracked_fd(BotsVfs *vfs, int *owned_fd, int site) {
    int fd;
    int mode = 0;
    int result;
    if (owned_fd == NULL || *owned_fd < 0) return 0;
    fd = *owned_fd;
    *owned_fd = -1;
    if (vfs != NULL) vfs->test_last_cleanup_fd = fd;
    if (cleanup_fault_armed(vfs, site)) {
        mode = vfs->test_cleanup_close_mode;
        vfs->test_cleanup_close_site = 0;
        vfs->test_cleanup_close_mode = 0;
    } else if (vfs != NULL && site == BOTS5_CLEANUP_XDELETE_PROOF &&
               vfs->test_io_fault_event == BOTS5_TRACE_DELETE_PROOF_CLOSE) {
        mode = 1;
    }
    if (mode == 1) {
        record_unknown_close(vfs);
        errno = EIO;
        return -1;
    }
    result = close(fd);
    if (mode == 2) {
        record_unknown_close(vfs);
        errno = EINTR;
        return -1;
    }
    if (result != 0) record_unknown_close(vfs);
    return result;
}

static void trace_event(BotsVfs *vfs, int event) {
    if (vfs == NULL || event == 0) return;
    pthread_mutex_lock(&vfs->mutex);
    if (vfs->trace_count < BOTS5_TRACE_CAPACITY)
        vfs->trace[vfs->trace_count++] = event;
    pthread_mutex_unlock(&vfs->mutex);
}

static int component_ok(const char *name) {
    size_t n;
    if (name == NULL || name[0] == '\0') return 0;
    n = strlen(name);
    if (n > 255 || strcmp(name, ".") == 0 || strcmp(name, "..") == 0) return 0;
    return strchr(name, '/') == NULL && strchr(name, '\\') == NULL;
}

static int cloexec_dup(int fd) {
    return fcntl(fd, F_DUPFD_CLOEXEC, 3);
}

static int fd_mount_id(int fd, unsigned long long *value) {
    struct statx sx;
    memset(&sx, 0, sizeof(sx));
    if (syscall(SYS_statx, fd, "", AT_EMPTY_PATH | AT_NO_AUTOMOUNT,
                STATX_TYPE | STATX_MODE | STATX_UID | STATX_INO | STATX_MNT_ID,
                &sx) != 0) {
        return -1;
    }
    if ((sx.stx_mask & STATX_MNT_ID) == 0) {
        errno = EOPNOTSUPP;
        return -1;
    }
    *value = sx.stx_mnt_id;
    return 0;
}

int bots5_statx_fd(int fd, unsigned long long *dev_major,
                   unsigned long long *dev_minor, unsigned long long *inode,
                   unsigned long long *mount_id, unsigned int *mode,
                   unsigned int *uid, unsigned int *nlink, long long *size) {
    struct statx sx;
    memset(&sx, 0, sizeof(sx));
    if (syscall(SYS_statx, fd, "", AT_EMPTY_PATH | AT_NO_AUTOMOUNT,
                STATX_BASIC_STATS | STATX_MNT_ID, &sx) != 0) {
        snprintf(g_error, sizeof(g_error), "statx(AT_EMPTY_PATH): %s", strerror(errno));
        return -1;
    }
    if ((sx.stx_mask & STATX_MNT_ID) == 0) return set_error("STATX_MNT_ID is unavailable");
    *dev_major = sx.stx_dev_major;
    *dev_minor = sx.stx_dev_minor;
    *inode = sx.stx_ino;
    *mount_id = sx.stx_mnt_id;
    *mode = sx.stx_mode;
    *uid = sx.stx_uid;
    *nlink = sx.stx_nlink;
    *size = sx.stx_size;
    return 0;
}

int bots5_open_component(int parent_fd, const char *name, int flags,
                         unsigned int mode, unsigned long long resolve) {
    struct open_how how;
    int fd;
    if (!component_ok(name)) {
        errno = EINVAL;
        return set_error("invalid descriptor-relative component");
    }
    memset(&how, 0, sizeof(how));
    how.flags = ((uint64_t)flags) | O_CLOEXEC;
    how.mode = mode;
    how.resolve = resolve;
    fd = (int)syscall(SYS_openat2, parent_fd, name, &how, sizeof(how));
    if (fd < 0) {
        snprintf(g_error, sizeof(g_error), "openat2(%s): %s", name, strerror(errno));
    }
    return fd;
}

int bots5_open_fresh_directory(int retained_fd) {
    struct open_how how;
    int fd;
    memset(&how, 0, sizeof(how));
    how.flags = O_RDONLY | O_DIRECTORY | O_CLOEXEC;
    how.resolve = RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS |
                  RESOLVE_NO_SYMLINKS | RESOLVE_NO_XDEV;
    fd = (int)syscall(SYS_openat2, retained_fd, ".", &how, sizeof(how));
    if (fd < 0) {
        snprintf(g_error, sizeof(g_error), "openat2(.): %s", strerror(errno));
    }
    return fd;
}

int bots5_renameat2(int old_fd, const char *old_name, int new_fd,
                    const char *new_name, unsigned int flags) {
    if (!component_ok(old_name) || !component_ok(new_name)) {
        errno = EINVAL;
        return set_error("invalid rename component");
    }
    if (flags != RENAME_NOREPLACE && flags != RENAME_EXCHANGE) {
        errno = EINVAL;
        return set_error("unsupported rename mode");
    }
    if (syscall(SYS_renameat2, old_fd, old_name, new_fd, new_name, flags) != 0) {
        snprintf(g_error, sizeof(g_error), "renameat2: %s", strerror(errno));
        return -1;
    }
    return 0;
}

static int validate_regular(BotsVfs *vfs, int fd, int exact_mode) {
    struct stat st;
    unsigned long long mount_id;
    if (fstat(fd, &st) != 0) return -1;
    if (!S_ISREG(st.st_mode) || st.st_uid != vfs->euid || st.st_nlink != 1) {
        errno = EPERM;
        return -1;
    }
    if (exact_mode >= 0 && (st.st_mode & 07777) != (mode_t)exact_mode) {
        errno = EPERM;
        return -1;
    }
    if (fd_mount_id(fd, &mount_id) != 0 || mount_id != vfs->mount_id) {
        errno = EXDEV;
        return -1;
    }
    errno = 0;
    if (fgetxattr(fd, "system.posix_acl_access", NULL, 0) >= 0 ||
        (errno != ENODATA && errno != EOPNOTSUPP && errno != ENOTSUP)) {
        errno = EPERM;
        return -1;
    }
    return 0;
}

static int same_file(int first, int second) {
    struct stat a, b;
    return fstat(first, &a) == 0 && fstat(second, &b) == 0 &&
           a.st_dev == b.st_dev && a.st_ino == b.st_ino;
}

static int ofd_lock(int fd, short type, off_t start, off_t length) {
    struct flock lock;
    memset(&lock, 0, sizeof(lock));
    lock.l_type = type;
    lock.l_whence = SEEK_SET;
    lock.l_start = start;
    lock.l_len = length;
    return fcntl(fd, F_OFD_SETLK, &lock);
}

static int bots_close(sqlite3_file *file) {
    BotsFile *f = (BotsFile *)file;
    BotsVfs *vfs = f->owner;
    int result = SQLITE_OK;
    if (f->shm != NULL && f->shm_size != 0) {
        if (munmap(f->shm, f->shm_size) != 0) result = SQLITE_IOERR_SHMMAP;
        f->shm = NULL;
        f->shm_size = 0;
    }
    if (f->fd >= 0) {
        int fd = f->fd;
        f->fd = -1;
        (void)ofd_lock(fd, F_UNLCK, BOTS5_PENDING_BYTE, BOTS5_SHARED_SIZE + 2);
        if (close(fd) != 0) {
            result = SQLITE_IOERR_CLOSE;
            record_unknown_close(vfs);
        }
    }
    /* Keep the VFS visibly busy until every consequential close outcome has
       been classified.  unregister therefore cannot free vfs while xClose
       still needs to record an UNKNOWN result. */
    if (vfs != NULL) {
        BotsFile **cursor;
        pthread_mutex_lock(&vfs->mutex);
        cursor = &vfs->files;
        while (*cursor != NULL && *cursor != f) cursor = &(*cursor)->next_open;
        if (*cursor == f) *cursor = f->next_open;
        if (vfs->open_count > 0) vfs->open_count--;
        pthread_mutex_unlock(&vfs->mutex);
        f->owner = NULL;
        f->next_open = NULL;
    }
    memset(file, 0, sizeof(BotsFile));
    return result;
}

static int bots_read(sqlite3_file *file, void *buffer, int amount, sqlite3_int64 offset) {
    BotsFile *f = (BotsFile *)file;
    ssize_t total = 0;
    if (!owner_ok(f->owner) || f->fd < 0) return SQLITE_IOERR_READ;
    while (total < amount) {
        ssize_t got = pread(f->fd, (char *)buffer + total, (size_t)(amount - total), offset + total);
        if (got < 0 && errno == EINTR) continue;
        if (got < 0) return SQLITE_IOERR_READ;
        if (got == 0) {
            memset((char *)buffer + total, 0, (size_t)(amount - total));
            return SQLITE_IOERR_SHORT_READ;
        }
        total += got;
    }
    return SQLITE_OK;
}

static int bots_write(sqlite3_file *file, const void *buffer, int amount, sqlite3_int64 offset) {
    BotsFile *f = (BotsFile *)file;
    ssize_t total = 0;
    if (!owner_ok(f->owner) || f->fd < 0) return SQLITE_IOERR_WRITE;
    if (f->file_class == BOTS5_FILE_MAIN)
        trace_event(f->owner, BOTS5_TRACE_MAIN_WRITE);
    while (total < amount) {
        ssize_t wrote = pwrite(f->fd, (const char *)buffer + total,
                               (size_t)(amount - total), offset + total);
        if (wrote < 0 && errno == EINTR) continue;
        if (wrote <= 0) return SQLITE_IOERR_WRITE;
        total += wrote;
    }
    return SQLITE_OK;
}

static int bots_truncate(sqlite3_file *file, sqlite3_int64 size) {
    BotsFile *f = (BotsFile *)file;
    if (!owner_ok(f->owner) || f->fd < 0 || ftruncate(f->fd, size) != 0) return SQLITE_IOERR_TRUNCATE;
    return SQLITE_OK;
}

static int bots_sync(sqlite3_file *file, int flags) {
    BotsFile *f = (BotsFile *)file;
    int rc;
    if (!owner_ok(f->owner) || f->fd < 0) return SQLITE_IOERR_FSYNC;
    if (flags != SQLITE_SYNC_NORMAL && flags != SQLITE_SYNC_FULL &&
        flags != (SQLITE_SYNC_NORMAL | SQLITE_SYNC_DATAONLY) &&
        flags != (SQLITE_SYNC_FULL | SQLITE_SYNC_DATAONLY)) {
        trace_event(f->owner, BOTS5_TRACE_INVALID_SYNC);
        return SQLITE_IOERR_FSYNC;
    }
    do { rc = fsync(f->fd); } while (rc != 0 && errno == EINTR);
    if (rc != 0) return SQLITE_IOERR_FSYNC;
    trace_event(
        f->owner,
        f->file_class == BOTS5_FILE_MAIN ? BOTS5_TRACE_MAIN_SYNC :
        f->file_class == BOTS5_FILE_MAIN_JOURNAL ? BOTS5_TRACE_JOURNAL_SYNC :
        f->file_class == BOTS5_FILE_WAL ? BOTS5_TRACE_WAL_SYNC : 0
    );
    if (f->needs_parent_sync) {
        trace_event(f->owner, BOTS5_TRACE_CREATE_PARENT_SYNC);
        if (f->owner->test_io_fault_event == BOTS5_TRACE_CREATE_PARENT_SYNC)
            return SQLITE_IOERR_DIR_FSYNC;
        do { rc = fsync(f->owner->db_dir_fd); } while (rc != 0 && errno == EINTR);
        if (rc != 0) return SQLITE_IOERR_DIR_FSYNC;
        f->needs_parent_sync = 0;
    }
    return SQLITE_OK;
}

static int bots_file_size(sqlite3_file *file, sqlite3_int64 *size) {
    BotsFile *f = (BotsFile *)file;
    struct stat st;
    if (!owner_ok(f->owner) || f->fd < 0 || fstat(f->fd, &st) != 0) return SQLITE_IOERR_FSTAT;
    *size = st.st_size;
    return SQLITE_OK;
}

static int bots_lock(sqlite3_file *file, int level) {
    BotsFile *f = (BotsFile *)file;
    int rc = 0;
    int prior;
    if (!owner_ok(f->owner) || !f->is_main || f->fd < 0) return SQLITE_IOERR_LOCK;
    if (level <= f->lock_level) return SQLITE_OK;
    prior = f->lock_level;
    if (level >= SQLITE_LOCK_SHARED && f->lock_level < SQLITE_LOCK_SHARED)
        rc = ofd_lock(f->fd, F_RDLCK, BOTS5_SHARED_FIRST, BOTS5_SHARED_SIZE);
    if (rc == 0 && level >= SQLITE_LOCK_RESERVED && f->lock_level < SQLITE_LOCK_RESERVED)
        rc = ofd_lock(f->fd, F_WRLCK, BOTS5_RESERVED_BYTE, 1);
    if (rc == 0 && level >= SQLITE_LOCK_PENDING && f->lock_level < SQLITE_LOCK_PENDING)
        rc = ofd_lock(f->fd, F_WRLCK, BOTS5_PENDING_BYTE, 1);
    if (rc == 0 && level >= SQLITE_LOCK_EXCLUSIVE && f->lock_level < SQLITE_LOCK_EXCLUSIVE)
        rc = ofd_lock(f->fd, F_WRLCK, BOTS5_SHARED_FIRST, BOTS5_SHARED_SIZE);
    if (rc != 0) {
        if (prior < SQLITE_LOCK_PENDING && level >= SQLITE_LOCK_PENDING)
            (void)ofd_lock(f->fd, F_UNLCK, BOTS5_PENDING_BYTE, 1);
        return SQLITE_BUSY;
    }
    f->lock_level = level;
    return SQLITE_OK;
}

static int bots_unlock(sqlite3_file *file, int level) {
    BotsFile *f = (BotsFile *)file;
    if (!owner_ok(f->owner) || !f->is_main || f->fd < 0) return SQLITE_IOERR_UNLOCK;
    if (level >= f->lock_level) return SQLITE_OK;
    if (ofd_lock(f->fd, F_UNLCK, BOTS5_PENDING_BYTE, BOTS5_SHARED_SIZE + 2) != 0)
        return SQLITE_IOERR_UNLOCK;
    f->lock_level = SQLITE_LOCK_NONE;
    if (level >= SQLITE_LOCK_SHARED) {
        if (ofd_lock(f->fd, F_RDLCK, BOTS5_SHARED_FIRST, BOTS5_SHARED_SIZE) != 0)
            return SQLITE_IOERR_UNLOCK;
        f->lock_level = SQLITE_LOCK_SHARED;
    }
    if (level >= SQLITE_LOCK_RESERVED) {
        if (ofd_lock(f->fd, F_WRLCK, BOTS5_RESERVED_BYTE, 1) != 0)
            return SQLITE_IOERR_UNLOCK;
        f->lock_level = SQLITE_LOCK_RESERVED;
    }
    return SQLITE_OK;
}

static int bots_check_reserved(sqlite3_file *file, int *result) {
    BotsFile *f = (BotsFile *)file;
    struct flock lock;
    if (!owner_ok(f->owner) || f->fd < 0) return SQLITE_IOERR_CHECKRESERVEDLOCK;
    if (f->lock_level >= SQLITE_LOCK_RESERVED) {
        *result = 1;
        return SQLITE_OK;
    }
    memset(&lock, 0, sizeof(lock));
    lock.l_type = F_WRLCK;
    lock.l_whence = SEEK_SET;
    lock.l_start = BOTS5_RESERVED_BYTE;
    lock.l_len = 1;
    if (fcntl(f->fd, F_OFD_GETLK, &lock) != 0) return SQLITE_IOERR_CHECKRESERVEDLOCK;
    *result = lock.l_type != F_UNLCK;
    return SQLITE_OK;
}

static int bots_file_control(sqlite3_file *file, int op, void *arg) {
    BotsFile *f = (BotsFile *)file;
    if (!owner_ok(f->owner)) return SQLITE_IOERR;
    switch (op) {
    case SQLITE_FCNTL_LOCKSTATE:
        *(int *)arg = f->lock_level;
        return SQLITE_OK;
    case SQLITE_FCNTL_SIZE_HINT:
        /* Advisory only. Extending the logical EOF here would make a rolled
         * back startup behavior probe change the migration position hash. */
        (void)arg;
        return SQLITE_OK;
    case SQLITE_FCNTL_CHUNK_SIZE:
    case SQLITE_FCNTL_SYNC:
    case SQLITE_FCNTL_COMMIT_PHASETWO:
        return SQLITE_OK;
    case SQLITE_FCNTL_VFSNAME:
        *(char **)arg = sqlite3_mprintf("bots5-rooted/%s", f->owner->name);
        return SQLITE_OK;
    default:
        return SQLITE_NOTFOUND;
    }
}

static int bots_sector_size(sqlite3_file *file) { (void)file; return 4096; }
static int bots_device_characteristics(sqlite3_file *file) { (void)file; return 0; }
static int bots_shm_map(sqlite3_file *file, int i, int s, int e, void volatile **p) {
    BotsFile *f = (BotsFile *)file;
    size_t required;
    void *next;
    if (!owner_ok(f->owner) || !f->is_main || !f->owner->intake_wal || i < 0 || s <= 0)
        return SQLITE_IOERR_SHMOPEN;
    required = ((size_t)i + 1U) * (size_t)s;
    if (required > f->shm_size) {
        if (!e) { *p = NULL; return SQLITE_OK; }
        next = mmap(NULL, required, PROT_READ | PROT_WRITE,
                    MAP_SHARED | MAP_ANONYMOUS, -1, 0);
        if (next == MAP_FAILED) return SQLITE_IOERR_SHMMAP;
        if (f->shm != NULL) {
            memcpy(next, f->shm, f->shm_size);
            munmap(f->shm, f->shm_size);
        }
        f->shm = next;
        f->shm_size = required;
    }
    *p = (void volatile *)((char *)f->shm + ((size_t)i * (size_t)s));
    return SQLITE_OK;
}
static int bots_shm_lock(sqlite3_file *f, int o, int n, int flags) {
    BotsFile *file = (BotsFile *)f;
    (void)o; (void)n; (void)flags;
    return owner_ok(file->owner) && file->owner->intake_wal ? SQLITE_OK : SQLITE_IOERR_SHMLOCK;
}
static void bots_shm_barrier(sqlite3_file *f) { (void)f; __sync_synchronize(); }
static int bots_shm_unmap(sqlite3_file *file, int del) {
    BotsFile *f = (BotsFile *)file;
    (void)del;
    if (f->shm != NULL && f->shm_size != 0) {
        if (munmap(f->shm, f->shm_size) != 0) return SQLITE_IOERR_SHMMAP;
        f->shm = NULL;
        f->shm_size = 0;
    }
    return SQLITE_OK;
}
static int bots_fetch(sqlite3_file *f, sqlite3_int64 o, int a, void **p) {
    (void)f; (void)o; (void)a; *p = NULL; return SQLITE_OK;
}
static int bots_unfetch(sqlite3_file *f, sqlite3_int64 o, void *p) {
    (void)f; (void)o; (void)p; return SQLITE_OK;
}

static const sqlite3_io_methods bots_io = {
    3, bots_close, bots_read, bots_write, bots_truncate, bots_sync,
    bots_file_size, bots_lock, bots_unlock, bots_check_reserved,
    bots_file_control, bots_sector_size, bots_device_characteristics,
    bots_shm_map, bots_shm_lock, bots_shm_barrier, bots_shm_unmap,
    bots_fetch, bots_unfetch
};

static int random_temp_fd(BotsVfs *vfs) {
    int fd;
#ifdef O_TMPFILE
    fd = openat(vfs->temp_dir_fd, ".", O_TMPFILE | O_RDWR | O_CLOEXEC, 0600);
    if (fd >= 0) {
        if (!cleanup_fault_armed(vfs, BOTS5_CLEANUP_TEMP) &&
            fchmod(fd, 0600) == 0) return fd;
        (void)close_untracked_fd(vfs, &fd, BOTS5_CLEANUP_TEMP);
        return -1;
    }
#endif
    for (int attempt = 0; attempt < 32; attempt++) {
        unsigned char random_bytes[16];
        char name[48];
        ssize_t got = getrandom(random_bytes, sizeof(random_bytes), 0);
        if (got != (ssize_t)sizeof(random_bytes)) return -1;
        snprintf(name, sizeof(name), ".sqlite-temp-%02x%02x%02x%02x%02x%02x%02x%02x",
                 random_bytes[0], random_bytes[1], random_bytes[2], random_bytes[3],
                 random_bytes[4], random_bytes[5], random_bytes[6], random_bytes[7]);
        fd = openat(vfs->temp_dir_fd, name,
                    O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (fd >= 0) {
            if (cleanup_fault_armed(vfs, BOTS5_CLEANUP_TEMP) ||
                fchmod(fd, 0600) != 0) {
                (void)close_untracked_fd(vfs, &fd, BOTS5_CLEANUP_TEMP);
                (void)unlinkat(vfs->temp_dir_fd, name, 0);
                return -1;
            }
            if (unlinkat(vfs->temp_dir_fd, name, 0) != 0) {
                (void)close_untracked_fd(vfs, &fd, BOTS5_CLEANUP_TEMP);
                return -1;
            }
            return fd;
        }
        if (errno != EEXIST) return -1;
    }
    errno = EEXIST;
    return -1;
}

static int open_auxiliary(BotsVfs *vfs, const char *leaf) {
    int created = 0;
    int fd = openat(vfs->db_dir_fd, leaf, O_RDWR | O_CLOEXEC | O_NOFOLLOW);
    if (fd >= 0) return fd;
    if (errno != ENOENT) return -1;
    fd = openat(vfs->db_dir_fd, leaf,
                O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd >= 0) created = 1;
    if (fd >= 0 &&
        (cleanup_fault_armed(vfs, BOTS5_CLEANUP_AUXILIARY) ||
         fchmod(fd, 0600) != 0)) {
        (void)close_untracked_fd(vfs, &fd, BOTS5_CLEANUP_AUXILIARY);
        if (created) (void)unlinkat(vfs->db_dir_fd, leaf, 0);
        return -1;
    }
    return fd;
}

static int bots_xopen(sqlite3_vfs *base, const char *name, sqlite3_file *file,
                      int flags, int *out_flags) {
    BotsVfs *vfs = (BotsVfs *)base;
    BotsFile *f = (BotsFile *)file;
    int fd = -1;
    int is_main = (flags & SQLITE_OPEN_MAIN_DB) != 0;
    int is_journal = (flags & SQLITE_OPEN_MAIN_JOURNAL) != 0;
    int is_wal = (flags & SQLITE_OPEN_WAL) != 0;
    int is_temp = (flags & (SQLITE_OPEN_TEMP_DB | SQLITE_OPEN_TEMP_JOURNAL |
                            SQLITE_OPEN_TRANSIENT_DB | SQLITE_OPEN_SUBJOURNAL)) != 0;
    memset(f, 0, sizeof(*f));
    f->fd = -1;
    if (!owner_ok(vfs)) return SQLITE_MISUSE;
    if (
        !!(flags & SQLITE_OPEN_MAIN_DB)
        + !!(flags & SQLITE_OPEN_TEMP_DB)
        + !!(flags & SQLITE_OPEN_TRANSIENT_DB)
        + !!(flags & SQLITE_OPEN_MAIN_JOURNAL)
        + !!(flags & SQLITE_OPEN_TEMP_JOURNAL)
        + !!(flags & SQLITE_OPEN_SUBJOURNAL)
        + !!(flags & SQLITE_OPEN_SUPER_JOURNAL)
        + !!(flags & SQLITE_OPEN_WAL)
        != 1
    ) return SQLITE_CANTOPEN;
    if (is_main) {
        char capability[64];
        if (name == NULL || strcmp(name, vfs->synthetic) != 0) return SQLITE_CANTOPEN;
        if (snprintf(capability, sizeof(capability), "/proc/self/fd/%d",
                     vfs->main_claim_fd) >= (int)sizeof(capability))
            return SQLITE_CANTOPEN;
        fd = open(capability, O_RDWR | O_CLOEXEC);
        if (fd < 0 || cleanup_fault_armed(vfs, BOTS5_CLEANUP_XOPEN_REJECT) ||
            !same_file(fd, vfs->main_claim_fd) ||
            validate_regular(vfs, fd, 0600) != 0) {
            if (fd >= 0)
                (void)close_untracked_fd(
                    vfs, &fd, BOTS5_CLEANUP_XOPEN_REJECT
                );
            return SQLITE_CANTOPEN;
        }
    } else if (is_journal) {
        size_t n = strlen(vfs->synthetic);
        if (name == NULL || strncmp(name, vfs->synthetic, n) != 0 || strcmp(name + n, "-journal") != 0)
            return SQLITE_CANTOPEN;
        fd = open_auxiliary(vfs, vfs->journal_leaf);
        if (fd < 0 || validate_regular(vfs, fd, 0600) != 0) {
            if (fd >= 0)
                (void)close_untracked_fd(
                    vfs, &fd, BOTS5_CLEANUP_XOPEN_REJECT
                );
            return SQLITE_CANTOPEN;
        }
    } else if (is_wal && vfs->intake_wal) {
        size_t n = strlen(vfs->synthetic);
        char *leaf;
        if (name == NULL || strncmp(name, vfs->synthetic, n) != 0 ||
            strcmp(name + n, "-wal") != 0)
            return SQLITE_CANTOPEN;
        leaf = sqlite3_mprintf("%s-wal", vfs->main_leaf);
        if (leaf == NULL) return SQLITE_NOMEM;
        fd = open_auxiliary(vfs, leaf);
        sqlite3_free(leaf);
        if (fd < 0 || validate_regular(vfs, fd, 0600) != 0) {
            if (fd >= 0)
                (void)close_untracked_fd(
                    vfs, &fd, BOTS5_CLEANUP_XOPEN_REJECT
                );
            return SQLITE_CANTOPEN;
        }
    } else if (is_temp && name == NULL && (flags & SQLITE_OPEN_DELETEONCLOSE)) {
        fd = random_temp_fd(vfs);
        if (fd < 0 || validate_regular(vfs, fd, 0600) != 0) {
            if (fd >= 0)
                (void)close_untracked_fd(
                    vfs, &fd, BOTS5_CLEANUP_XOPEN_REJECT
                );
            return SQLITE_CANTOPEN;
        }
        f->delete_on_close = 1;
    } else {
        return SQLITE_CANTOPEN;
    }
    f->base.pMethods = &bots_io;
    f->owner = vfs;
    f->fd = fd;
    f->lock_level = SQLITE_LOCK_NONE;
    f->is_main = is_main;
    f->file_class = is_main ? BOTS5_FILE_MAIN :
                    is_journal ? BOTS5_FILE_MAIN_JOURNAL :
                    is_wal ? BOTS5_FILE_WAL :
                    is_temp ? BOTS5_FILE_TEMP : BOTS5_FILE_OTHER;
    f->open_flags = flags;
    f->needs_parent_sync =
        (flags & SQLITE_OPEN_CREATE) != 0 && (is_journal || is_wal);
    pthread_mutex_lock(&vfs->mutex);
    f->next_open = vfs->files;
    vfs->files = f;
    vfs->open_count++;
    pthread_mutex_unlock(&vfs->mutex);
    if (f->needs_parent_sync)
        trace_event(
            vfs,
            is_journal ? BOTS5_TRACE_JOURNAL_OPEN_CREATE :
            BOTS5_TRACE_WAL_OPEN_CREATE
        );
    if (out_flags) *out_flags = flags;
    return SQLITE_OK;
}

static int journal_name(BotsVfs *vfs, const char *name) {
    size_t n = strlen(vfs->synthetic);
    return name != NULL && strncmp(name, vfs->synthetic, n) == 0 &&
           strcmp(name + n, "-journal") == 0;
}

static int wal_name(BotsVfs *vfs, const char *name) {
    size_t n = strlen(vfs->synthetic);
    return vfs->intake_wal && name != NULL &&
           strncmp(name, vfs->synthetic, n) == 0 && strcmp(name + n, "-wal") == 0;
}

static int shm_name(BotsVfs *vfs, const char *name) {
    size_t n = strlen(vfs->synthetic);
    return vfs->intake_wal && name != NULL &&
           strncmp(name, vfs->synthetic, n) == 0 && strcmp(name + n, "-shm") == 0;
}

static char *physical_aux_leaf(BotsVfs *vfs, const char *name) {
    if (journal_name(vfs, name)) return sqlite3_mprintf("%s", vfs->journal_leaf);
    if (wal_name(vfs, name)) return sqlite3_mprintf("%s-wal", vfs->main_leaf);
    if (shm_name(vfs, name)) return sqlite3_mprintf("%s-shm", vfs->main_leaf);
    return NULL;
}

static int bots_xdelete(sqlite3_vfs *base, const char *name, int sync_dir) {
    BotsVfs *vfs = (BotsVfs *)base;
    char *leaf;
    int fd;
    struct stat opened, named;
    int close_failed = 0;
    int sync_failed = 0;
    (void)sync_dir;
    if (!owner_ok(vfs)) return SQLITE_IOERR_DELETE;
    leaf = physical_aux_leaf(vfs, name);
    if (leaf == NULL) return SQLITE_IOERR_DELETE;
    fd = openat(vfs->db_dir_fd, leaf,
                O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0 && errno == ENOENT) {
        sqlite3_free(leaf);
        trace_event(vfs, BOTS5_TRACE_DELETE_PARENT_SYNC);
        if (vfs->test_io_fault_event == BOTS5_TRACE_DELETE_PARENT_SYNC)
            return SQLITE_IOERR_DIR_FSYNC;
        do { fd = fsync(vfs->db_dir_fd); } while (fd != 0 && errno == EINTR);
        return fd == 0 ? SQLITE_OK : SQLITE_IOERR_DIR_FSYNC;
    }
    if (fd < 0 || cleanup_fault_armed(vfs, BOTS5_CLEANUP_XDELETE_REJECT) ||
        validate_regular(vfs, fd, 0600) != 0 ||
        fstat(fd, &opened) != 0 ||
        fstatat(vfs->db_dir_fd, leaf, &named, AT_SYMLINK_NOFOLLOW) != 0 ||
        opened.st_dev != named.st_dev || opened.st_ino != named.st_ino) {
        if (fd >= 0)
            (void)close_untracked_fd(
                vfs, &fd, BOTS5_CLEANUP_XDELETE_REJECT
            );
        sqlite3_free(leaf);
        return SQLITE_IOERR_DELETE;
    }
    if (unlinkat(vfs->db_dir_fd, leaf, 0) != 0) {
        (void)close_untracked_fd(vfs, &fd, BOTS5_CLEANUP_XDELETE_REJECT);
        sqlite3_free(leaf);
        return SQLITE_IOERR_DELETE;
    }
    trace_event(vfs, journal_name(vfs, name) ? BOTS5_TRACE_JOURNAL_DELETE :
                                            BOTS5_TRACE_WAL_DELETE);
    trace_event(vfs, BOTS5_TRACE_DELETE_PROOF_CLOSE);
    if (close_untracked_fd(vfs, &fd, BOTS5_CLEANUP_XDELETE_PROOF) != 0) {
        close_failed = 1;
    }
    sqlite3_free(leaf);
    trace_event(vfs, BOTS5_TRACE_DELETE_PARENT_SYNC);
    if (vfs->test_io_fault_event == BOTS5_TRACE_DELETE_PARENT_SYNC)
        sync_failed = 1;
    else {
    do { fd = fsync(vfs->db_dir_fd); } while (fd != 0 && errno == EINTR);
    if (fd != 0) sync_failed = 1;
    }
    if (sync_failed) return SQLITE_IOERR_DIR_FSYNC;
    if (close_failed) return SQLITE_IOERR_CLOSE;
    return SQLITE_OK;
}

static int bots_xaccess(sqlite3_vfs *base, const char *name, int flags, int *result) {
    BotsVfs *vfs = (BotsVfs *)base;
    struct stat st;
    char *leaf;
    (void)flags;
    if (!owner_ok(vfs)) return SQLITE_IOERR_ACCESS;
    if (strcmp(name, vfs->synthetic) == 0) {
        *result = 1;
        return SQLITE_OK;
    }
    leaf = physical_aux_leaf(vfs, name);
    if (leaf == NULL) {
        *result = 0;
        return SQLITE_OK;
    }
    if (fstatat(vfs->db_dir_fd, leaf, &st, AT_SYMLINK_NOFOLLOW) == 0) {
        sqlite3_free(leaf);
        *result = 1;
        return SQLITE_OK;
    }
    sqlite3_free(leaf);
    if (errno == ENOENT) {
        *result = 0;
        return SQLITE_OK;
    }
    return SQLITE_IOERR_ACCESS;
}

static int bots_xfullpath(sqlite3_vfs *base, const char *name, int size, char *out) {
    BotsVfs *vfs = (BotsVfs *)base;
    if (!owner_ok(vfs) || name == NULL || strcmp(name, vfs->synthetic) != 0)
        return SQLITE_CANTOPEN;
    if ((int)strlen(vfs->synthetic) + 1 > size) return SQLITE_CANTOPEN;
    memcpy(out, vfs->synthetic, strlen(vfs->synthetic) + 1);
    return SQLITE_OK;
}

static void *bots_xdlopen(sqlite3_vfs *base, const char *name) {
    (void)base; (void)name; return NULL;
}
static void bots_xdlerror(sqlite3_vfs *base, int n, char *out) {
    (void)base; snprintf(out, (size_t)n, "extension loading is disabled by B.O.T.S.");
}
static void (*bots_xdlsym(sqlite3_vfs *base, void *h, const char *s))(void) {
    (void)base; (void)h; (void)s; return NULL;
}
static void bots_xdlclose(sqlite3_vfs *base, void *h) { (void)base; (void)h; }

static BotsVfs *find_vfs(const char *name) {
    BotsVfs *item;
    for (item = g_vfs_list; item != NULL; item = item->next)
        if (strcmp(item->name, name) == 0) return item;
    return NULL;
}

int bots5_vfs_test_reset_trace(const char *name) {
    BotsVfs *vfs;
    pthread_mutex_lock(&g_vfs_mutex);
    vfs = find_vfs(name);
    if (vfs == NULL || !owner_ok(vfs)) {
        pthread_mutex_unlock(&g_vfs_mutex);
        return set_error("unknown rooted VFS for trace reset");
    }
    pthread_mutex_lock(&vfs->mutex);
    vfs->trace_count = 0;
    vfs->test_io_fault_event = 0;
    pthread_mutex_unlock(&vfs->mutex);
    pthread_mutex_unlock(&g_vfs_mutex);
    return 0;
}

int bots5_vfs_test_trace(const char *name, int *events, int capacity) {
    BotsVfs *vfs;
    int count;
    if (events == NULL || capacity < 0)
        return set_error("invalid rooted VFS trace buffer");
    pthread_mutex_lock(&g_vfs_mutex);
    vfs = find_vfs(name);
    if (vfs == NULL || !owner_ok(vfs)) {
        pthread_mutex_unlock(&g_vfs_mutex);
        return set_error("unknown rooted VFS for trace read");
    }
    pthread_mutex_lock(&vfs->mutex);
    count = vfs->trace_count < capacity ? vfs->trace_count : capacity;
    if (count > 0) memcpy(events, vfs->trace, (size_t)count * sizeof(int));
    pthread_mutex_unlock(&vfs->mutex);
    pthread_mutex_unlock(&g_vfs_mutex);
    return count;
}

int bots5_vfs_test_inject_io_fault(const char *name, int event) {
    BotsVfs *vfs;
    if (event != 0 && event != BOTS5_TRACE_CREATE_PARENT_SYNC &&
        event != BOTS5_TRACE_DELETE_PARENT_SYNC &&
        event != BOTS5_TRACE_DELETE_PROOF_CLOSE)
        return set_error("invalid rooted VFS I/O fault");
    pthread_mutex_lock(&g_vfs_mutex);
    vfs = find_vfs(name);
    if (vfs == NULL || !owner_ok(vfs)) {
        pthread_mutex_unlock(&g_vfs_mutex);
        return set_error("unknown rooted VFS for I/O fault");
    }
    vfs->test_io_fault_event = event;
    pthread_mutex_unlock(&g_vfs_mutex);
    return 0;
}

int bots5_vfs_test_delete_journal(const char *name) {
    BotsVfs *vfs;
    char *synthetic_journal;
    int result;
    pthread_mutex_lock(&g_vfs_mutex);
    vfs = find_vfs(name);
    if (vfs == NULL || !owner_ok(vfs)) {
        pthread_mutex_unlock(&g_vfs_mutex);
        return set_error("unknown rooted VFS for journal delete");
    }
    synthetic_journal = sqlite3_mprintf("%s-journal", vfs->synthetic);
    pthread_mutex_unlock(&g_vfs_mutex);
    if (synthetic_journal == NULL) return SQLITE_NOMEM;
    result = bots_xdelete(&vfs->base, synthetic_journal, 0);
    sqlite3_free(synthetic_journal);
    return result;
}

int bots5_vfs_test_inject_untracked_close_fault(const char *name, int site,
                                                 int mode) {
    BotsVfs *vfs;
    if (site < BOTS5_CLEANUP_XOPEN_REJECT || site > BOTS5_CLEANUP_TEMP ||
        mode < 1 || mode > 2)
        return set_error("invalid untracked-close fault");
    pthread_mutex_lock(&g_vfs_mutex);
    vfs = find_vfs(name);
    if (vfs == NULL || !owner_ok(vfs)) {
        pthread_mutex_unlock(&g_vfs_mutex);
        return set_error("unknown rooted VFS for untracked-close fault");
    }
    vfs->test_cleanup_close_site = site;
    vfs->test_cleanup_close_mode = mode;
    pthread_mutex_unlock(&g_vfs_mutex);
    return 0;
}

int bots5_vfs_test_last_untracked_fd(const char *name) {
    BotsVfs *vfs;
    int fd;
    pthread_mutex_lock(&g_vfs_mutex);
    vfs = find_vfs(name);
    if (vfs == NULL || !owner_ok(vfs)) {
        pthread_mutex_unlock(&g_vfs_mutex);
        return set_error("unknown rooted VFS for last untracked descriptor");
    }
    fd = vfs->test_last_cleanup_fd;
    pthread_mutex_unlock(&g_vfs_mutex);
    if (fd < 0) return set_error("no untracked descriptor has been closed");
    return fd;
}

int bots5_vfs_test_open_cleanup_path(const char *name, int site) {
    BotsVfs *vfs;
    BotsFile file;
    char *opened_name = NULL;
    int flags;
    int result;
    pthread_mutex_lock(&g_vfs_mutex);
    vfs = find_vfs(name);
    if (vfs == NULL || !owner_ok(vfs)) {
        pthread_mutex_unlock(&g_vfs_mutex);
        return set_error("unknown rooted VFS for cleanup-path open");
    }
    if (site == BOTS5_CLEANUP_XOPEN_REJECT) {
        opened_name = sqlite3_mprintf("%s", vfs->synthetic);
        flags = SQLITE_OPEN_MAIN_DB | SQLITE_OPEN_READWRITE;
    } else if (site == BOTS5_CLEANUP_AUXILIARY) {
        opened_name = sqlite3_mprintf("%s-journal", vfs->synthetic);
        flags = SQLITE_OPEN_MAIN_JOURNAL | SQLITE_OPEN_READWRITE |
                SQLITE_OPEN_CREATE;
    } else if (site == BOTS5_CLEANUP_TEMP) {
        flags = SQLITE_OPEN_TEMP_DB | SQLITE_OPEN_READWRITE |
                SQLITE_OPEN_CREATE | SQLITE_OPEN_DELETEONCLOSE;
    } else {
        pthread_mutex_unlock(&g_vfs_mutex);
        return set_error("unsupported cleanup-path open");
    }
    pthread_mutex_unlock(&g_vfs_mutex);
    if (site != BOTS5_CLEANUP_TEMP && opened_name == NULL)
        return SQLITE_NOMEM;
    result = bots_xopen(
        &vfs->base,
        opened_name,
        (sqlite3_file *)&file,
        flags,
        NULL
    );
    sqlite3_free(opened_name);
    if (result == SQLITE_OK) {
        (void)bots_close((sqlite3_file *)&file);
        return set_error("cleanup-path open unexpectedly succeeded");
    }
    return result;
}

int bots5_vfs_test_inject_registration_cleanup_fault(int slot, int mode) {
    if (slot < 1 || slot > 3 || mode < 1 || mode > 2)
        return set_error("invalid registration-cleanup fault");
    pthread_mutex_lock(&g_vfs_mutex);
    if (g_test_registration_cleanup_slot != 0) {
        pthread_mutex_unlock(&g_vfs_mutex);
        return set_error("registration-cleanup fault is already armed");
    }
    g_test_registration_cleanup_slot = slot;
    g_test_registration_cleanup_mode = mode;
    pthread_mutex_unlock(&g_vfs_mutex);
    return 0;
}

int bots5_vfs_register(const char *name, const char *synthetic,
                       int db_dir_fd, int main_claim_fd, int temp_dir_fd,
                       const char *main_leaf, const char *journal_leaf,
                       unsigned long long mount_id, int owner_pid,
                       int intake_wal) {
    BotsVfs *vfs;
    sqlite3_vfs *delegate;
    int force_cleanup_unwind = 0;
    int mutex_initialized = 0;
    int cleanup_unknown = 0;
    if (!component_ok(name) || !component_ok(synthetic) ||
        !component_ok(main_leaf) || !component_ok(journal_leaf))
        return set_error("invalid rooted VFS name");
    if (owner_pid != getpid()) return set_error("rooted VFS owner PID mismatch");
    delegate = sqlite3_vfs_find(NULL);
    if (delegate == NULL) return set_error("default SQLite VFS is unavailable");
    pthread_mutex_lock(&g_vfs_mutex);
    if (find_vfs(name) != NULL) {
        pthread_mutex_unlock(&g_vfs_mutex);
        return set_error("rooted VFS name already registered");
    }
    vfs = calloc(1, sizeof(*vfs));
    if (vfs == NULL) { pthread_mutex_unlock(&g_vfs_mutex); return set_error("out of memory"); }
    vfs->db_dir_fd = -1;
    vfs->main_claim_fd = -1;
    vfs->temp_dir_fd = -1;
    vfs->test_last_cleanup_fd = -1;
    atomic_init(&vfs->unknown_close_generation, 0U);
    vfs->owner_pid = owner_pid;
    if (g_test_registration_cleanup_slot != 0) {
        vfs->test_cleanup_close_site =
            BOTS5_CLEANUP_REGISTER_DB_DIR +
            (g_test_registration_cleanup_slot - 1);
        vfs->test_cleanup_close_mode = g_test_registration_cleanup_mode;
        g_test_registration_cleanup_slot = 0;
        g_test_registration_cleanup_mode = 0;
        force_cleanup_unwind = 1;
    }
    vfs->name = strdup(name);
    vfs->synthetic = strdup(synthetic);
    vfs->main_leaf = strdup(main_leaf);
    vfs->journal_leaf = strdup(journal_leaf);
    vfs->db_dir_fd = cloexec_dup(db_dir_fd);
    vfs->main_claim_fd = cloexec_dup(main_claim_fd);
    vfs->temp_dir_fd = cloexec_dup(temp_dir_fd);
    if (!vfs->name || !vfs->synthetic || !vfs->main_leaf || !vfs->journal_leaf ||
        vfs->db_dir_fd < 0 || vfs->main_claim_fd < 0 || vfs->temp_dir_fd < 0) {
        pthread_mutex_unlock(&g_vfs_mutex);
        set_error("cannot duplicate rooted VFS capabilities");
        goto fail;
    }
    if (force_cleanup_unwind) {
        pthread_mutex_unlock(&g_vfs_mutex);
        set_error("injected rooted VFS registration unwind");
        goto fail;
    }
    vfs->delegate = delegate;
    vfs->mount_id = mount_id;
    vfs->euid = geteuid();
    vfs->owner_pid = owner_pid;
    vfs->intake_wal = intake_wal != 0;
    pthread_mutex_init(&vfs->mutex, NULL);
    mutex_initialized = 1;
    vfs->base.iVersion = 3;
    vfs->base.szOsFile = sizeof(BotsFile);
    vfs->base.mxPathname = 255;
    vfs->base.zName = vfs->name;
    vfs->base.pAppData = vfs;
    vfs->base.xOpen = bots_xopen;
    vfs->base.xDelete = bots_xdelete;
    vfs->base.xAccess = bots_xaccess;
    vfs->base.xFullPathname = bots_xfullpath;
    vfs->base.xDlOpen = bots_xdlopen;
    vfs->base.xDlError = bots_xdlerror;
    vfs->base.xDlSym = bots_xdlsym;
    vfs->base.xDlClose = bots_xdlclose;
    vfs->base.xRandomness = delegate->xRandomness;
    vfs->base.xSleep = delegate->xSleep;
    vfs->base.xCurrentTime = delegate->xCurrentTime;
    vfs->base.xGetLastError = delegate->xGetLastError;
    vfs->base.xCurrentTimeInt64 = delegate->iVersion >= 2 ? delegate->xCurrentTimeInt64 : NULL;
    vfs->base.xSetSystemCall = delegate->iVersion >= 3 ? delegate->xSetSystemCall : NULL;
    vfs->base.xGetSystemCall = delegate->iVersion >= 3 ? delegate->xGetSystemCall : NULL;
    vfs->base.xNextSystemCall = delegate->iVersion >= 3 ? delegate->xNextSystemCall : NULL;
    if (sqlite3_vfs_register(&vfs->base, 0) != SQLITE_OK) {
        pthread_mutex_unlock(&g_vfs_mutex);
        set_error("sqlite3_vfs_register failed");
        goto fail;
    }
    vfs->registered = 1;
    vfs->next = g_vfs_list;
    g_vfs_list = vfs;
    pthread_mutex_unlock(&g_vfs_mutex);
    return 0;
fail:
    if (vfs) {
        (void)close_untracked_fd(
            vfs, &vfs->db_dir_fd, BOTS5_CLEANUP_REGISTER_DB_DIR
        );
        (void)close_untracked_fd(
            vfs, &vfs->main_claim_fd, BOTS5_CLEANUP_REGISTER_MAIN
        );
        (void)close_untracked_fd(
            vfs, &vfs->temp_dir_fd, BOTS5_CLEANUP_REGISTER_TEMP
        );
        cleanup_unknown = atomic_load_explicit(
            &vfs->unknown_close_generation, memory_order_acquire
        ) != 0U;
        if (mutex_initialized) pthread_mutex_destroy(&vfs->mutex);
        free(vfs->name); free(vfs->synthetic); free(vfs->main_leaf); free(vfs->journal_leaf); free(vfs);
    }
    if (cleanup_unknown) {
        set_error("rooted VFS registration cleanup incomplete");
        return -2;
    }
    return -1;
}

int bots5_vfs_open_count(const char *name) {
    BotsVfs *vfs;
    int count = -1;
    pthread_mutex_lock(&g_vfs_mutex);
    vfs = find_vfs(name);
    if (vfs) {
        pthread_mutex_lock(&vfs->mutex);
        count = vfs->open_count;
        pthread_mutex_unlock(&vfs->mutex);
    }
    pthread_mutex_unlock(&g_vfs_mutex);
    return count;
}

unsigned int bots5_vfs_unknown_close_generation(const char *name) {
    BotsVfs *vfs;
    unsigned int generation = UINT_MAX;
    pthread_mutex_lock(&g_vfs_mutex);
    vfs = find_vfs(name);
    if (vfs != NULL && owner_ok(vfs)) {
        generation = atomic_load_explicit(
            &vfs->unknown_close_generation, memory_order_acquire
        );
    }
    pthread_mutex_unlock(&g_vfs_mutex);
    if (generation == UINT_MAX) {
        (void)set_error("unknown rooted VFS for close outcome");
    }
    return generation;
}

unsigned int bots5_vfs_thread_unknown_close_generation(const char *name) {
    BotsVfs *vfs;
    unsigned int generation = UINT_MAX;
    unsigned int i;
    pthread_mutex_lock(&g_vfs_mutex);
    vfs = find_vfs(name);
    if (vfs != NULL && owner_ok(vfs)) {
        generation = 0U;
        for (i = 0U; i < BOTS5_THREAD_VFS_SLOTS; i++) {
            if (g_thread_unknown_close[i].name[0] != '\0' &&
                strcmp(g_thread_unknown_close[i].name, name) == 0) {
                generation = g_thread_unknown_close[i].generation;
                break;
            }
        }
    }
    pthread_mutex_unlock(&g_vfs_mutex);
    if (generation == UINT_MAX) {
        (void)set_error("unknown rooted VFS for thread close outcome");
    }
    return generation;
}

int bots5_vfs_test_private_fd(const char *name, int slot) {
    BotsVfs *vfs;
    int fd = -1;
    pthread_mutex_lock(&g_vfs_mutex);
    vfs = find_vfs(name);
    if (vfs != NULL && owner_ok(vfs)) {
        if (slot == 1) fd = vfs->db_dir_fd;
        else if (slot == 2) fd = vfs->main_claim_fd;
        else if (slot == 3) fd = vfs->temp_dir_fd;
    }
    pthread_mutex_unlock(&g_vfs_mutex);
    if (fd < 0) return set_error("unknown rooted VFS private descriptor");
    return fd;
}

int bots5_vfs_test_inject_close_fault(const char *name, int slot, int mode) {
    BotsVfs *vfs;
    if (slot < 1 || slot > 3 || mode < 0 || mode > 2)
        return set_error("invalid rooted VFS close fault");
    pthread_mutex_lock(&g_vfs_mutex);
    vfs = find_vfs(name);
    if (vfs == NULL || !owner_ok(vfs)) {
        pthread_mutex_unlock(&g_vfs_mutex);
        return set_error("unknown rooted VFS for close fault");
    }
    vfs->test_close_fault_slot = slot;
    vfs->test_close_fault_mode = mode;
    pthread_mutex_unlock(&g_vfs_mutex);
    return 0;
}

static int close_private_fd(BotsVfs *vfs, int fd, int slot) {
    int result;
    if (vfs->test_close_fault_slot == slot && vfs->test_close_fault_mode == 1) {
        errno = EIO;
        return -1;
    }
    result = close(fd);
    if (vfs->test_close_fault_slot == slot && vfs->test_close_fault_mode == 2) {
        errno = EINTR;
        return -1;
    }
    return result;
}

int bots5_vfs_unregister(const char *name, int *db_dir_status,
                         int *main_claim_status, int *temp_dir_status,
                         int *native_open_status) {
    BotsVfs **cursor;
    BotsVfs *vfs;
    int db_dir_fd;
    int main_claim_fd;
    int temp_dir_fd;
    int failed = 0;
    if (db_dir_status == NULL || main_claim_status == NULL ||
        temp_dir_status == NULL || native_open_status == NULL)
        return set_error("rooted VFS close inventory is required");
    *db_dir_status = BOTS5_CLOSE_HELD;
    *main_claim_status = BOTS5_CLOSE_HELD;
    *temp_dir_status = BOTS5_CLOSE_HELD;
    *native_open_status = BOTS5_CLOSE_HELD;
    pthread_mutex_lock(&g_vfs_mutex);
    cursor = &g_vfs_list;
    while (*cursor != NULL && strcmp((*cursor)->name, name) != 0) cursor = &(*cursor)->next;
    if (*cursor == NULL) { pthread_mutex_unlock(&g_vfs_mutex); return set_error("unknown rooted VFS"); }
    vfs = *cursor;
    pthread_mutex_lock(&vfs->mutex);
    if (vfs->open_count != 0) {
        pthread_mutex_unlock(&vfs->mutex);
        pthread_mutex_unlock(&g_vfs_mutex);
        return set_error("rooted VFS still has open files");
    }
    pthread_mutex_unlock(&vfs->mutex);
    if (sqlite3_vfs_unregister(&vfs->base) != SQLITE_OK) {
        pthread_mutex_unlock(&g_vfs_mutex);
        return set_error("sqlite3_vfs_unregister failed");
    }
    *cursor = vfs->next;
    vfs->registered = 0;
    db_dir_fd = vfs->db_dir_fd;
    main_claim_fd = vfs->main_claim_fd;
    temp_dir_fd = vfs->temp_dir_fd;
    vfs->db_dir_fd = -1;
    vfs->main_claim_fd = -1;
    vfs->temp_dir_fd = -1;
    pthread_mutex_unlock(&g_vfs_mutex);
    if (close_private_fd(vfs, db_dir_fd, 1) == 0) {
        *db_dir_status = BOTS5_CLOSE_RELEASED;
    } else {
        *db_dir_status = BOTS5_CLOSE_UNKNOWN;
        failed = 1;
    }
    if (close_private_fd(vfs, main_claim_fd, 2) == 0) {
        *main_claim_status = BOTS5_CLOSE_RELEASED;
    } else {
        *main_claim_status = BOTS5_CLOSE_UNKNOWN;
        failed = 1;
    }
    if (close_private_fd(vfs, temp_dir_fd, 3) == 0) {
        *temp_dir_status = BOTS5_CLOSE_RELEASED;
    } else {
        *temp_dir_status = BOTS5_CLOSE_UNKNOWN;
        failed = 1;
    }
    if (atomic_load_explicit(
            &vfs->unknown_close_generation, memory_order_acquire
        ) != 0U) {
        *native_open_status = BOTS5_CLOSE_UNKNOWN;
        failed = 1;
    } else {
        *native_open_status = BOTS5_CLOSE_RELEASED;
    }
    pthread_mutex_destroy(&vfs->mutex);
    free(vfs->name); free(vfs->synthetic); free(vfs->main_leaf); free(vfs->journal_leaf); free(vfs);
    if (failed) {
        snprintf(
            g_error,
            sizeof(g_error),
            "rooted VFS private close incomplete: database-directory=%s, main-claim=%s, temp-directory=%s, native-open-files=%s",
            *db_dir_status == BOTS5_CLOSE_RELEASED ? "RELEASED" : "UNKNOWN",
            *main_claim_status == BOTS5_CLOSE_RELEASED ? "RELEASED" : "UNKNOWN",
            *temp_dir_status == BOTS5_CLOSE_RELEASED ? "RELEASED" : "UNKNOWN",
            *native_open_status == BOTS5_CLOSE_RELEASED ? "RELEASED" : "UNKNOWN"
        );
        return -1;
    }
    return 0;
}

void bots5_vfs_after_fork_child(void) {
    BotsVfs *vfs;
    for (vfs = g_vfs_list; vfs != NULL; vfs = vfs->next) {
        BotsFile *file;
        if (vfs->owner_pid == getpid()) continue;
        file = vfs->files;
        while (file != NULL) {
            BotsFile *next = file->next_open;
            if (file->fd >= 0) { close(file->fd); file->fd = -1; }
            if (file->shm != NULL && file->shm_size != 0) {
                (void)munmap(file->shm, file->shm_size);
                file->shm = NULL;
                file->shm_size = 0;
            }
            file->owner = NULL;
            file->next_open = NULL;
            file = next;
        }
        vfs->files = NULL;
        vfs->open_count = 0;
        if (vfs->db_dir_fd >= 0) { close(vfs->db_dir_fd); vfs->db_dir_fd = -1; }
        if (vfs->main_claim_fd >= 0) { close(vfs->main_claim_fd); vfs->main_claim_fd = -1; }
        if (vfs->temp_dir_fd >= 0) { close(vfs->temp_dir_fd); vfs->temp_dir_fd = -1; }
        vfs->registered = 0;
    }
}
