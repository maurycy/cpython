#include "_remote_debugging.h"

#if defined(__APPLE__) && TARGET_OS_OSX

#include <mach/notify.h>

#ifndef VM_FLAGS_RETURN_DATA_ADDR
#  define VM_FLAGS_RETURN_DATA_ADDR 0
#endif

static void
alias_deallocate_entry(AliasPageEntry *entry)
{
    if (!entry->valid) {
        return;
    }
    (void)mach_vm_deallocate(mach_task_self(), entry->local_page_base,
                             entry->size);
    memset(entry, 0, sizeof(*entry));
}

static AliasPageEntry *
alias_find_entry(RemoteUnwinderObject *unwinder, uintptr_t page_base)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    for (int i = 0; i < MAX_ALIAS_PAGES; i++) {
        AliasPageEntry *entry = &cache->pages[i];
        if (entry->valid && entry->remote_page_base == page_base) {
            return entry;
        }
    }
    return NULL;
}

void
_Py_RemoteDebug_AliasCacheClear(RemoteUnwinderObject *unwinder)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    for (int i = 0; i < MAX_ALIAS_PAGES; i++) {
        alias_deallocate_entry(&cache->pages[i]);
    }
    if (cache->notify_port != MACH_PORT_NULL) {
        (void)mach_port_mod_refs(mach_task_self(), cache->notify_port,
                                 MACH_PORT_RIGHT_RECEIVE, -1);
        cache->notify_port = MACH_PORT_NULL;
    }
    cache->notify_armed = 0;
}

static void
alias_disable_runtime(RemoteUnwinderObject *unwinder)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    cache->disabled = 1;
    _Py_RemoteDebug_AliasCacheClear(unwinder);
}

void
_Py_RemoteDebug_AliasCacheInvalidatePage(RemoteUnwinderObject *unwinder,
                                         uintptr_t remote_addr)
{
    size_t page_size = (size_t)unwinder->handle.page_size;
    uintptr_t page_base = remote_addr & ~(uintptr_t)(page_size - 1);
    AliasPageEntry *entry;
    while ((entry = alias_find_entry(unwinder, page_base)) != NULL) {
        alias_deallocate_entry(entry);
    }
}

void
_Py_RemoteDebug_AliasCacheInit(RemoteUnwinderObject *unwinder)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    memset(cache, 0, sizeof(*cache));

    mach_port_t notify_port = MACH_PORT_NULL;
    kern_return_t kr = mach_port_allocate(mach_task_self(),
                                          MACH_PORT_RIGHT_RECEIVE,
                                          &notify_port);
    if (kr != KERN_SUCCESS) {
        return;
    }

    mach_port_t previous = MACH_PORT_NULL;
    kr = mach_port_request_notification(mach_task_self(),
                                        unwinder->handle.task,
                                        MACH_NOTIFY_DEAD_NAME,
                                        1,
                                        notify_port,
                                        MACH_MSG_TYPE_MAKE_SEND_ONCE,
                                        &previous);
    if (previous != MACH_PORT_NULL) {
        (void)mach_port_deallocate(mach_task_self(), previous);
    }
    if (kr != KERN_SUCCESS) {
        (void)mach_port_mod_refs(mach_task_self(), notify_port,
                                 MACH_PORT_RIGHT_RECEIVE, -1);
        return;
    }

    cache->notify_port = notify_port;
    cache->notify_armed = 1;
}

static int
alias_identity_matches_fallback(RemoteUnwinderObject *unwinder)
{
    mach_port_type_t type = 0;
    kern_return_t kr = mach_port_type(mach_task_self(),
                                      unwinder->handle.task, &type);
    if (kr != KERN_SUCCESS) {
        return 0;
    }
    return ((type & MACH_PORT_TYPE_SEND) &&
            !(type & MACH_PORT_TYPE_DEAD_NAME)) ? 1 : 0;
}

static int
alias_identity_matches(RemoteUnwinderObject *unwinder)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    if (!cache->notify_armed) {
        return alias_identity_matches_fallback(unwinder);
    }

    union {
        mach_dead_name_notification_t dead_name;
        struct {
            mach_msg_header_t header;
            mach_msg_trailer_t trailer;
        } msg;
        char storage[sizeof(mach_dead_name_notification_t) +
                     sizeof(mach_msg_trailer_t)];
    } msg;
    memset(&msg, 0, sizeof(msg));

    mach_msg_return_t mr = mach_msg(&msg.msg.header,
                                    MACH_RCV_MSG | MACH_RCV_TIMEOUT,
                                    0,
                                    (mach_msg_size_t)sizeof(msg),
                                    cache->notify_port,
                                    0,
                                    MACH_PORT_NULL);
    if (mr == MACH_RCV_TIMED_OUT) {
        return 1;
    }
    if (mr == MACH_MSG_SUCCESS &&
        msg.msg.header.msgh_id == MACH_NOTIFY_DEAD_NAME)
    {
        if (msg.dead_name.not_port != MACH_PORT_NULL) {
            (void)mach_port_deallocate(mach_task_self(),
                                       msg.dead_name.not_port);
        }
        return 0;
    }
    return 0;
}

