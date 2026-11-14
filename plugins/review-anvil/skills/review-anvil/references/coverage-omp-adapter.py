"""Load in Python Eval and inject its real `agent` global. No OMP imports required.

    adapter = OMPNativeAdapter(agent)
    summary = CoverageCoordinator(coverage, adapter, run_id, context, workers=4).run()

Every persistent Eval invocation must use timeout:0. Agent starts and waits occur
on Eval's calling thread, never an executor thread or asyncio.run().
"""
from coverage_coordinator import WorkerFailure


class OMPNativeAdapter:
    def __init__(self, spawn):
        if not callable(spawn):
            raise TypeError("OMP Eval's real agent callable is required")
        self.spawn = spawn
        self._waited = set()

    def start(self, request):
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
        for handle in handles:
            try:
                if handle.done():
                    break
            except Exception as exc:
                raise WorkerFailure(handle, exc) from exc
        else:
            handle = handles[0]
        identity = self.identity(handle)
        if identity in self._waited:
            raise WorkerFailure(handle, RuntimeError("OMP handle must be waited exactly once"))
        self._waited.add(identity)
        try:
            response = handle.wait()
        except BaseException as exc:
            raise WorkerFailure(handle, exc) from exc
        # .wait() already returns parsed structured output, not another handle or
        # awaitable. Pass it directly to the coordinator's durable transport.
        return handle, response
