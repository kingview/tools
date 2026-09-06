from pathlib import Path

import numpy as np
from PIL import Image

from media_content_analyzer.material_inspection import inspect_file, _sha


def picture(path):
    pixels=np.random.default_rng(12).integers(20,235,(256,256,3),dtype=np.uint8)
    Image.fromarray(pixels).save(path)
    return path


def clean(_):
    return {'watermark_confident':False,'uncertain':False,'quality_issues':[]}


def test_clean_image_preserves_source(tmp_path):
    source=picture(tmp_path/'source.png')
    before=source.read_bytes()
    report=inspect_file(source,tmp_path/'work',checker=clean)
    assert report['passed'] and report['candidate_path']==str(source)
    assert report['sha256']==_sha(source)
    assert source.read_bytes()==before


def test_watermark_repaired_copy_and_rechecked(tmp_path):
    source=picture(tmp_path/'source.png'); before=source.read_bytes()
    calls=[]
    def checker(path):
        calls.append(path)
        return clean(path) if len(calls)>1 else {'watermark_confident':True,'uncertain':False,
            'quality_issues':[],'watermark_regions':[{'x':.8,'y':.8,'width':.1,'height':.1}]}
    report=inspect_file(source,tmp_path/'work',checker=checker)
    assert report['passed'] and len(calls)==2
    assert Path(report['candidate_path'])!=source
    assert report['actions']==['图片水印修复并复检']
    assert source.read_bytes()==before


def test_uncertainty_or_model_failure_never_passes(tmp_path):
    source=picture(tmp_path/'source.png')
    assert not inspect_file(source,tmp_path/'work',checker=lambda _: {**clean(None),'uncertain':True})['passed']
    def unavailable(_): raise TimeoutError('model timeout')
    report=inspect_file(source,tmp_path/'work',checker=unavailable)
    assert not report['passed'] and report['issues']==['model timeout']


def test_low_quality_or_invalid_files_not_admitted(tmp_path):
    source=tmp_path/'plain.png'; Image.new('RGB',(256,256),(255,255,255)).save(source)
    assert not inspect_file(source,tmp_path/'work',checker=clean)['passed']
    broken=tmp_path/'broken.png'; broken.write_bytes(b'not an image')
    assert not inspect_file(broken,tmp_path/'work',checker=clean)['passed']


def test_large_mask_does_not_modify_image(tmp_path):
    source=picture(tmp_path/'source.png'); before=source.read_bytes()
    checker=lambda _: {'watermark_confident':True,'uncertain':False,'quality_issues':[],
        'watermark_regions':[{'x':0,'y':0,'width':.9,'height':.9}]}
    report=inspect_file(source,tmp_path/'work',checker=checker)
    assert not report['passed'] and source.read_bytes()==before


def test_phases_follow_real_repair_and_recheck_and_never_claim_admission(tmp_path):
    source = picture(tmp_path/'source.png')
    phases, checks = [], []
    def checker(path):
        checks.append(path)
        if len(checks) == 1:
            assert phases[-1][0] == '检查中'
            return {**clean(path), 'watermark_confident': True,
                    'watermark_regions': [{'x': .8, 'y': .8, 'width': .1, 'height': .1}]}
        assert phases[-1][0] == '待重新检查'
        return clean(path)
    result = inspect_file(source, tmp_path/'work', checker=checker,
                          on_progress=lambda *args: phases.append(args))
    assert result['passed']
    assert [phase[0] for phase in phases] == ['检查中', '处理中', '待重新检查']
    assert result['found_issues'] == ['检测到水印']
    assert phases[-1][1:] == (['检测到水印'], ['图片去水印候选已生成'])


def test_clean_image_does_not_emit_fake_repair_phases(tmp_path):
    phases = []
    assert inspect_file(picture(tmp_path/'source.png'), tmp_path/'work', checker=clean,
                        on_progress=lambda *args: phases.append(args))['passed']
    assert [phase[0] for phase in phases] == ['检查中']


def test_failed_recheck_retains_repair_evidence(tmp_path):
    calls = []
    def checker(path):
        calls.append(path)
        return {**clean(path), 'watermark_confident': True,
                'watermark_regions': [{'x': .8, 'y': .8, 'width': .1, 'height': .1}]}
    result = inspect_file(picture(tmp_path/'source.png'), tmp_path/'work', checker=checker)
    assert not result['passed'] and result['actions'] == ['图片去水印候选已生成']
    assert result['issues'] == ['图片修复后复检未通过']
