#include "_remote_debugging.h"

#if defined(__APPLE__) && TARGET_OS_OSX

#ifndef VM_FLAGS_RETURN_DATA_ADDR
#  define VM_FLAGS_RETURN_DATA_ADDR 0
#endif

static int
read_target_identity(RemoteUnwinderObject *unwinder,
                     uint64_t *start_tvsec)
{
    struct proc_bsdinfo info;
    int n = proc_pidinfo(unwinder->handle.pid, PROC_PIDTBSDINFO, 0,
                         &info, sizeof(info));
    if (n != (int)sizeof(info)) {
        return -1;
    }
    if (info.pbi_start_tvsec == 0) {
        return -1;
    }
    *start_tvsec = (uint64_t)info.pbi_start_tvsec;
    return 0;
}

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

static size_t
alias_set_index(RemoteUnwinderObject *unwinder, uintptr_t page_base)
{
    size_t page_size = (size_t)unwinder->handle.page_size;
    uintptr_t page_index = page_base >> __builtin_ctzll(page_size);
    return (size_t)(page_index & (ALIAS_CACHE_SETS - 1));
}

static AliasPageEntry *
alias_find_entry(RemoteUnwinderObject *unwinder, uintptr_t page_base)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    AliasPageEntry *ways = cache->pages[alias_set_index(unwinder, page_base)];
    for (int w = 0; w < ALIAS_CACHE_WAYS; w++) {
        if (ways[w].valid && ways[w].remote_page_base == page_base) {
            return &ways[w];
        }
    }
    return NULL;
}

void
_Py_RemoteDebug_AliasCacheClear(RemoteUnwinderObject *unwinder)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    for (size_t s = 0; s < ALIAS_CACHE_SETS; s++) {
        for (int w = 0; w < ALIAS_CACHE_WAYS; w++) {
            alias_deallocate_entry(&cache->pages[s][w]);
        }
    }
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
    AliasPageEntry *entry = alias_find_entry(unwinder, page_base);
    if (entry != NULL) {
        alias_deallocate_entry(entry);
    }
}

void
_Py_RemoteDebug_AliasCacheInit(RemoteUnwinderObject *unwinder)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    memset(cache, 0, sizeof(*cache));

    uint64_t start_tvsec = 0;
    if (read_target_identity(unwinder, &start_tvsec) < 0) {
        cache->disabled = 1;
    }
    else {
        cache->target_start_tvsec = start_tvsec;
    }
}

static int
alias_identity_matches(RemoteUnwinderObject *unwinder)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    uint64_t start_tvsec = 0;
    if (read_target_identity(unwinder, &start_tvsec) < 0) {
        return 0;
    }
    return start_tvsec == cache->target_start_tvsec;
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
alias_alloc_entry(RemoteUnwinderObject *unwinder, uintptr_t page_base)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    size_t set = alias_set_index(unwinder, page_base);
    AliasPageEntry *ways = cache->pages[set];

    for (int w = 0; w < ALIAS_CACHE_WAYS; w++) {
        if (!ways[w].valid) {
            return &ways[w];
        }
    }

    uint8_t victim = cache->victim[set];
    cache->victim[set] = (uint8_t)((victim + 1) % ALIAS_CACHE_WAYS);
    alias_deallocate_entry(&ways[victim]);
    STATS_INC(unwinder, alias_evictions);
    return &ways[victim];
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
    mach_vm_address_t local_addr = 0;
    if (alias_map_readonly_page(unwinder, page_base, &local_addr) < 0) {
        STATS_INC(unwinder, alias_remap_failures);
        alias_disable_runtime(unwinder);
        return -1;
    }

    AliasPageEntry *entry = alias_alloc_entry(unwinder, page_base);
    entry->remote_page_base = page_base;
    entry->local_page_base = local_addr;
    entry->size = (mach_vm_size_t)unwinder->handle.page_size;
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
