#ifndef Py_INTERNAL_DARWIN_VM_H
#define Py_INTERNAL_DARWIN_VM_H
#ifdef __cplusplus
extern "C" {
#endif

#ifndef Py_BUILD_CORE
#  error "this header requires Py_BUILD_CORE define"
#endif

// Helpers for XNU's reusable-memory advice on macOS.
//
// On macOS, MADV_DONTNEED does not decommit: it mostly deactivates pages while
// preserving their contents, so it does little to reduce phys_footprint.  XNU
// instead offers a paired protocol:
//
//   MADV_FREE_REUSABLE  marks a range reusable and moves it out of
//                       phys_footprint (VM_BEHAVIOR_REUSABLE).
//   MADV_FREE_REUSE     reclaims a previously-reusable range before it is read
//                       or written again (VM_BEHAVIOR_REUSE).
//
// The invariant is: once a range is marked reusable, the owner must not read or
// write it until it has issued MADV_FREE_REUSE for that range.
//
// This header is intentionally self-contained (no Python.h dependency) so it
// can be included from low-level allocator code.

#if defined(__APPLE__)

#include <stddef.h>               // size_t
#include <stdint.h>               // uintptr_t
#include <errno.h>                // errno
#include <sys/mman.h>             // madvise(), MADV_FREE_REUSABLE/REUSE

// Runtime gate (default 0/off) and the page size used for alignment.  Both are
// defined in Objects/obmalloc.c and initialized once, single-threaded, during
// pre-configuration; the inline helpers below only read them.
extern int _Py_obmalloc_reusable_enabled;
extern size_t _Py_obmalloc_reusable_pagesize;

static inline int
_PyDarwinVM_ReusableEnabled(void)
{
    return _Py_obmalloc_reusable_enabled;
}

#if defined(MADV_FREE_REUSABLE) && defined(MADV_FREE_REUSE)

#define _PyDarwinVM_HAVE_REUSABLE 1

// Page-align [ptr, ptr+size): round start up and end down to whole pages.
// Returns the aligned start in *aligned and the aligned length as the result;
// a zero length means there are no whole pages to advise.
static inline size_t
_PyDarwinVM_AlignRange(void *ptr, size_t size, void **aligned)
{
    size_t ps = _Py_obmalloc_reusable_pagesize;
    if (ps == 0) {
        *aligned = ptr;
        return 0;
    }
    uintptr_t start = ((uintptr_t)ptr + ps - 1) & ~(uintptr_t)(ps - 1);
    uintptr_t end = ((uintptr_t)ptr + size) & ~(uintptr_t)(ps - 1);
    *aligned = (void *)start;
    if (end <= start) {
        return 0;
    }
    return (size_t)(end - start);
}

// Mark a range reusable (out of phys_footprint).  Returns 0 on success or the
// errno from madvise() so the caller can fall back / leave it charged.
static inline int
_PyDarwinVM_MarkReusable(void *ptr, size_t size)
{
    void *aligned;
    size_t len = _PyDarwinVM_AlignRange(ptr, size, &aligned);
    if (len == 0) {
        return 0;
    }
    if (madvise(aligned, len, MADV_FREE_REUSABLE) != 0) {
        return errno;
    }
    return 0;
}

// Reclaim a previously-reusable range before it is read or written again.
// Returns 0 on success or the errno from madvise().
static inline int
_PyDarwinVM_MarkReused(void *ptr, size_t size)
{
    void *aligned;
    size_t len = _PyDarwinVM_AlignRange(ptr, size, &aligned);
    if (len == 0) {
        return 0;
    }
    if (madvise(aligned, len, MADV_FREE_REUSE) != 0) {
        return errno;
    }
    return 0;
}

#endif  // MADV_FREE_REUSABLE && MADV_FREE_REUSE

#endif  // __APPLE__

#ifdef __cplusplus
}
#endif
#endif  // !Py_INTERNAL_DARWIN_VM_H
