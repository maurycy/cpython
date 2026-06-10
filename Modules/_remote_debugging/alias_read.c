#include "_remote_debugging.h"

#if defined(__APPLE__) && TARGET_OS_OSX

#ifndef VM_FLAGS_RETURN_DATA_ADDR
#  define VM_FLAGS_RETURN_DATA_ADDR 0
#endif

static int
alias_query_region(task_t task,
                   mach_vm_address_t addr,
                   uint64_t *objid,
                   uint64_t *offset)
{
    mach_vm_address_t region_addr = addr;
    mach_vm_size_t region_size = 0;
    natural_t depth = 0;
    vm_region_submap_info_data_64_t info = {0};
    mach_msg_type_number_t count = VM_REGION_SUBMAP_INFO_COUNT_64;

    kern_return_t kr = mach_vm_region_recurse(
        task, &region_addr, &region_size, &depth,
        (vm_region_recurse_info_t)&info, &count);
    if (kr == MACH_SEND_INVALID_DEST || kr == KERN_INVALID_ARGUMENT) {
        return -1;
    }
    if (kr != KERN_SUCCESS || addr < region_addr ||
        (mach_vm_size_t)(addr - region_addr) >= region_size ||
        info.is_submap)
    {
        return 1;
    }

    *objid = info.object_id_full;
    *offset = info.offset + (uint64_t)(addr - region_addr);
    return 0;
}

static void
alias_invalidate_entry(AliasPageEntry *entry)
{
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
        alias_invalidate_entry(&cache->pages[i]);
    }
    if (cache->region_base != 0) {
        mach_vm_size_t region_size =
            (mach_vm_size_t)MAX_ALIAS_PAGES *
            (mach_vm_size_t)unwinder->handle.page_size;
        (void)mach_vm_deallocate(mach_task_self(), cache->region_base,
                                 region_size);
        cache->region_base = 0;
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
    AliasPageEntry *entry;
    while ((entry = alias_find_entry(unwinder, page_base)) != NULL) {
        alias_invalidate_entry(entry);
    }
}

void
_Py_RemoteDebug_AliasCacheInit(RemoteUnwinderObject *unwinder)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    memset(cache, 0, sizeof(*cache));
    cache->probe_mask = ALIAS_PROBE_DEFAULT_MASK;

    mach_vm_size_t region_size =
        (mach_vm_size_t)MAX_ALIAS_PAGES *
        (mach_vm_size_t)unwinder->handle.page_size;
    kern_return_t kr = mach_vm_allocate(mach_task_self(),
                                        &cache->region_base,
                                        region_size,
                                        VM_FLAGS_ANYWHERE);
    if (kr != KERN_SUCCESS) {
        cache->disabled = 1;
        return;
    }
    kr = mach_vm_protect(mach_task_self(), cache->region_base, region_size,
                         FALSE, VM_PROT_NONE);
    if (kr != KERN_SUCCESS) {
        (void)mach_vm_deallocate(mach_task_self(), cache->region_base,
                                 region_size);
        cache->region_base = 0;
        cache->disabled = 1;
        return;
    }
}

static int
alias_maybe_probe_entry(RemoteUnwinderObject *unwinder,
                        AliasPageEntry *entry)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    if ((++cache->probe_counter & cache->probe_mask) != 0) {
        return 1;
    }
    STATS_INC(unwinder, alias_probe_checks);

    uint64_t objid = 0;
    uint64_t offset = 0;
    int rc = alias_query_region(unwinder->handle.task,
                                entry->remote_page_base,
                                &objid, &offset);
    if (rc < 0) {
        STATS_INC(unwinder, alias_identity_mismatches);
        alias_disable_runtime(unwinder);
        return -1;
    }
    if (rc == 0 && objid == entry->map_objid &&
        offset == entry->map_offset)
    {
        if (cache->probe_mask < ALIAS_PROBE_MAX_MASK) {
            cache->probe_mask = (cache->probe_mask << 1) | 1;
        }
        return 1;
    }
    STATS_INC(unwinder, alias_probe_recycles);
    if (cache->probe_mask > ALIAS_PROBE_MIN_MASK) {
        cache->probe_mask >>= 1;
    }
    alias_invalidate_entry(entry);
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
    alias_invalidate_entry(oldest);
    STATS_INC(unwinder, alias_evictions);
    return oldest;
}

static kern_return_t
alias_map_readonly_page(RemoteUnwinderObject *unwinder,
                        uintptr_t page_base,
                        AliasPageEntry *entry)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    mach_vm_size_t page_size = (mach_vm_size_t)unwinder->handle.page_size;
    mach_vm_address_t local_addr =
        cache->region_base + (entry - cache->pages) * page_size;
    vm_prot_t cur_protection = VM_PROT_NONE;
    vm_prot_t max_protection = VM_PROT_NONE;

    kern_return_t kr = mach_vm_remap(
        mach_task_self(),
        &local_addr,
        page_size,
        0,
        VM_FLAGS_FIXED | VM_FLAGS_OVERWRITE | VM_FLAGS_RETURN_DATA_ADDR,
        unwinder->handle.task,
        (mach_vm_address_t)page_base,
        FALSE,
        &cur_protection,
        &max_protection,
        VM_INHERIT_NONE);
    if (kr != KERN_SUCCESS) {
        return kr;
    }
    if ((cur_protection & VM_PROT_READ) == 0) {
        return KERN_PROTECTION_FAILURE;
    }
    kr = mach_vm_protect(mach_task_self(), local_addr, page_size, FALSE,
                         VM_PROT_READ);
    if (kr != KERN_SUCCESS) {
        return kr;
    }
    entry->local_page_base = local_addr;
    return KERN_SUCCESS;
}

static int
alias_remap_page(RemoteUnwinderObject *unwinder,
                 uintptr_t page_base,
                 AliasPageEntry **entry_out)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    AliasPageEntry *entry = alias_alloc_entry(unwinder);
    kern_return_t kr = alias_map_readonly_page(unwinder, page_base, entry);
    if (kr != KERN_SUCCESS) {
        alias_invalidate_entry(entry);
        STATS_INC(unwinder, alias_remap_failures);
        if (kr == MACH_SEND_INVALID_DEST || kr == KERN_INVALID_ARGUMENT) {
            STATS_INC(unwinder, alias_identity_mismatches);
            alias_disable_runtime(unwinder);
        }
        else if (kr == KERN_NOT_SUPPORTED) {
            alias_disable_runtime(unwinder);
        }
        return -1;
    }

    entry->remote_page_base = page_base;
    entry->access_seq = ++cache->access_seq;
    entry->valid = 1;

    if (alias_query_region(mach_task_self(), entry->local_page_base,
                           &entry->map_objid, &entry->map_offset) != 0)
    {
        alias_invalidate_entry(entry);
        STATS_INC(unwinder, alias_remap_failures);
        return -1;
    }

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
        int probe = alias_maybe_probe_entry(unwinder, entry);
        if (probe < 0) {
            return _Py_RemoteDebug_ReadRemoteMemory(
                &unwinder->handle, remote_addr, len, dst);
        }
        if (probe == 0) {
            PyErr_SetString(PyExc_RuntimeError,
                            "alias page recycled under sample");
            return -1;
        }
        if (probe > 0) {
            entry->access_seq = ++cache->access_seq;
            memcpy(dst, (const char *)entry->local_page_base + offset, len);
            STATS_INC(unwinder, alias_hits);
            return 0;
        }
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
