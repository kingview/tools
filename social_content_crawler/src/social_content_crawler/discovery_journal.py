"""Atomic per-file exports and a data-only discovery checkpoint.

Checkpoints are not authorization and are not automatically replayed. A future
resume operation must revalidate ownership, parameters and the browser session.
"""
import csv
import io
import json
import os
from pathlib import Path
import uuid


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
