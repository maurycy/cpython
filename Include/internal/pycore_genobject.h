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
#include "pycore_interpframe_structs.h" // _PyGenObject

#include <stddef.h>               // offsetof()
#include "pycore_tstate.h"        // _PyThreadStateImpl
#include "pycore_interpframe.h"   // _PyFrame_GetCode()
#include "pycore_pyatomic_ft_wrappers.h"
#include "opcode_ids.h"           // RESUME_CHECK

/* A suspended generator whose next instructions are RESUME_CHECK and
   POP_TOP discards the value it is resumed with. When RESUME_CHECK would
   pass, both instructions can be skipped and the value need not be
   pushed. Returns the instruction to resume at, or NULL. */
static inline _Py_CODEUNIT *
_PyGen_SkipDiscardedResume(PyThreadState *tstate, _PyInterpreterFrame *frame)
{
    _Py_CODEUNIT *ip = frame->instr_ptr;
    if (ip[0].op.code != RESUME_CHECK || ip[2].op.code != POP_TOP) {
        return NULL;
    }
    uintptr_t eval_breaker = _Py_atomic_load_uintptr_relaxed(&tstate->eval_breaker);
    uintptr_t version = FT_ATOMIC_LOAD_UINTPTR_ACQUIRE(
        _PyFrame_GetCode(frame)->_co_instrumentation_version);
    if (eval_breaker != version) {
        return NULL;
    }
#ifdef Py_GIL_DISABLED
    if (frame->tlbc_index != ((_PyThreadStateImpl *)tstate)->tlbc_index) {
        return NULL;
    }
#endif
    return ip + 3;
}


static inline
PyGenObject *_PyGen_GetGeneratorFromFrame(_PyInterpreterFrame *frame)
{
    assert(frame->owner == FRAME_OWNED_BY_GENERATOR);
    size_t offset_in_gen = offsetof(PyGenObject, gi_iframe);
    return (PyGenObject *)(((char *)frame) - offset_in_gen);
}

// The generator finalizer is a no-op once its frame is cleared.
static inline int
_PyGen_FinalizerIsNoop(PyObject *op)
{
    return Py_TYPE(op)->tp_finalize == PyGen_Type.tp_finalize &&
        FRAME_STATE_FINISHED(((PyGenObject *)op)->gi_frame_state);
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
PyAPI_FUNC(PyObject *) _PyCoro_ComputeOrigin(int origin_depth, _PyInterpreterFrame *current_frame);

extern PyTypeObject _PyCoroWrapper_Type;
extern PyTypeObject _PyAsyncGenWrappedValue_Type;
extern PyTypeObject _PyAsyncGenAThrow_Type;

PyAPI_FUNC(PySendResult) _PyAsyncGenASend_Send(PyObject *iter, PyObject *arg, PyObject **result);

#ifdef __cplusplus
}
#endif
#endif /* !Py_INTERNAL_GENOBJECT_H */
