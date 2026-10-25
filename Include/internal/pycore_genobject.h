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

// The generator finalizer is a no-op once its frame is cleared.
static inline int
_PyGen_FinalizerIsNoop(PyObject *op)
{
    return Py_TYPE(op)->tp_finalize == PyGen_Type.tp_finalize &&
        FRAME_STATE_FINISHED(((PyGenObject *)op)->gi_frame_state);
}

/* The caller has set FRAME_EXECUTING and keeps the generator alive. */
static inline void
_PyGen_ActivateFrame(PyGenObject *gen, _PyControlRecord *slot,
                     _PyStackRef *value)
{
    _PyControlRecord *frame = &gen->gi_iframe;
    assert(gen->gi_control_slot == NULL);
    assert(frame->state == CONTROL_DETACHED);
    assert(frame->data == &gen->gi_data);
    assert(frame->owner == FRAME_OWNED_BY_GENERATOR);
    if (value != NULL) {
        *frame->stackpointer++ = *value;
    }
    slot->data = &gen->gi_data;
    slot->state = CONTROL_INDIRECT;
    frame->state = CONTROL_RESERVED;
    gen->gi_control_slot = slot;
}

static inline void
_PyGen_DeactivateFrame(PyGenObject *gen, _PyControlRecord *frame)
{
    assert(frame == &gen->gi_iframe);
    assert(gen->gi_control_slot != NULL);
    frame->state = CONTROL_DETACHED;
    gen->gi_control_slot = NULL;
}

#ifdef Py_GIL_DISABLED
PyAPI_FUNC(void) _PyGen_LinkFrame(PyGenObject *gen, _PyControlRecord *slot,
                                 _PyStackRef *value);
PyAPI_FUNC(void) _PyGen_UnlinkFrame(PyGenObject *gen, _PyControlRecord *frame);
#else
static inline void
_PyGen_LinkFrame(PyGenObject *gen, _PyControlRecord *slot, _PyStackRef *value)
{
    _PyGen_ActivateFrame(gen, slot, value);
}

static inline void
_PyGen_UnlinkFrame(PyGenObject *gen, _PyControlRecord *frame)
{
    _PyGen_DeactivateFrame(gen, frame);
}
#endif

static inline _PyControlRecord *
_PyGen_PushFrameUnchecked(PyThreadState *tstate, PyGenObject *gen, _PyStackRef value)
{
    assert(gen->gi_control_slot == NULL);
    _PyControlRecord *frame = _PyThreadState_PushControlUnchecked(tstate);
    _PyGen_LinkFrame(gen, frame, &value);
    return &gen->gi_iframe;
}

static inline _PyControlRecord *
_PyGen_PushFrame(PyThreadState *tstate, PyGenObject *gen)
{
    assert(gen->gi_control_slot == NULL);
    _PyControlRecord *frame = _PyThreadState_PushControl(tstate);
    if (frame != NULL) {
        _PyGen_LinkFrame(gen, frame, NULL);
        return &gen->gi_iframe;
    }
    return frame;
}

static inline void
_PyGen_PopFrame(PyThreadState *tstate, PyGenObject *gen, _PyControlRecord *frame)
{
    _PyControlRecord *slot = gen->gi_control_slot;
    frame->state = CONTROL_RESERVED;
    _PyGen_UnlinkFrame(gen, frame);
    _PyThreadState_PopControl(tstate, slot);
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