static int
alias_maybe_probe_identity(RemoteUnwinderObject *unwinder)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    if ((++cache->probe_counter & ALIAS_PROBE_MASK) != 0) {
        return 1;
    }
    if (alias_identity_matches(unwinder)) {
        return 1;
    }
    STATS_INC(unwinder, alias_identity_mismatches);
    alias_disable_runtime(unwinder);
    return 0;
}

static AliasPageEntry *
alias_alloc_entry(RemoteUnwinderObject *unwinder)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    AliasPageEntry *oldest = NULL;

    for (int i = 0; i < MAX_ALIAS_PAGES; i++) {
        AliasPageEntry *entry = &cache->pages[i];
        if (!entry->valid) {
            return entry;
        }
        if (oldest == NULL || entry->access_seq < oldest->access_seq) {
            oldest = entry;
        }
    }

    assert(oldest != NULL);
    alias_deallocate_entry(oldest);
    STATS_INC(unwinder, alias_evictions);
    return oldest;
}

static int
alias_map_readonly_page(RemoteUnwinderObject *unwinder,
                        uintptr_t page_base,
                        mach_vm_address_t *local_addr_out)
{
    mach_vm_size_t page_size = (mach_vm_size_t)unwinder->handle.page_size;
    mach_vm_address_t local_addr = 0;
    vm_prot_t cur_protection = VM_PROT_NONE;
    vm_prot_t max_protection = VM_PROT_NONE;

    kern_return_t kr = mach_vm_remap(
        mach_task_self(),
        &local_addr,
        page_size,
        0,
        VM_FLAGS_ANYWHERE | VM_FLAGS_RETURN_DATA_ADDR,
        unwinder->handle.task,
        (mach_vm_address_t)page_base,
        FALSE,
        &cur_protection,
        &max_protection,
        VM_INHERIT_NONE);
    if (kr != KERN_SUCCESS) {
        return -1;
    }
    if ((cur_protection & VM_PROT_READ) == 0) {
        (void)mach_vm_deallocate(mach_task_self(), local_addr, page_size);
        return -1;
    }
    kr = mach_vm_protect(mach_task_self(), local_addr, page_size, FALSE,
                         VM_PROT_READ);
    if (kr != KERN_SUCCESS) {
        (void)mach_vm_deallocate(mach_task_self(), local_addr, page_size);
        return -1;
    }
    *local_addr_out = local_addr;
    return 0;
}

static int
alias_remap_page(RemoteUnwinderObject *unwinder,
                 uintptr_t page_base,
                 AliasPageEntry **entry_out)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    mach_vm_address_t local_addr = 0;
    if (alias_map_readonly_page(unwinder, page_base, &local_addr) < 0) {
        STATS_INC(unwinder, alias_remap_failures);
        alias_disable_runtime(unwinder);
        return -1;
    }

    AliasPageEntry *entry = alias_alloc_entry(unwinder);
    entry->remote_page_base = page_base;
    entry->local_page_base = local_addr;
    entry->size = (mach_vm_size_t)unwinder->handle.page_size;
    entry->access_seq = ++cache->access_seq;
    entry->valid = 1;

    *entry_out = entry;
    return 0;
}

int
_Py_RemoteDebug_AliasedRead(RemoteUnwinderObject *unwinder,
                            uintptr_t remote_addr,
                            size_t len,
                            void *dst)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    if (len == 0) {
        return 0;
    }
    if (cache->disabled) {
        return _Py_RemoteDebug_ReadRemoteMemory(
            &unwinder->handle, remote_addr, len, dst);
    }

    size_t page_size = (size_t)unwinder->handle.page_size;
    uintptr_t page_base = remote_addr & ~(uintptr_t)(page_size - 1);
    size_t offset = (size_t)(remote_addr - page_base);
    if (offset >= page_size || len > page_size - offset) {
        return _Py_RemoteDebug_ReadRemoteMemory(
            &unwinder->handle, remote_addr, len, dst);
    }

    AliasPageEntry *entry = alias_find_entry(unwinder, page_base);
    if (entry != NULL) {
        if (!alias_maybe_probe_identity(unwinder)) {
            return _Py_RemoteDebug_ReadRemoteMemory(
                &unwinder->handle, remote_addr, len, dst);
        }
        entry->access_seq = ++cache->access_seq;
        memcpy(dst, (const char *)entry->local_page_base + offset, len);
        STATS_INC(unwinder, alias_hits);
        return 0;
    }

    STATS_INC(unwinder, alias_misses);
    if (alias_remap_page(unwinder, page_base, &entry) < 0) {
        return _Py_RemoteDebug_ReadRemoteMemory(
            &unwinder->handle, remote_addr, len, dst);
    }
    memcpy(dst, (const char *)entry->local_page_base + offset, len);
    return 0;
}

#endif /* defined(__APPLE__) && TARGET_OS_OSX */
