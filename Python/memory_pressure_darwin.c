/* macOS memory-pressure source.
 *
 * Listens for XNU memory-pressure warnings via a libdispatch source and, at a
 * GIL-safe point, asks allocator caches to release reclaimable memory
 * (_PyMem_ReclaimUnusedMemory).  The whole file is empty off macOS, and its
 * symbols only exist on macOS - callers in pylifecycle.c are guarded by
 * #if defined(__APPLE__), so no other build (e.g. Windows/PCbuild) references
 * them or needs to compile this file.
 *
 * Concurrency contract (load-bearing): the dispatch event handler runs OFF the
 * GIL on a libdispatch worker thread, so it must NOT touch allocator/runtime
 * state.  It only updates atomics and schedules a Py_AddPendingCall; the actual
 * reclaim runs later under the GIL on the main interpreter's main thread.
 */

#if defined(__APPLE__)

#include "Python.h"
#include "pycore_pymem.h"         // _PyMem_ReclaimUnusedMemory()
#include "pycore_darwin_vm.h"     // _PyDarwinVM_ReusableEnabled()

#include <dispatch/dispatch.h>
#include <stdatomic.h>

/* Process-global: a single source for the whole process (the main interpreter
 * owns it).  Touched only from the main thread's start/stop and the source's
 * own serial queue. */
static dispatch_queue_t pressure_queue;
static dispatch_source_t pressure_source;
static dispatch_semaphore_t pressure_stop_sema;

/* Cleared before teardown.  Checked by both the off-GIL event handler (don't
 * schedule) and the under-GIL pending call (don't reclaim) so a late event or
 * an already-scheduled pending call becomes a no-op. */
static _Atomic(int) pressure_alive;
/* 1 while a Py_AddPendingCall is outstanding (coalesce repeated events). */
static _Atomic(int) pressure_pending;
/* Highest pressure level requested but not yet serviced (1=warn, 2=critical). */
static _Atomic(int) pressure_level;

/* Runs under the GIL on the main thread via the pending-call machinery. */
static int
pressure_pending_call(void *Py_UNUSED(arg))
{
    atomic_store_explicit(&pressure_pending, 0, memory_order_release);
    if (!atomic_load_explicit(&pressure_alive, memory_order_acquire)) {
        return 0;  /* stopped between scheduling and running: do nothing */
    }
    int level = atomic_exchange_explicit(&pressure_level, 0, memory_order_acq_rel);
    if (level <= 0) {
        return 0;
    }
    _PyMem_ReclaimUnusedMemory(PyThreadState_Get(), level);
    return 0;
}

/* Runs OFF the GIL on the serial dispatch queue.  Atomics + Py_AddPendingCall
 * only - never touches allocator state. */
static void
pressure_event_handler(void *Py_UNUSED(ctx))
{
    if (!atomic_load_explicit(&pressure_alive, memory_order_acquire)) {
        return;
    }
    unsigned long flags = dispatch_source_get_data(pressure_source);
    int level = (flags & DISPATCH_MEMORYPRESSURE_CRITICAL) ? 2 : 1;

    /* Raise the stored level to max(current, level). */
    int cur = atomic_load_explicit(&pressure_level, memory_order_relaxed);
    while (level > cur &&
           !atomic_compare_exchange_weak_explicit(&pressure_level, &cur, level,
                                                  memory_order_acq_rel,
                                                  memory_order_relaxed)) {
        /* cur reloaded by the CAS */
    }

    /* Schedule at most one outstanding pending call. */
    int expected = 0;
    if (atomic_compare_exchange_strong_explicit(&pressure_pending, &expected, 1,
                                                memory_order_acq_rel,
                                                memory_order_relaxed)) {
        if (Py_AddPendingCall(pressure_pending_call, NULL) != 0) {
            /* Pending queue full: clear the flag so a later event re-arms
             * rather than getting stuck "pending but never enqueued". */
            atomic_store_explicit(&pressure_pending, 0, memory_order_release);
        }
    }
}

/* Runs on the serial queue after the source is cancelled and any in-flight
 * event handler has finished.  Releases the dispatch objects (the documented-
 * safe place to do so) and unblocks _PyMem_DarwinPressureStop(). */
static void
pressure_cancel_handler(void *Py_UNUSED(ctx))
{
    if (pressure_source != NULL) {
        dispatch_release(pressure_source);
        pressure_source = NULL;
    }
    if (pressure_queue != NULL) {
        dispatch_release(pressure_queue);
        pressure_queue = NULL;
    }
    dispatch_semaphore_signal(pressure_stop_sema);
}

void
_PyMem_DarwinPressureStart(void)
{
    if (!_PyDarwinVM_ReusableEnabled()) {
        return;
    }
    if (pressure_source != NULL) {
        return;  /* already started */
    }
    pressure_stop_sema = dispatch_semaphore_create(0);
    if (pressure_stop_sema == NULL) {
        return;
    }
    pressure_queue = dispatch_queue_create("org.python.memory-pressure",
                                           DISPATCH_QUEUE_SERIAL);
    if (pressure_queue == NULL) {
        dispatch_release(pressure_stop_sema);
        pressure_stop_sema = NULL;
        return;
    }
    pressure_source = dispatch_source_create(
        DISPATCH_SOURCE_TYPE_MEMORYPRESSURE, 0,
        DISPATCH_MEMORYPRESSURE_WARN | DISPATCH_MEMORYPRESSURE_CRITICAL,
        pressure_queue);
    if (pressure_source == NULL) {
        dispatch_release(pressure_queue);
        pressure_queue = NULL;
        dispatch_release(pressure_stop_sema);
        pressure_stop_sema = NULL;
        return;
    }
    dispatch_source_set_event_handler_f(pressure_source, pressure_event_handler);
    dispatch_source_set_cancel_handler_f(pressure_source, pressure_cancel_handler);
    /* Fresh state for this start cycle (don't inherit a stale level/pending
     * from a previous start/stop). */
    atomic_store_explicit(&pressure_level, 0, memory_order_relaxed);
    atomic_store_explicit(&pressure_pending, 0, memory_order_relaxed);
    atomic_store_explicit(&pressure_alive, 1, memory_order_release);
    /* Dispatch sources start suspended; resume exactly once. */
    dispatch_resume(pressure_source);
}

void
_PyMem_DarwinPressureStop(void)
{
    /* Make any concurrent/late handler and any already-scheduled pending call
     * a no-op before we tear down. */
    atomic_store_explicit(&pressure_alive, 0, memory_order_release);
    if (pressure_source == NULL) {
        return;
    }
    /* Cancel stops new event delivery; the cancel handler runs after any
     * in-flight event handler completes, releases the source+queue, and signals
     * the semaphore.  Waiting here guarantees no handler is in flight and the
     * objects are freed before finalization proceeds.  The handler never takes
     * the GIL, so waiting while holding it cannot deadlock. */
    dispatch_source_cancel(pressure_source);
    dispatch_semaphore_wait(pressure_stop_sema, DISPATCH_TIME_FOREVER);
    dispatch_release(pressure_stop_sema);
    pressure_stop_sema = NULL;
    /* pressure_source / pressure_queue were released and NULLed by the cancel
     * handler. */
}

#endif  /* __APPLE__ */
