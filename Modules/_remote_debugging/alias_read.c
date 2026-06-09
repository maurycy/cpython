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

/* ===== E1 instrumentation: ground-truth same-VA recycling via object_id ===== */
#include <mach/mach_vm.h>

static AliasPageEntry *alias_find_entry(RemoteUnwinderObject *unwinder, uintptr_t page_base);

static int
alias_dbg_query_region(RemoteUnwinderObject *unwinder, uintptr_t addr,
                       uint64_t *objid, uint64_t *offset,
                       uint32_t *user_tag, uint8_t *share_mode)
{
    mach_vm_address_t a = (mach_vm_address_t)addr;
    mach_vm_size_t sz = 0;
    natural_t depth = 0;
    struct vm_region_submap_info_64 info;
    mach_msg_type_number_t count = VM_REGION_SUBMAP_INFO_COUNT_64;
    kern_return_t kr = mach_vm_region_recurse(
        unwinder->handle.task, &a, &sz, &depth,
        (vm_region_recurse_info_t)&info, &count);
    if (kr != KERN_SUCCESS) {
        return -1;
    }
    /* a may be > addr if addr is in a hole; require addr inside region */
    if ((mach_vm_address_t)addr < a || (mach_vm_address_t)addr >= a + sz) {
        return -1;
    }
    *objid = (uint64_t)info.object_id_full;
    *offset = (uint64_t)info.offset;
    *user_tag = (uint32_t)info.user_tag;
    *share_mode = (uint8_t)info.share_mode;
    return 0;
}

static void
alias_dbg_record_map(RemoteUnwinderObject *unwinder, AliasPageEntry *entry)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    if (!cache->dbg_enabled) {
        return;
    }
    entry->dbg_owner = -1;
    if (alias_dbg_query_region(unwinder, entry->remote_page_base,
                               &entry->dbg_objid, &entry->dbg_offset,
                               &entry->dbg_user_tag, &entry->dbg_share_mode) < 0) {
        entry->dbg_objid = 0;
        entry->dbg_offset = 0;
    }
    /* E3 immutable baseline: snapshot map-time identity; never updated by
     * check_hit, so FrameCheck detects recycling independently of E1. */
    entry->dbg_map_objid = entry->dbg_objid;
    entry->dbg_map_offset = entry->dbg_offset;
    fprintf(stderr,
        "[aliasdbg] MAP page=0x%llx objid=0x%llx off=0x%llx tag=%u share=%u "
        "is_tstate=%d is_interp=%d\n",
        (unsigned long long)entry->remote_page_base,
        (unsigned long long)entry->dbg_objid,
        (unsigned long long)entry->dbg_offset,
        entry->dbg_user_tag, entry->dbg_share_mode,
        (entry->remote_page_base ==
            (unwinder->tstate_addr & ~((uintptr_t)unwinder->handle.page_size - 1))),
        (entry->remote_page_base ==
            (unwinder->interpreter_addr & ~((uintptr_t)unwinder->handle.page_size - 1))));
}

