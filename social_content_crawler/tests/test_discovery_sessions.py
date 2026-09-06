from contextlib import contextmanager
import threading
from types import SimpleNamespace

import pytest

from social_content_crawler.discovery_sessions import RetainedDiscovery


def factory(events, closed):
    @contextmanager
    def create():
        events.append(('open', threading.get_ident()))
        try:
            yield SimpleNamespace(is_closed=lambda:False)
        finally:
            events.append(('close', threading.get_ident()))
            closed.set()
    return create


def test_reuses_same_thread_then_closes_owned_page():
    pool=RetainedDiscovery(idle_seconds=3); events=[]; closed=threading.Event()
    create=factory(events,closed)
    def operation(page,reused):
        events.append(('reuse' if reused else 'first',threading.get_ident()))
        return {'needs_human_review':not reused}
    assert pool.run('same',create,operation)['needs_human_review']
    assert not closed.is_set()
    assert not pool.run('same',create,operation)['needs_human_review']
    assert closed.wait(2)
    assert [name for name,_ in events]==['open','first','reuse','close']
    assert len({ident for _,ident in events})==1
    assert events[0][1]!=threading.get_ident()
    pool.close()


def test_expiry_capacity_and_exception_cleanup():
    pool=RetainedDiscovery(idle_seconds=.2,capacity=1); events=[]; closed=threading.Event()
    pool.run('one',factory(events,closed),lambda *a:{'needs_human_review':True})
    with pytest.raises(ValueError,match='匿名窗口'):
        pool.run('two',factory([],threading.Event()),lambda *a:{})
    assert closed.wait(2)
    def fail(*args):raise ValueError('browser disconnected')
    with pytest.raises(ValueError,match='disconnected'):
        pool.run('new',factory([],threading.Event()),fail)
    pool.close()
