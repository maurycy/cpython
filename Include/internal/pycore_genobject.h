#ifndef Py_INTERNAL_GENOBJECT_H
#define Py_INTERNAL_GENOBJECT_H
#ifdef __cplusplus
extern "C" {
#endif

#ifndef Py_BUILD_CORE
#  error "this header requires Py_BUILD_CORE define"
#endif

#include "pycore_frame.h"         // FRAME_STATE_FINISHED()
#include "pycore_interpframe.h"
#include "pycore_tstate.h"        // _PyThreadStateImpl
#include "opcode_ids.h"           // RESUME_CHECK

#include <stddef.h>               // offsetof()


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
PyAPI_FUNC(void) _PyGen_LinkFrame(PyGenObject *gen, _PyControlRecord *frame, _PyStackRef *value, int state);
PyAPI_FUNC(void) _PyGen_UnlinkFrame(PyGenObject *gen, _PyControlRecord *frame);
#else
/* gi_iframe is a complete DETACHED record. Restoring it reads every field
   into registers before the first store: the saved record lives on the
   heap and the slot on the control stack, and when the two share the low
   12 bits of their addresses, a load issued after a store to the other
   location waits for that store to retire (4K aliasing). */
static inline void
_PyGen_LinkFrame(PyGenObject *gen, _PyControlRecord *frame, _PyStackRef *value, int state)
{
    assert(gen->gi_iframe.state == CONTROL_DETACHED);
    assert(gen->gi_iframe.data == &gen->gi_data);
    assert(gen->gi_iframe.owner == FRAME_OWNED_BY_GENERATOR);
    _PyControlRecord saved = gen->gi_iframe;
    _PyStackRef *sp = saved.stackpointer;
    if (value != NULL) {
        *sp++ = *value;
        saved.stackpointer = sp;
    }
    saved.state = (uint8_t)state;
    *frame = saved;
    PyFrameObject *f = gen->gi_data.frame_obj;
    if (f != NULL) {
        f->f_frame = frame;
    }
    gen->gi_live_frame = frame;
}

static inline void
_PyGen_UnlinkFrame(PyGenObject *gen, _PyControlRecord *frame)
{
    gen->gi_iframe.instr_ptr = frame->instr_ptr;
    gen->gi_iframe.stackpointer = frame->stackpointer;
    gen->gi_iframe.return_offset = frame->return_offset;
#ifdef Py_DEBUG
    gen->gi_iframe.stackpointer_valid = frame->stackpointer_valid;
    gen->gi_iframe.lltrace = frame->lltrace;
#endif
    PyFrameObject *f = gen->gi_data.frame_obj;
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
    if (PyStackRef_IsNone(value)) {
        _Py_CODEUNIT *ip = _PyGen_SkipDiscardedResume(tstate, &gen->gi_iframe);
        if (ip != NULL) {
            _PyGen_LinkFrame(gen, frame, NULL, CONTROL_ACTIVE);
            frame->instr_ptr = ip;
            return frame;
        }
    }
    _PyGen_LinkFrame(gen, frame, &value, CONTROL_ACTIVE);
    return frame;
}

static inline _PyControlRecord *
_PyGen_PushFrame(PyThreadState *tstate, PyGenObject *gen)
{
    assert(gen->gi_live_frame == &gen->gi_iframe);
    _PyControlRecord *frame = _PyThreadState_PushControl(tstate);
    if (frame != NULL) {
        _PyGen_LinkFrame(gen, frame, NULL, CONTROL_RESERVED);
    }
    return frame;
}

/* Resuming a generator from C needs an interpreter entry record and the
   generator record. Reserve both with one bounds check; fall back to the
   two separate pushes when the committed region must grow. On failure
   nothing stays pushed and *entry is NULL. */
static inline _PyControlRecord *
_PyGen_PushEntryAndFrame(PyThreadState *tstate, PyGenObject *gen,
                         _PyControlRecord **entry)
{
    assert(gen->gi_live_frame == &gen->gi_iframe);
    if (_PyThreadState_HasControlSpace(tstate, 2)) {
        _PyThreadState_AdjustControlLimit(tstate, 1);
        _PyControlRecord *e = _PyThreadState_PushControlUnchecked(tstate, CONTROL_RESERVED);
        e->owner = FRAME_OWNED_BY_INTERPRETER;
        _PyControlRecord *frame = _PyThreadState_ReserveControlUnchecked(tstate);
        _PyGen_LinkFrame(gen, frame, NULL, CONTROL_RESERVED);
        *entry = e;
        return frame;
    }
    _PyControlRecord *e = _PyThreadState_PushEntry(tstate);
    if (e == NULL) {
        *entry = NULL;
        return NULL;
    }
    _PyControlRecord *frame = _PyGen_PushFrame(tstate, gen);
    if (frame == NULL) {
        _PyThreadState_PopEntry(tstate, e);
        *entry = NULL;
        return NULL;
    }
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
