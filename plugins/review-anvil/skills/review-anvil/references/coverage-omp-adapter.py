"""Load in Python Eval and inject its real `agent` global. No OMP imports required.

    adapter = OMPNativeAdapter(agent)
    summary = CoverageCoordinator(coverage, adapter, run_id, context, workers=4).run()

Every persistent Eval invocation must use timeout:0. Starts remain on Eval's
calling thread. Independent receives copy its context, as native handle awaiting
does, so one slow handle cannot let completed siblings expire.
"""
# AgentHandle.wait() resolves OMP's global `wait` in Python Eval.
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as wait_for_futures
from contextvars import copy_context
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
        self._receiving = {}
        self._executor = None

    def begin_wave(self):
        """Start one shared stage budget, never a fresh budget per handle."""
        self.deadline = monotonic() + self.timeout

    def end_wave(self):
        """Release receivers without blocking or discarding unharvested output."""
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None

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
    def done(self, handle):
        receiving = self._receiving.get(self.identity(handle))
        return receiving.done() if receiving is not None else handle.done()

    @staticmethod
    def cancel(handle):
        # A cancellation request is not a fabricated reviewer response.
        return handle.cancel()


    def _receive(self, handle):
        remaining = self._remaining()
        if remaining <= 0 and not handle.done():
            raise TimeoutError("OMP native wave deadline expired")
        return handle.wait(timeout=remaining)

    def wait_one(self, handles):
        if not handles:
            raise ValueError("At least one live OMP handle is required")
        self._remaining()  # initialize the shared deadline on the calling thread
        for handle in handles:
            if self.identity(handle) in self._waited:
                raise WorkerFailure(handle, RuntimeError("OMP handle must be waited exactly once"))
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=len(handles))
        # Receive every live handle, including ready siblings, before returning
        # one value for durable storage. Persistence must not delay their receipt.
        for handle in handles:
            identity = self.identity(handle)
            if identity not in self._receiving:
                self._receiving[identity] = self._executor.submit(
                    copy_context().run, self._receive, handle)
        wait_for_futures([self._receiving[self.identity(handle)] for handle in handles],
                         return_when=FIRST_COMPLETED)
        handle = next(handle for handle in handles
                      if self._receiving[self.identity(handle)].done())
        identity = self.identity(handle)
        receiving = self._receiving.pop(identity)
        self._waited.add(identity)
        try:
            response = receiving.result()
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
        finally:
            if not self._receiving:
                self.end_wave()
        # .wait() already returns parsed structured output, not another handle or
        # awaitable. Pass it directly to the coordinator's durable transport.
        return handle, response
