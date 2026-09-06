"""Atomic per-file exports and a data-only discovery checkpoint.

Checkpoints are not authorization. Reusing a task-scoped opaque key requires the
same request fingerprint and revalidates the current browser session separately.
"""
import csv
import io
import json
import os
from pathlib import Path
import uuid
from contextlib import contextmanager


def atomic_text(path,content,*,encoding='utf-8'):
    temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('x',encoding=encoding,newline='') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def export_links(posts,folder):
    folder=Path(folder); folder.mkdir(parents=True,exist_ok=True)
    atomic_text(folder/'links.txt','\n'.join(p['url'] for p in posts))
    atomic_text(folder/'metadata.json',json.dumps(posts,ensure_ascii=False,indent=2))
    stream=io.StringIO(newline='')
    writer=csv.DictWriter(stream,fieldnames=['url','post_id','author_name','published_at','text'])
    writer.writeheader()
    writer.writerows({key:post.get(key) for key in writer.fieldnames} for post in posts)
    atomic_text(folder/'links.csv',stream.getvalue(),encoding='utf-8-sig')


class DiscoveryJournal:
    def __init__(self,folder):
        self.folder=Path(folder)

    def save(self,state):
        export_links(state.ordered(),self.folder)
        # Write last: a durable data snapshot of the same exported selection.
        atomic_text(self.folder/'checkpoint.json',json.dumps(state.snapshot(),ensure_ascii=False,indent=2))

    def load(self,state):
        path=self.folder/'checkpoint.json'
        if not path.exists():
            return False
        with path.open('rb') as stream:
            data=stream.read(20*1024*1024+1)
        if len(data)>20*1024*1024:
            raise ValueError('采集检查点超过大小上限')
        state.restore(json.loads(data))
        return True


@contextmanager
def checkpoint_lock(folder):
    from .profile_tasks import _acquire_file_lock,_release_file_lock
    folder.mkdir(parents=True,exist_ok=True)
    with (folder/'.execution.lock').open('a+b') as stream:
        if not _acquire_file_lock(stream,0):
            raise ValueError('该采集任务正在运行，不能重复执行')
        try:
            yield
        finally:
            _release_file_lock(stream)
