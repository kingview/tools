"""Conservative, independently testable decisions for X web dialogs."""
from __future__ import annotations

from dataclasses import dataclass
import re


def _pattern(value: str) -> re.Pattern:
    return re.compile(value, re.IGNORECASE)


_CLOSE = _pattern(r"^(?:关闭|關閉|Close|Dismiss|暂不|暫不|稍后再说|稍後再說|以后再说|Not now|Maybe later|No thanks|不用了|不，谢谢|不，謝謝|跳过|略過|Skip|Cancel|取消)$")
_ACK = _pattern(r"^(?:知道了|明白了|Got it!?|OK|Okay)$")
_SECURITY = _pattern(r"登录|登入|验证码|驗證|验证身份|确认身份|安全检查|账号异常|账户异常|账号已锁定|账号被冻结|帳戶已鎖定|sign[ -]?in|log[ -]?in|captcha|verify (?:your|you)|verification|authenticate|account (?:locked|suspended)|security check")
_DRAFT = _pattern(r"(?:丢弃|舍弃|放弃|删除|保存|捨棄|刪除|儲存).{0,8}(?:草稿|帖子|貼文)|(?:discard|delete|save).{0,12}(?:draft|post)|unsaved changes")
_PAYMENT = _pattern(r"确认(?:支付|付款|购买|订阅)|確認(?:付款|購買|訂閱)|支付信息|付款信息|信用卡|credit card|payment details|confirm (?:purchase|payment|subscription)|complete your purchase")
_PAY_BUTTON = _pattern(r"^(?:付款|支付|立即付款|确认购买|Pay(?: now)?|Confirm purchase|Subscribe and pay)$")
_SETTINGS = _pattern(r"隐私设置|隱私設定|隐私选择|修改设置|更改设置|privacy (?:settings|choices)|change (?:your )?settings|cookies?|数据共享|data sharing")
_RULES = (
    ("information", _pattern(r"^(?:可下载视频简介|可下載影片簡介|Introducing downloadable videos|Downloadable videos|新功能介绍|新功能上线|功能更新|X 新功能(?:介绍)?|What[’']s new(?: on X)?)$")),
    ("promotion", _pattern(r"^(?:(?:订阅|訂閱|升级到|升級至|体验|體驗)\s*(?:X\s*)?Premium(?:\+)?|(?:Try|Get|Subscribe to|Upgrade to)\s+(?:X\s*)?Premium(?:\+)?)$")),
    ("notifications", _pattern(r"^(?:开启通知|打开通知|启用通知|開啟通知|Turn on notifications|Enable notifications)$")),
    ("app_install", _pattern(r"^(?:获取\s*X\s*应用|下载\s*X\s*应用|安装\s*X|Get the X app|Download the X app|Install X)$")),
)


@dataclass(frozen=True)
class DialogDecision:
    category: str
    button_name: str | None = None


def classify_dialog(text: str, buttons: list[str], *, has_form: bool = False) -> DialogDecision:
    # An onboarding title alone never overrides login/payment/settings checks.
    for category, pattern in (("authentication", _SECURITY), ("draft_confirmation", _DRAFT),
                              ("payment_confirmation", _PAYMENT), ("settings_confirmation", _SETTINGS)):
        if pattern.search(text):
            return DialogDecision(category)
    if any(_PAY_BUTTON.fullmatch(label.strip()) for label in buttons):
        return DialogDecision("payment_confirmation")
    if has_form:
        return DialogDecision("interactive_form")
    # X uses generic div/span titles. Check only the first non-button lines.
    labels = {label.strip() for label in buttons}
    titles = [line.strip() for line in text.splitlines() if line.strip() and line.strip() not in labels][:3]
    for category, title_pattern in _RULES:
        if any(title_pattern.fullmatch(title) for title in titles):
            for label in buttons:
                if _CLOSE.fullmatch(label.strip()):
                    return DialogDecision(category, label)
            if category == "information":
                for label in buttons:
                    if _ACK.fullmatch(label.strip()):
                        return DialogDecision(category, label)
            return DialogDecision(category)
    return DialogDecision("unknown")
