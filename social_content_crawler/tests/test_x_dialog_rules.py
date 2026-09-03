import pytest

from social_content_crawler.x_dialog_rules import classify_dialog


@pytest.mark.parametrize('text,buttons,category,button',[
    ('可下载视频简介',['关闭','知道了'],'information','关闭'),
    ('Introducing downloadable videos',['Got it'],'information','Got it'),
    ("What's new on X",['OK'],'information','OK'),
    ('订阅 Premium\n免广告并查看更多数据',['立即订阅','暂不'],'promotion','暂不'),
    ('Subscribe to Premium',['Subscribe','No thanks'],'promotion','No thanks'),
    ('Turn on notifications',['Enable','Not now'],'notifications','Not now'),
    ('Get the X app',['Download','Skip'],'app_install','Skip'),
    ('开启通知',['关闭','开启'],'notifications','关闭'),
    ('未知新弹框',['关闭','知道了'],'unknown',None),
    ('Subscribe to Premium',['OK','Subscribe'],'promotion',None),
    ('可下载视频简介\n请先登录',['关闭','知道了'],'authentication',None),
    ('Verify your identity',['Close'],'authentication',None),
    ('确认丢弃草稿？',['取消','丢弃'],'draft_confirmation',None),
    ('Save draft?',['Close','Save'],'draft_confirmation',None),
    ('Subscribe to Premium\nConfirm purchase',['Cancel','Pay'],'payment_confirmation',None),
    ('订阅 Premium',['关闭','立即付款'],'payment_confirmation',None),
    ('Privacy choices',['Close','Accept all'],'settings_confirmation',None),
    ('Cookie 设置',['全部接受','拒绝'],'settings_confirmation',None),
    ('正文提到了可下载视频简介但没有对应标题',['关闭'],'unknown',None),
])
def test_dialog_classification(text,buttons,category,button):
    result=classify_dialog(text,buttons)
    assert result.category==category
    assert result.button_name==button


def test_recognized_title_does_not_override_interactive_form():
    result=classify_dialog('新功能介绍',['关闭'],has_form=True)
    assert result.category=='interactive_form' and result.button_name is None
