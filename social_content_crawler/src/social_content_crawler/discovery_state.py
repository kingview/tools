"""Pure filtering, ordering and progress state; independent of browser handles."""
from dataclasses import dataclass,field
from datetime import datetime,timezone
import hashlib
import json

from .public_materials import accepted
from .discovery_contract import utc


def published_key(post):
    try:
        return utc(datetime.fromisoformat(str(post.get('published_at') or '').replace('Z','+00:00')))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


@dataclass
class DiscoveryState:
    request: object
    selected: list = field(default_factory=list)
    seen: set = field(default_factory=set)
    duplicates: int = 0
    filtered: int = 0
    stagnant: int = 0
    reason: str = 'exhausted'
    needs_review: bool = False
    warnings: list = field(default_factory=list)
    cursor: str | None = None
    scroll_count: int = 0
    account_candidates: list = field(default_factory=list)
    selected_account_url: str | None = None
    as_of: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def add(self,posts):
        new=0
        for post in posts:
            if post['url'] in self.seen:
                self.duplicates+=1
                continue
            self.seen.add(post['url']); new+=1
            if accepted(post,self.request,now=self.as_of): self.selected.append(post)
            else: self.filtered+=1
        self.stagnant=0 if new else self.stagnant+1

    @property
    def reached_target(self):
        return len(self.selected)>=self.request.max_items

    def ordered(self):
        posts=self.selected
        if self.request.sort=='likes':
            posts=sorted(posts,key=lambda p:p.get('metrics',{}).get('likes') or 0,reverse=True)
        elif self.request.sort=='latest':
            posts=sorted(posts,key=published_key,reverse=True)
        return posts[:self.request.max_items]

    def result(self,folder):
        posts=self.ordered()
        warnings=list(self.warnings)
        if self.request.sort=='likes': warnings.append('点赞排序依据本次可访问结果，不代表平台全量排名。')
        if not self.reached_target and not self.needs_review:
            warnings.append('符合筛选条件的可访问链接不足，已保留当前结果。未知日期不视为通过时间筛选。')
        return dict(posts=posts,count=len(posts),requested=self.request.max_items,found=len(self.seen),
            skipped_duplicates=self.duplicates,filtered_out=self.filtered,completed=self.reached_target,
            needs_human_review=self.needs_review,completion_reason=self.reason,warnings=warnings,
            output_directory=str(folder),browser_engine=self.request.browser_engine,execution_mode=self.request.execution_mode,
            account_candidates=self.account_candidates,selected_account_url=self.selected_account_url)

    def snapshot(self):
        return dict(schema_version=2,request_fingerprint=self.fingerprint(),as_of=self.as_of.isoformat(),
                    scroll_count=self.scroll_count,posts=self.ordered(),seen_urls=sorted(self.seen),
                    duplicates=self.duplicates,filtered=self.filtered,stagnant=self.stagnant,
                    cursor=self.cursor,reason=self.reason,needs_human_review=self.needs_review,
                    account_candidates=self.account_candidates,selected_account_url=self.selected_account_url)

    def fingerprint(self):
        parameters=self.request.model_dump(mode='json')
        # Keep pre-account-lookup version-2 checkpoints valid for ID/URL tasks.
        if parameters.get('account_kind') == 'id': parameters.pop('account_kind',None)
        value=json.dumps(parameters,sort_keys=True,separators=(',',':'))
        return hashlib.sha256(value.encode()).hexdigest()

    def restore(self,snapshot):
        if snapshot.get('schema_version')!=2 or snapshot.get('request_fingerprint')!=self.fingerprint():
            raise ValueError('采集检查点与原参数不匹配，不能混用其他任务；请新建任务')
        self.as_of=utc(datetime.fromisoformat(snapshot['as_of']))
        posts=snapshot['posts']; seen=snapshot['seen_urls']
        if not isinstance(posts,list) or len(posts)>500 or not isinstance(seen,list) or not all(isinstance(u,str) for u in seen):
            raise ValueError('采集检查点损坏')
        for post in posts:
            if not isinstance(post,dict) or post.get('url') not in seen or not accepted(post,self.request,now=self.as_of):
                raise ValueError('采集检查点包含无效结果')
        self.selected=list(posts); self.seen=set(seen)
        from .discovery_accounts import normalize_candidates
        candidates=snapshot.get('account_candidates',[])
        if not isinstance(candidates,list) or normalize_candidates(candidates,self.request.platform)!=candidates:
            raise ValueError('账号候选检查点损坏')
        self.account_candidates=candidates
        self.selected_account_url=snapshot.get('selected_account_url')
        if self.selected_account_url and self.selected_account_url not in {c['url'] for c in candidates}:
            raise ValueError('已确认账号不在候选列表中')
        self.cursor=snapshot.get('cursor')
        if self.cursor is not None and (not isinstance(self.cursor,str) or not self.cursor.isdigit()):
            raise ValueError('采集游标无效')
        for key in ('scroll_count','duplicates','filtered'):
            value=snapshot.get(key,0)
            if type(value) is not int or value<0 or (key=='scroll_count' and value>500):
                raise ValueError('采集检查点计数无效')
            setattr(self,key,value)
        # A retry receives a new bounded deadline/stagnation allowance. It does
        # not assume that a previously blocked page has become accessible.
        self.reason='target_reached' if self.reached_target else 'exhausted'
