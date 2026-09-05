"""Pure filtering, ordering and progress state; independent of browser handles."""
from dataclasses import dataclass,field
from datetime import datetime,timezone

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

    def add(self,posts):
        new=0
        for post in posts:
            if post['url'] in self.seen:
                self.duplicates+=1
                continue
            self.seen.add(post['url']); new+=1
            if accepted(post,self.request): self.selected.append(post)
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
            output_directory=str(folder),browser_engine=self.request.browser_engine,execution_mode=self.request.execution_mode)

    def snapshot(self):
        return dict(schema_version=1,posts=self.ordered(),seen_urls=sorted(self.seen),
                    duplicates=self.duplicates,filtered=self.filtered,stagnant=self.stagnant,
                    cursor=self.cursor,reason=self.reason,needs_human_review=self.needs_review)
