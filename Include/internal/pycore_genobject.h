#ifndef Py_INTERNAL_GENOBJECT_H
#define Py_INTERNAL_GENOBJECT_H
#ifdef __cplusplus
extern "C" {
#endif

#ifndef Py_BUILD_CORE
#  error "this header requires Py_BUILD_CORE define"
#endif

#include "pycore_abstract.h"      // PySendResultPair
#include "pycore_frame.h"         // FRAME_STATE_FINISHED()
#include "pycore_interpframe.h"
#include "pycore_tstate.h"        // _PyThreadStateImpl
#include "opcode_ids.h"           // RESUME_CHECK

#include <stddef.h>               // offsetof()
#include <string.h>               // memcpy()


static inline
PyGenObject *_PyGen_GetGeneratorFromFrame(_PyInterpreterDataFrame *frame)
{
    return (PyGenObject *)(((char *)frame) - offsetof(PyGenObject, gi_data));
}

// The generator finalizer is a no-op once its frame is cleared.
static inline int
_PyGen_FinalizerIsNoop(PyObject *op)
{
    return Py_TYPE(op)->tp_finalize == PyGen_Type.tp_finalize &&
        FRAME_STATE_FINISHED(((PyGenObject *)op)->gi_frame_state);
}
#ifdef Py_GIL_DISABLED
PyAPI_FUNC(void) _PyGen_LinkFrame(PyGenObject *gen, _PyControlRecord *frame, _PyStackRef *value, _Py_CODEUNIT *ip, int state);
PyAPI_FUNC(void) _PyGen_UnlinkFrame(PyGenObject *gen, _PyControlRecord *frame);
#else
/* Restore the saved record, optionally replacing its instruction pointer
   and pushing a value. Keep this helper inlined on the resume paths. */
static inline Py_ALWAYS_INLINE void
_PyGen_LinkFrame(PyGenObject *gen, _PyControlRecord *frame, _PyStackRef *value, _Py_CODEUNIT *ip, int state)
{
    assert(gen->gi_iframe.state == CONTROL_DETACHED);
    assert(gen->gi_iframe.data == &gen->gi_data);
    assert(gen->gi_iframe.owner == FRAME_OWNED_BY_GENERATOR);
    /* Load all five words before writing the live record. Interleaving
       these accesses can stall when the addresses alias in the store buffer.
       The explicit words avoided spills from an aggregate copy in the
       measured GCC PGO build. Words 1 and 2 hold the instruction and stack
       pointers; update them before storing the record to avoid a reload. */
    uint64_t w0, w1, w2, w3, w4;
    memcpy(&w0, (const char *)&gen->gi_iframe + 0, 8);
    memcpy(&w1, (const char *)&gen->gi_iframe + 8, 8);
    memcpy(&w2, (const char *)&gen->gi_iframe + 16, 8);
    memcpy(&w3, (const char *)&gen->gi_iframe + 24, 8);
    memcpy(&w4, (const char *)&gen->gi_iframe + 32, 8);
    _PyStackRef *sp = (_PyStackRef *)(uintptr_t)w2;
    if (value != NULL) {
        *sp++ = *value;
        w2 = (uint64_t)(uintptr_t)sp;
    }
    if (ip != NULL) {
        w1 = (uint64_t)(uintptr_t)ip;
    }
    memcpy((char *)frame + 0, &w0, 8);
    memcpy((char *)frame + 8, &w1, 8);
    memcpy((char *)frame + 16, &w2, 8);
    memcpy((char *)frame + 24, &w3, 8);
    memcpy((char *)frame + 32, &w4, 8);
    frame->state = (uint8_t)state;
    PyFrameObject *f = gen->gi_data.frame_obj;
    if (f != NULL) {
        f->f_frame = frame;
    }
    gen->gi_live_frame = frame;
}

/* Save the fields that change while the generator runs. Read them before
   writing gi_iframe, for the same aliasing reason as in _PyGen_LinkFrame.
   The saved record keeps its executable, data, owner and DETACHED state. */
static inline void
_PyGen_UnlinkFrame(PyGenObject *gen, _PyControlRecord *frame)
{
    _Py_CODEUNIT *ip = frame->instr_ptr;
    _PyStackRef *sp = frame->stackpointer;
    uint16_t return_offset = frame->return_offset;
#ifdef Py_DEBUG
    uint8_t stackpointer_valid = frame->stackpointer_valid;
    uint8_t lltrace = frame->lltrace;
#endif
    PyFrameObject *f = gen->gi_data.frame_obj;
    gen->gi_iframe.instr_ptr = ip;
    gen->gi_iframe.stackpointer = sp;
    gen->gi_iframe.return_offset = return_offset;
#ifdef Py_DEBUG
    gen->gi_iframe.stackpointer_valid = stackpointer_valid;
    gen->gi_iframe.lltrace = lltrace;
#endif
    if (f != NULL) {
        f->f_frame = &gen->gi_iframe;
    }
    gen->gi_live_frame = &gen->gi_iframe;
}
#endif

/* Skipping RESUME_CHECK must still respect pending work, instrumentation
   changes and, in free-threaded builds, the thread-local bytecode index. */
static inline int
_PyGen_ResumeCheckPasses(PyThreadState *tstate, _PyControlRecord *saved)
{
    uintptr_t eval_breaker = _Py_atomic_load_uintptr_relaxed(&tstate->eval_breaker);
    uintptr_t version = FT_ATOMIC_LOAD_UINTPTR_ACQUIRE(
        _PyFrame_GetCode(saved)->_co_instrumentation_version);
#ifdef Py_GIL_DISABLED
    if (saved->tlbc_index != ((_PyThreadStateImpl *)tstate)->tlbc_index) {
        return 0;
    }
#endif
    return eval_breaker == version;
}

/* Return the instruction after RESUME_CHECK and POP_TOP if the check would
   pass, or NULL for the normal resume path. Callers use this when sending
   None, so the discarded value need not be pushed. ip[1] is the inline
   cache entry for RESUME_CHECK. */
static inline _Py_CODEUNIT *
_PyGen_SkipDiscardedResume(PyThreadState *tstate, _PyControlRecord *saved)
{
    _Py_CODEUNIT *ip = saved->instr_ptr;
    if (ip[0].op.code != RESUME_CHECK || ip[2].op.code != POP_TOP ||
        !_PyGen_ResumeCheckPasses(tstate, saved)) {
        return NULL;
    }
    return ip + 3;
}

static inline _PyControlRecord *
_PyGen_PushFrameUnchecked(PyThreadState *tstate, PyGenObject *gen, _PyStackRef value)
{
    assert(gen->gi_live_frame == &gen->gi_iframe);
    // No Python callback can run before _PUSH_FRAME installs this record.
    _PyControlRecord *frame = _PyThreadState_ReserveControlUnchecked(tstate);
    _Py_CODEUNIT *ip = NULL;
    if (PyStackRef_IsNone(value)) {
        ip = _PyGen_SkipDiscardedResume(tstate, &gen->gi_iframe);
    }
    _PyGen_LinkFrame(gen, frame, ip == NULL ? &value : NULL, ip, CONTROL_ACTIVE);
    return frame;
}

static inline _PyControlRecord *
_PyGen_PushFrame(PyThreadState *tstate, PyGenObject *gen)
{
    assert(gen->gi_live_frame == &gen->gi_iframe);
    _PyControlRecord *frame = _PyThreadState_PushControl(tstate);
    if (frame != NULL) {
        _PyGen_LinkFrame(gen, frame, NULL, NULL, CONTROL_RESERVED);
    }
    return frame;
}

/* Reserve the C entry and generator records with one bounds check. Use
   checked pushes when space is unavailable, unwinding the entry on failure.
   A NULL value means no push; a NULL ip keeps the saved resume address.
   On success, the record takes *value if supplied. On failure, *entry is
   NULL, neither record remains pushed, and the caller still owns *value. */
static inline _PyControlRecord *
_PyGen_PushEntryAndFrame(PyThreadState *tstate, PyGenObject *gen,
                         _PyControlRecord **entry, _PyStackRef *value,
                         _Py_CODEUNIT *ip)
{
    assert(gen->gi_live_frame == &gen->gi_iframe);
    _PyControlRecord *e;
    _PyControlRecord *frame;
    if (_PyThreadState_HasControlSpace(tstate, 2)) {
        _PyThreadState_AdjustControlLimit(tstate, 1);
        e = _PyThreadState_PushControlUnchecked(tstate, CONTROL_RESERVED);
        e->owner = FRAME_OWNED_BY_INTERPRETER;
        frame = _PyThreadState_ReserveControlUnchecked(tstate);
    }
    else {
        e = _PyThreadState_PushEntry(tstate);
        if (e == NULL) {
            *entry = NULL;
            return NULL;
        }
        frame = _PyThreadState_PushControl(tstate);
        if (frame == NULL) {
            _PyThreadState_PopEntry(tstate, e);
            *entry = NULL;
            return NULL;
        }
    }
    _PyGen_LinkFrame(gen, frame, value, ip, CONTROL_RESERVED);
    *entry = e;
    return frame;
}

static inline void
_PyGen_PopFrame(PyThreadState *tstate, PyGenObject *gen, _PyControlRecord *frame)
{
    assert(gen->gi_live_frame == frame);
    frame->state = CONTROL_RESERVED;
    _PyGen_UnlinkFrame(gen, frame);
    _PyThreadState_PopControl(tstate, frame);
}

#ifndef Py_GIL_DISABLED
/* Detach a finished generator without saving a continuation, then clear its
   references. The caller has marked it CLEARED and restored the exception
   chain. No frame object may refer to the live record.

   Keep gi_iframe DETACHED with an empty stack, and clear the locals using
   the live stack pointer captured before the pop. Finalizers can re-enter
   Python, so gi_live_frame must stop pointing to the slot before cleanup. */
static inline void
_PyGen_PopFrameFinished(PyThreadState *tstate, PyGenObject *gen,
                        _PyControlRecord *frame, _PyInterpreterDataFrame *fp)
{
    assert(gen->gi_live_frame == frame);
    assert(fp->frame_obj == NULL);
    assert(fp == &gen->gi_data);
    _PyStackRef *sp = frame->stackpointer;
    _PyStackRef *locals = fp->localsplus;
    gen->gi_iframe.stackpointer = locals;
#ifdef Py_DEBUG
    gen->gi_iframe.stackpointer_valid = 1;
#endif
    gen->gi_live_frame = &gen->gi_iframe;
    _PyThreadState_PopControl(tstate, frame);
    while (sp > locals) {
        sp--;
        PyStackRef_XCLOSE(*sp);
    }
    Py_CLEAR(fp->f_locals);
    PyStackRef_CLEAR(fp->f_funcobj);
}
#endif

PyAPI_FUNC(PyObject *)_PyGen_yf(PyGenObject *);
extern int _PyGen_ClearFrame(PyGenObject *self);

// Export for '_asyncio' shared extension
PyAPI_FUNC(int) _PyGen_SetStopIterationValue(PyObject *);

// Export for '_asyncio' shared extension
PyAPI_FUNC(PySendResultPair) _PyGen_SendNone(PyObject *gen);

// Export for '_asyncio' shared extension
PyAPI_FUNC(int) _PyGen_FetchStopIterationValue(PyObject **);
// Set the exception passed to throw(typ[, val[, tb]]).
// Return 0 on success, -1 on failure.
extern int _PyGen_SetException(PyObject *typ, PyObject *val, PyObject *tb);

PyAPI_FUNC(PyObject *)_PyCoro_GetAwaitableIter(PyObject *o);
PyAPI_FUNC(PyObject *)_PyAsyncGenValueWrapperNew(PyThreadState *state, PyObject *);

// Exported for external JIT support
PyAPI_FUNC(PyObject *) _PyCoro_ComputeOrigin(int origin_depth, _PyControlRecord *current_frame);

extern PyTypeObject _PyCoroWrapper_Type;
extern PyTypeObject _PyAsyncGenWrappedValue_Type;
extern PyTypeObject _PyAsyncGenAThrow_Type;

PyAPI_FUNC(PySendResult) _PyAsyncGenASend_Send(PyObject *iter, PyObject *arg, PyObject **result);

#ifdef __cplusplus
}
#endif
#endif /* !Py_INTERNAL_GENOBJECT_H */
