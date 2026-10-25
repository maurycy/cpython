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
/* gi_iframe is a complete DETACHED record, so restoring is one struct copy.
   The stack pointer is kept in a register across the copy so pushing the
   sent value does not reload it from the bytes just written. When ip is
   not NULL it replaces the saved instruction pointer, so the slot is
   written once with its final contents. */
static inline Py_ALWAYS_INLINE void
_PyGen_LinkFrame(PyGenObject *gen, _PyControlRecord *frame, _PyStackRef *value, _Py_CODEUNIT *ip, int state)
{
    assert(gen->gi_iframe.state == CONTROL_DETACHED);
    assert(gen->gi_iframe.data == &gen->gi_data);
    assert(gen->gi_iframe.owner == FRAME_OWNED_BY_GENERATOR);
    /* Read the whole saved record into registers before the first store
       to the slot: when the two share the low 12 bits of their addresses,
       a load issued after a store to the other one waits for that store
       to retire (4K aliasing). Five scalar words keep gcc from spilling. */
    uint64_t w0, w1, w2, w3, w4;
    memcpy(&w0, (const char *)&gen->gi_iframe + 0, 8);
    memcpy(&w1, (const char *)&gen->gi_iframe + 8, 8);
    memcpy(&w2, (const char *)&gen->gi_iframe + 16, 8);
    memcpy(&w3, (const char *)&gen->gi_iframe + 24, 8);
    memcpy(&w4, (const char *)&gen->gi_iframe + 32, 8);
    /* Keep the five words in general registers: the LTO build merged
       these loads into 16-byte vector loads, which cannot be forwarded
       from the 8-byte stores the last suspend made to the same fields
       (four store-to-load interlocks per resume). */
    __asm__("" : "+r"(w0), "+r"(w1), "+r"(w2), "+r"(w3), "+r"(w4));
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

/* Saving the live record: read every field from the slot before the
   first store into the generator, for the same aliasing reason as in
   _PyGen_LinkFrame. */
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

/* Would RESUME_CHECK pass for this suspended generator right now? */
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

/* A suspended generator whose next instructions are RESUME_CHECK and
   POP_TOP discards the value it is resumed with. When RESUME_CHECK would
   pass, both instructions can be skipped and the value need not be
   pushed. Returns the instruction to resume at, or NULL to take the
   ordinary path. */
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
    // Inlined resume: published ACTIVE, nothing runs before _PUSH_FRAME.
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

/* Resuming a generator from C needs an interpreter entry record and the
   generator record. Reserve both with one bounds check; fall back to the
   two separate pushes when the committed region must grow. The value to
   push (or NULL) and the resume address (or NULL for the saved one) are
   known before the slot is written, so it is written once. On failure
   nothing stays pushed, *entry is NULL and *value is untouched. */
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