static void
alias_dbg_check_hit(RemoteUnwinderObject *unwinder, AliasPageEntry *entry)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    if (!cache->dbg_enabled) {
        return;
    }
    uint64_t objid = 0, offset = 0;
    uint32_t tag = 0;
    uint8_t share = 0;
    cache->dbg_checks++;
    if (alias_dbg_query_region(unwinder, entry->remote_page_base,
                               &objid, &offset, &tag, &share) < 0) {
        /* region gone entirely: that is also a recycling/unmap event */
        cache->dbg_recycle_events++;
        fprintf(stderr,
            "[aliasdbg] RECYCLE(unmapped) page=0x%llx old_objid=0x%llx owner=%d\n",
            (unsigned long long)entry->remote_page_base,
            (unsigned long long)entry->dbg_objid, entry->dbg_owner);
        entry->dbg_objid = 0;
        return;
    }
    if (objid != entry->dbg_objid || offset != entry->dbg_offset) {
        cache->dbg_recycle_events++;
        fprintf(stderr,
            "[aliasdbg] RECYCLE page=0x%llx old_objid=0x%llx new_objid=0x%llx "
            "old_off=0x%llx new_off=0x%llx tag=%u share=%u owner=%d "
            "is_tstate=%d is_interp=%d\n",
            (unsigned long long)entry->remote_page_base,
            (unsigned long long)entry->dbg_objid, (unsigned long long)objid,
            (unsigned long long)entry->dbg_offset, (unsigned long long)offset,
            tag, share, entry->dbg_owner,
            (entry->remote_page_base ==
                (unwinder->tstate_addr & ~((uintptr_t)unwinder->handle.page_size - 1))),
            (entry->remote_page_base ==
                (unwinder->interpreter_addr & ~((uintptr_t)unwinder->handle.page_size - 1))));
        entry->dbg_objid = objid;
        entry->dbg_offset = offset;
    }
}

static int
alias_dbg_plausible(RemoteUnwinderObject *unwinder, uintptr_t ptr)
{
    if (ptr == 0) {
        return 1;
    }
    if ((ptr & (uintptr_t)(sizeof(void *) - 1)) != 0) {
        return 0;
    }
    if (ptr < (uintptr_t)unwinder->handle.page_size) {
        return 0;
    }
    if ((ptr >> 56) != 0) {
        return 0;
    }
    return 1;
}

/* E3: on a frame read through the alias, detect (ground truth) whether the
 * page was recycled, and attribute which offset-free predicate would catch it.
 */
void
_Py_RemoteDebug_AliasDbgFrameCheck(RemoteUnwinderObject *unwinder,
                                   uintptr_t address, const char *frame_buf,
                                   uintptr_t expected_parent)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    if (!cache->dbg_enabled) {
        return;
    }
    struct _Py_DebugOffsets *off = &unwinder->debug_offsets;
    uintptr_t exec = GET_MEMBER_NO_TAG(uintptr_t, frame_buf,
                                       off->interpreter_frame.executable);
    int owner = (unsigned char)GET_MEMBER(char, frame_buf,
                                          off->interpreter_frame.owner);
    uintptr_t previous = GET_MEMBER(uintptr_t, frame_buf,
                                    off->interpreter_frame.previous);

    size_t ps = (size_t)unwinder->handle.page_size;
    uintptr_t page_base = address & ~(uintptr_t)(ps - 1);
    AliasPageEntry *entry = alias_find_entry(unwinder, page_base);
    if (!entry) {
        return;
    }
    uint64_t objid = 0, offset = 0;
    uint32_t tag = 0;
    uint8_t share = 0;
    int recycled;
    if (alias_dbg_query_region(unwinder, page_base, &objid, &offset,
                               &tag, &share) < 0) {
        recycled = 1;  /* region gone */
    }
    else {
        /* compare against the IMMUTABLE map-time baseline, not entry->dbg_objid
         * (which E1's check_hit mutates during this same read). */
        recycled = (objid != entry->dbg_map_objid
                    || offset != entry->dbg_map_offset);
    }

    int baseline_ok = (owner >= 0 && owner <= 3)
        && alias_dbg_plausible(unwinder, exec)
        && alias_dbg_plausible(unwinder, previous);

    if (!recycled) {
        /* bootstrap &PyCode_Type from a healthy, plausible THREAD/GEN frame */
        if (cache->dbg_code_type == 0 && baseline_ok && exec != 0
                && (owner == 0 || owner == 1)) {
            uintptr_t obtype = 0;
            if (_Py_RemoteDebug_ReadRemoteMemory(
                    &unwinder->handle, exec + off->pyobject.ob_type,
                    sizeof(obtype), &obtype) >= 0
                && alias_dbg_plausible(unwinder, obtype)) {
                cache->dbg_code_type = obtype;
            }
        }
        return;
    }

    cache->dbg_frame_recyc++;
    cache->dbg_sample_recyc++;
    int caught = 0;
    if (!baseline_ok) {
        cache->dbg_catch_valsnap++;
        caught = 1;
    }
    /* V3: ob_type anchor (only meaningful when baseline passed) */
    if (cache->dbg_code_type != 0 && exec != 0
            && alias_dbg_plausible(unwinder, exec)) {
        uintptr_t obtype = 0;
        if (_Py_RemoteDebug_ReadRemoteMemory(
                &unwinder->handle, exec + off->pyobject.ob_type,
                sizeof(obtype), &obtype) >= 0) {
            if (obtype != cache->dbg_code_type) {
                cache->dbg_catch_obtype++;
                if (baseline_ok) {
                    caught = 1;
                }
            }
        }
    }
    /* V1: expected_parent (only meaningful when the caller supplied one) */
    if (expected_parent != 0) {
        cache->dbg_parent_available++;
        if (previous != expected_parent) {
            cache->dbg_catch_parent++;
            if (baseline_ok) {
                caught = 1;
            }
        }
    }
    if (caught) {
        cache->dbg_catch_any++;
    }
}

