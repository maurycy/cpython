#include "_remote_debugging.h"

#if defined(__APPLE__) && TARGET_OS_OSX

#ifndef VM_FLAGS_RETURN_DATA_ADDR
#  define VM_FLAGS_RETURN_DATA_ADDR 0
#endif

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
        cache->dbg_sample_recycle_events++;
        fprintf(stderr,
            "[aliasdbg] RECYCLE(unmapped) page=0x%llx old_objid=0x%llx owner=%d "
            "sample=%llu tick=%d\n",
            (unsigned long long)entry->remote_page_base,
            (unsigned long long)entry->dbg_objid, entry->dbg_owner,
            (unsigned long long)unwinder->stats.total_samples,
            cache->dbg_sample_tick);
        entry->dbg_objid = 0;
        return;
    }
    if (objid != entry->dbg_objid || offset != entry->dbg_offset) {
        cache->dbg_recycle_events++;
        cache->dbg_sample_recycle_events++;
        fprintf(stderr,
            "[aliasdbg] RECYCLE page=0x%llx old_objid=0x%llx new_objid=0x%llx "
            "old_off=0x%llx new_off=0x%llx tag=%u share=%u owner=%d "
            "is_tstate=%d is_interp=%d sample=%llu tick=%d\n",
            (unsigned long long)entry->remote_page_base,
            (unsigned long long)entry->dbg_objid, (unsigned long long)objid,
            (unsigned long long)entry->dbg_offset, (unsigned long long)offset,
            tag, share, entry->dbg_owner,
            (entry->remote_page_base ==
                (unwinder->tstate_addr & ~((uintptr_t)unwinder->handle.page_size - 1))),
            (entry->remote_page_base ==
                (unwinder->interpreter_addr & ~((uintptr_t)unwinder->handle.page_size - 1))),
            (unsigned long long)unwinder->stats.total_samples,
            cache->dbg_sample_tick);
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

static int
alias_query_region(task_t task,
                   mach_vm_address_t addr,
                   uint64_t *objid,
                   uint64_t *offset)
{
    mach_vm_address_t region_addr = addr;
    mach_vm_size_t region_size = 0;
    natural_t depth = 0;
    vm_region_submap_info_data_64_t info;
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
    if (cache->dbg_churn_enabled && cache->dbg_churn_checks > 0) {
        fprintf(stderr,
            "[aliasdbg] CHURN checks=%llu ticks=%llu\n",
            (unsigned long long)cache->dbg_churn_checks,
            (unsigned long long)cache->dbg_churn_ticks);
        if (cache->dbg_enabled) {
            fprintf(stderr,
                "[aliasdbg] CHURN-XCOR recycle_in_tick=%llu recycle_in_notick=%llu "
                "frecyc_in_tick=%llu frecyc_in_notick=%llu "
                "samples_recycle_tick=%llu samples_recycle_notick=%llu\n",
                (unsigned long long)cache->dbg_recycle_in_tick,
                (unsigned long long)cache->dbg_recycle_in_notick,
                (unsigned long long)cache->dbg_frecyc_in_tick,
                (unsigned long long)cache->dbg_frecyc_in_notick,
                (unsigned long long)cache->dbg_samples_recycle_tick,
                (unsigned long long)cache->dbg_samples_recycle_notick);
        }
        for (int i = 0; i < ALIAS_DBG_CHURN_SLOTS; i++) {
            AliasDbgChurnSlot *s = &cache->dbg_churn[i];
            if (s->tstate_addr == 0) {
                break;
            }
            if (s->checks > 0) {
                fprintf(stderr,
                    "[aliasdbg] CHURN-THREAD tstate=0x%llx checks=%llu ticks=%llu\n",
                    (unsigned long long)s->tstate_addr,
                    (unsigned long long)s->checks,
                    (unsigned long long)s->ticks);
            }
        }
    }
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
    cache->dbg_enabled = (getenv("ALIAS_OBJID_DEBUG") != NULL);
    cache->dbg_force_deepwalk = (getenv("ALIAS_FORCE_DEEPWALK") != NULL);
    cache->dbg_no_datastack = (getenv("ALIAS_NO_DATASTACK") != NULL);
    cache->dbg_chunkcopy_datastack = (getenv("ALIAS_CHUNK_DATASTACK") != NULL);
    cache->dbg_churn_enabled =
        (cache->dbg_enabled || getenv("ALIAS_CHURN_DEBUG") != NULL);
}

/* churn sensor: compare the live tstate->datastack_chunk pointer against the
 * value seen at the previous sample of the same thread. The pointer comes from
 * the tstate buffer the unwinder already read this sample, so the sensor adds
 * no remote reads. First sighting of a tstate records a baseline, no tick. */
void
_Py_RemoteDebug_AliasDbgChurnCheck(RemoteUnwinderObject *unwinder,
                                   uintptr_t tstate_addr, uintptr_t chunk_ptr)
{
    AliasReadCache *cache = &unwinder->alias_cache;
    if (!cache->dbg_churn_enabled || tstate_addr == 0) {
        return;
    }
    AliasDbgChurnSlot *slot = NULL;
    for (int i = 0; i < ALIAS_DBG_CHURN_SLOTS; i++) {
        AliasDbgChurnSlot *s = &cache->dbg_churn[i];
        if (s->tstate_addr == tstate_addr) {
            slot = s;
            break;
        }
        if (s->tstate_addr == 0) {
            s->tstate_addr = tstate_addr;
            s->last_chunk = chunk_ptr;
            return;
        }
    }
    if (slot == NULL) {
        return;
    }
    slot->checks++;
    cache->dbg_churn_checks++;
    cache->dbg_sample_churn_valid = 1;
    if (chunk_ptr != slot->last_chunk) {
        slot->ticks++;
        cache->dbg_churn_ticks++;
        cache->dbg_sample_tick = 1;
        slot->last_chunk = chunk_ptr;
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
        return -1;
    }

    entry->remote_page_base = page_base;
    entry->size = (mach_vm_size_t)unwinder->handle.page_size;
    entry->access_seq = ++cache->access_seq;
    entry->valid = 1;
    alias_dbg_record_map(unwinder, entry);

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

    if (unwinder->collect_stats) {
        uint32_t i;
        for (i = 0; i < cache->ws_count; i++) {
            if (cache->ws_pages[i] == page_base) {
                break;
            }
        }
        if (i == cache->ws_count) {
            if (cache->ws_count < ALIAS_WS_MAX) {
                cache->ws_pages[cache->ws_count++] = page_base;
            }
            else {
                cache->ws_overflow++;
            }
        }
    }

    AliasPageEntry *entry = alias_find_entry(unwinder, page_base);
    if (entry != NULL) {
        int probe = alias_maybe_probe_entry(unwinder, entry);
        if (probe < 0) {
            return _Py_RemoteDebug_ReadRemoteMemory(
                &unwinder->handle, remote_addr, len, dst);
        }
        if (probe > 0) {
            entry->access_seq = ++cache->access_seq;
            alias_dbg_check_hit(unwinder, entry);
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
