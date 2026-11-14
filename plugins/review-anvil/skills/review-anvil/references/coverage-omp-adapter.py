"""Load in Python Eval and inject its real `agent` global. No OMP imports required.

    adapter = OMPNativeAdapter(agent)
    summary = CoverageCoordinator(coverage, adapter, run_id, context, workers=4).run()

Every persistent Eval invocation must use timeout:0. Agent starts and waits occur
on Eval's calling thread, never an executor thread or asyncio.run().
"""
from math import isfinite
from time import monotonic

from coverage_coordinator import WorkerFailure


class OMPNativeAdapter:
    def __init__(self, spawn, timeout=600):
        if not callable(spawn):
            raise TypeError("OMP Eval's real agent callable is required")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not isfinite(timeout) or timeout <= 0):
            raise ValueError("Native wave timeout must be finite and positive")
        self.spawn = spawn
        self._waited = set()
        self.timeout = timeout
        self.deadline = None

    def begin_wave(self):
        """Start one shared stage budget, never a fresh budget per handle."""
        self.deadline = monotonic() + self.timeout

    def _remaining(self):
        if self.deadline is None:
            self.begin_wave()
        return max(0, self.deadline - monotonic())

    def start(self, request):
        if self._remaining() <= 0:
            raise TimeoutError("OMP native wave deadline expired before dispatch")
        return self.spawn(request["prompt"], label=request["label"],
                          schema=request["schema"], schema_mode="strict")

    @staticmethod
    def identity(handle):
        return str(handle.handle)
    @staticmethod
    def done(handle):
        return handle.done()

    @staticmethod
    def cancel(handle):
        # A cancellation request is not a fabricated reviewer response.
        return handle.cancel()


    def wait_one(self, handles):
        if not handles:
            raise ValueError("At least one live OMP handle is required")
        # No polling: choose an already completed result when available, otherwise
        # block on one live handle. All workers have already started concurrently.
        # A failed readiness lookup belongs to that handle, just like a failed
        # wait. Process interrupts still propagate so unconsumed work can drain.
        ready = False
        for handle in handles:
            try:
                if handle.done():
                    ready = True
                    break
            except Exception as exc:
                raise WorkerFailure(handle, exc) from exc
        else:
            handle = handles[0]
        identity = self.identity(handle)
        if identity in self._waited:
            raise WorkerFailure(handle, RuntimeError("OMP handle must be waited exactly once"))
        self._waited.add(identity)
        remaining = self._remaining()
        try:
            if not ready and remaining <= 0:
                raise TimeoutError("OMP native wave deadline expired")
            response = handle.wait(timeout=remaining)
        except TimeoutError as exc:
            # Retire this handle once. Never wait for cancellation or drain an
            # unfinished worker after expiry; later calls can still harvest ready siblings.
            try:
                handle.cancel()
            except Exception as cancel_error:
                exc = TimeoutError(f"{exc}; cancellation failed: {cancel_error}")
            raise WorkerFailure(handle, exc) from exc
        except BaseException as exc:
            raise WorkerFailure(handle, exc) from exc
        # .wait() already returns parsed structured output, not another handle or
        # awaitable. Pass it directly to the coordinator's durable transport.
        return handle, response