void
_Py_RemoteDebug_AliasDbgSetOwner(RemoteUnwinderObject *unwinder,
                                 uintptr_t remote_addr, int owner)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    if (!cache->dbg_enabled) {
        return;
    }
    size_t page_size = (size_t)unwinder->handle.page_size;
    uintptr_t page_base = remote_addr & ~(uintptr_t)(page_size - 1);
    AliasPageEntry *entry = alias_find_entry(unwinder, page_base);
    if (entry) {
        entry->dbg_owner = (int8_t)owner;
    }
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
    if (cache->dbg_enabled) {
        fprintf(stderr,
            "[aliasdbg] SUMMARY checks=%llu recycle_events=%llu\n",
            (unsigned long long)cache->dbg_checks,
            (unsigned long long)cache->dbg_recycle_events);
        fprintf(stderr,
            "[aliasdbg] E3 code_type=0x%llx frame_recyc=%llu "
            "catch_baseline=%llu catch_obtype=%llu "
            "parent_avail=%llu catch_parent=%llu catch_any=%llu\n",
            (unsigned long long)cache->dbg_code_type,
            (unsigned long long)cache->dbg_frame_recyc,
            (unsigned long long)cache->dbg_catch_valsnap,
            (unsigned long long)cache->dbg_catch_obtype,
            (unsigned long long)cache->dbg_parent_available,
            (unsigned long long)cache->dbg_catch_parent,
            (unsigned long long)cache->dbg_catch_any);
        fprintf(stderr,
            "[aliasdbg] SILENT recyc_in_success=%llu recyc_in_fail=%llu "
            "samples_success_with_recyc=%llu samples_fail_with_recyc=%llu\n",
            (unsigned long long)cache->dbg_recyc_in_success,
            (unsigned long long)cache->dbg_recyc_in_fail,
            (unsigned long long)cache->dbg_samples_success_with_recyc,
            (unsigned long long)cache->dbg_samples_fail_with_recyc);
    }
    for (int i = 0; i < MAX_ALIAS_PAGES; i++) {
        alias_deallocate_entry(&cache->pages[i]);
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
    cache->dbg_enabled = (getenv("ALIAS_OBJID_DEBUG") != NULL);
    cache->dbg_force_deepwalk = (getenv("ALIAS_FORCE_DEEPWALK") != NULL);
    cache->dbg_no_datastack = (getenv("ALIAS_NO_DATASTACK") != NULL);
    cache->dbg_chunkcopy_datastack = (getenv("ALIAS_CHUNK_DATASTACK") != NULL);
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
    alias_dbg_record_map(unwinder, entry);

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
        alias_dbg_check_hit(unwinder, entry);
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
