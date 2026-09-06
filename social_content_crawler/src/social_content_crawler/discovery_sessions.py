"""Thread-owned anonymous pages retained only while awaiting human input.

Playwright sync objects never cross threads. No credentials or browser profiles
are persisted; application exit/idle expiry closes only these owned browsers.
"""
import atexit
import contextvars
import queue
import threading
import time
from concurrent.futures import Future


class _Session:
    def __init__(self, factory, idle_seconds):
        self.factory, self.idle_seconds = factory, idle_seconds
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.closing = False
        self.thread = threading.Thread(target=self.run, daemon=True, name='public-discovery-page')
        self.thread.start()

    def submit(self, operation):
        with self.lock:
            if self.closing:
                return None
            future = Future()
            self.queue.put((operation, contextvars.copy_context(), future))
            return future

    def stop(self):
        with self.lock:
            self.closing = True
            self.queue.put(None)

    def run(self):
        active = None
        try:
            with self.factory() as page:
                reused = False
                expires = time.monotonic() + self.idle_seconds
                while True:
                    try:
                        active = self.queue.get(timeout=max(.001,min(1,expires-time.monotonic())))
                    except queue.Empty:
                        # Serialize expiry with submit; a just-enqueued request
                        # is either processed or failed, never left hanging.
                        if time.monotonic() >= expires or page.is_closed():
                            break
                        continue
                    if active is None:
                        break
                    operation, context, future = active
                    result = context.run(operation, page, reused)
                    keep = bool(result.get('needs_human_review'))
                    if not keep:
                        with self.lock:
                            self.closing = True
                    future.set_result(result)
                    active = None
                    if not keep:
                        break
                    reused = True
                    expires = time.monotonic() + self.idle_seconds
        except BaseException as exc:
            from .diagnostics import record_exception
            record_exception('social-content', 'discovery.retained-page', exc)
            if active is not None and not active[2].done():
                active[2].set_exception(exc)
        finally:
            with self.lock:
                self.closing = True
                while not self.queue.empty():
                    pending = self.queue.get_nowait()
                    if pending is not None and not pending[2].done():
                        pending[2].set_exception(RuntimeError('匿名采集窗口已关闭，请继续原任务以重新连接检查点'))


class RetainedDiscovery:
    def __init__(self, *, idle_seconds=900, capacity=4):
        self.idle_seconds, self.capacity = idle_seconds, capacity
        self.sessions = {}
        self.lock = threading.Lock()

    def run(self, key, factory, operation):
        with self.lock:
            self.sessions = {k: v for k, v in self.sessions.items() if not v.closing}
            session = self.sessions.get(key)
            future = session.submit(operation) if session else None
            if future is None:
                if len(self.sessions) >= self.capacity:
                    raise ValueError('已有 4 个匿名窗口等待人工处理，请先完成这些任务或关闭后再试')
                session = _Session(factory, self.idle_seconds)
                self.sessions[key] = session
                future = session.submit(operation)
                if future is None:
                    raise RuntimeError('匿名浏览器启动失败，请检查 Chrome 安装与异常日志')
        return future.result()

    def close(self):
        with self.lock:
            sessions, self.sessions = list(self.sessions.values()), {}
        for session in sessions:
            session.stop()
        for session in sessions:
            session.thread.join(timeout=.2)


RETAINED_DISCOVERY = RetainedDiscovery()
atexit.register(RETAINED_DISCOVERY.close)
