"""Make a self-contained visual storyboard and ID-bound revision patch form."""
from __future__ import annotations
import argparse
import base64
import copy
import hashlib
import html
import json
import re
import subprocess
import sys
from pathlib import Path
from revise_project import (PATCH_SCHEMA, COMMON_FIELDS, TYPE_FIELDS, authored_project,
                            generated_provenance, json_bytes, read_json, sha256_bytes,
                            validate_plan)


def visual_fingerprint(doc):
    """Build adds defaults and audio bindings; those do not change authored visuals."""
    profile = doc.get('drawPolicy', {}).get('profile', 'all-drawn')
    if profile == 'all-drawn':
        theme = copy.deepcopy(doc.get('theme', {}))
        theme.setdefault('paper', '#ffffff')
        theme.setdefault('dotSpacing', 0)
        theme.setdefault('ink', '#282b28')
    else:
        theme = copy.deepcopy(doc.get('theme', {'paper': '#fdfcf3', 'ink': '#282b28', 'dot': '#d5d8c8'}))
    groups = [{'id': 'global', 'startMs': 0, 'endMs': doc['durationMs'],
               'elements': copy.deepcopy(doc.get('elements', []))}]
    groups += copy.deepcopy(doc['scenes'])
    for scope in groups:
        for element in scope.get('elements', []):
            element.setdefault('startMs', 0)
            element.setdefault('durationMs', 0)
            element.setdefault('tool', 'eraser' if element.get('type') == 'erase'
                               else doc.get('defaultTool', 'pencil'))
    visual = {'durationMs': doc['durationMs'], 'canvas': doc.get('canvas', {'width': 1280, 'height': 720}),
              'fps': doc.get('fps', 30), 'defaultTool': doc.get('defaultTool', 'pencil'),
              'theme': theme, 'drawProfile': profile,
              'scopes': groups, 'cues': doc.get('cues', [])}
    return hashlib.sha256(json.dumps(visual, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def capture_previews(doc, source, compiled, output):
    if compiled is None:
        compiled = output/'preview-build'
        # Visual-only compilation reuses local illustrations and never synthesizes speech.
        subprocess.run([sys.executable, str(Path(__file__).with_name('build.py')),
                        '--project', str(source), '--output', str(compiled), '--silent'],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    baseline, _ = read_json(compiled/'source.project.json')
    if visual_fingerprint(doc) != visual_fingerprint(baseline):
        raise ValueError('Compiled preview is stale or belongs to another project; rebuild it from --project')
    import render
    from playwright.sync_api import sync_playwright
    snapshots = output/'scenes'
    snapshots.mkdir(parents=True, exist_ok=True)
    canvas = doc.get('canvas', {'width': 1280, 'height': 720})
    result = {}
    with render.serve(compiled) as base, sync_playwright() as runtime:
        browser = render.launch(runtime)
        try:
            page = browser.new_page(viewport=canvas, device_scale_factor=1)
            failures = []
            page.on('pageerror', lambda error: failures.append(str(error)))
            page.goto(base+'/index.html?export=1&timeline=timeline.json&hideTools=1', wait_until='load')
            page.evaluate('() => window.ready')
            if failures:
                raise ValueError('Preview failed: '+'; '.join(failures))
            for i, scene in enumerate(doc['scenes']):
                stamp = max(scene['startMs'], scene['endMs']-1)/1000
                name = f'{i+1:02d}-'+re.sub(r'[^a-zA-Z0-9_-]', '_', scene['id'])[:70]+'.jpg'
                picture = render.frame(page, stamp)
                path = snapshots/name
                path.write_bytes(picture)
                result[scene['id']] = {'path': path.relative_to(output).as_posix(), 'timeMs': stamp*1000,
                                       'dataUri': 'data:image/jpeg;base64,'+base64.b64encode(picture).decode('ascii')}
        finally:
            browser.close()
    return result, compiled


def scene_title(scene):
    if scene.get('title'):
        return str(scene['title'])
    texts = [element for element in scene.get('elements', []) if element.get('type') == 'text']
    title = next((element for element in texts if any(word in element['id'].lower()
                  for word in ('title', 'headline'))), texts[0] if texts else None)
    return title['text'] if title else scene['id']


def make_index(doc, source, raw, plan, plan_path, plan_raw, previews):
    scenes = copy.deepcopy(doc['scenes'])
    if doc.get('elements'):
        scenes.insert(0, {'id': 'global', 'title': '全局常驻对象', 'startMs': 0,
                          'endMs': doc['durationMs'], 'elements': doc['elements']})
    primary = {}
    for segment in (plan or {}).get('segments', []):
        if segment.get('sceneId'):
            primary[segment['id']] = segment['sceneId']
        else:
            primary[segment['id']] = max(doc['scenes'], key=lambda scene:
                max(0, min(scene['endMs'], segment['endMs'])-max(scene['startMs'], segment['startMs'])))['id']
    items = []
    for scene in scenes:
        elements = []
        for element in scene.get('elements', []):
            start = scene['startMs']+element.get('startMs', 0)
            length = 0 if element.get('instant') else element.get('durationMs', 0)
            label = element.get('label') or element.get('text') or element.get('drawGroup')
            label = label or (Path(element['src']).name if element.get('src') else element['type'])
            elements.append({'elementId': element['id'], 'type': element['type'], 'label': label,
                             'startMs': start, 'endMs': start+length,
                             'localStartMs': element.get('startMs', 0), 'durationMs': length,
                             'values': {key: copy.deepcopy(value) for key, value in element.items()
                                        if key in COMMON_FIELDS | TYPE_FIELDS[element['type']]},
                             'assetPaths': {key: element[key] for key in ('src', 'inkSrc', 'traceSrc') if key in element},
                             'editableFields': sorted(COMMON_FIELDS | TYPE_FIELDS[element['type']])})
        speech = []
        if scene['id'] != 'global':
            for segment in (plan or {}).get('segments', []):
                if segment['startMs'] < scene['endMs'] and segment['endMs'] > scene['startMs']:
                    speech.append({**copy.deepcopy(segment), 'segmentId': segment['id'],
                                   'primarySceneId': primary[segment['id']],
                                   'generatedProvenance': generated_provenance(segment)})
        items.append({'sceneId': scene['id'], 'title': scene_title(scene),
                      'startMs': scene['startMs'], 'endMs': scene['endMs'],
                      'narrationExcerpt': scene.get('narrationText'),
                      'labels': [e['values']['text'] for e in elements if e['type'] == 'text'],
                      'elements': elements, 'speech': speech,
                      'preview': {key: value for key, value in previews.get(scene['id'], {}).items() if key != 'dataUri'}})
    return {'schemaVersion': 'handdrawn-explainer/review/1', 'patchSchemaVersion': PATCH_SCHEMA,
            'sourceProject': str(source), 'baseProjectSha256': sha256_bytes(raw),
            'audioPlan': str(plan_path) if plan_path else None,
            'baseAudioPlanSha256': sha256_bytes(plan_raw) if plan_raw else None,
            'title': doc.get('title', source.stem), 'durationMs': doc['durationMs'],
            'canvas': doc.get('canvas', {'width': 1280, 'height': 720}), 'scenes': items}


def seconds(value):
    return f'{value/1000:.2f}s'


def markdown_cell(value):
    return str(value).replace('|', '\\|').replace('\n', ' ')


def markdown_review(index):
    lines = [f'# {index["title"]} · 可视分镜', '',
             f'原始项目：`{index["sourceProject"]}`', '',
             f'总时长：{seconds(index["durationMs"])}；补丁基线：`{index["baseProjectSha256"]}`。', '',
             '在 review.html 修改文字并下载补丁；通过 revise_project.py 应用后重新生成审阅帧。', '']
    for scene in index['scenes']:
        lines += [f'## {scene["title"]}', '',
                  f'幕 ID：`{scene["sceneId"]}` · {seconds(scene["startMs"])}–{seconds(scene["endMs"])}', '']
        if scene['preview']:
            lines += [f'![{scene["sceneId"]} 完整画面]({scene["preview"]["path"]})', '']
        lines += ['标签：'+('；'.join(scene['labels']) or '无文字标签'), '',
                  '| 对象 ID | 类型 | 内容 / 关键对象 | 全局绘制时间 |', '|---|---|---|---|']
        for element in scene['elements']:
            lines.append(f'| {markdown_cell(element["elementId"])} | {element["type"]} | '
                         f'{markdown_cell(element["label"])} | {seconds(element["startMs"])}–{seconds(element["endMs"])} |')
        lines += ['', '对应讲解：', '']
        if scene.get('narrationExcerpt'):
            lines += [scene['narrationExcerpt'], '', '录音绑定：', '']
        for speech in scene['speech']:
            if scene.get('narrationExcerpt') and speech['primarySceneId'] != scene['sceneId']:
                lines += [f'- `{speech["segmentId"]}` 连续录音跨幕使用；完整文本在 `{speech["primarySceneId"]}` 幕统一编辑。']
                continue
            lines += [f'- `{speech["segmentId"]}` · {seconds(speech["startMs"])}–{seconds(speech["endMs"])}：'
                      f'{speech.get("text", "（导入录音）")}']
            if speech.get('spokenText'):
                lines.append('  发音文本：'+speech['spokenText'])
        if not scene['speech']:
            lines.append('此幕未关联分段旁白。')
        lines.append('')
    return '\n'.join(lines)+'\n'


def html_review(index, previews):
    esc = lambda value: html.escape(str(value), quote=True)
    cards = []
    for scene in index['scenes']:
        sid = scene['sceneId']
        rows = []
        for element in scene['elements']:
            eid = element['elementId']
            detail = f'<span class="id">{esc(eid)}</span> · {esc(element["type"])} · {seconds(element["startMs"])}–{seconds(element["endMs"])}'
            if element['type'] == 'text':
                value = element['values']['text']
                rows.append(f'<label class="edit">{detail}<textarea data-target="visual" data-scene="{esc(sid)}" '
                            f'data-id="{esc(eid)}" data-field="text" data-original="{esc(value)}">{esc(value)}</textarea></label>')
            else:
                rows.append(f'<div class="object">{detail}<br><span class="muted">{esc(element["label"])}</span></div>')
        speech_rows = []
        if scene.get('narrationExcerpt'):
            speech_rows.append(f'<p>{esc(scene["narrationExcerpt"])}</p>')
        for speech in scene['speech']:
            segment_id = speech['segmentId']
            detail = f'<span class="id">{esc(segment_id)}</span> · {seconds(speech["startMs"])}–{seconds(speech["endMs"])}'
            if speech['primarySceneId'] != sid:
                body = '' if scene.get('narrationExcerpt') else esc(speech.get('text', '导入录音'))+'<br>'
                speech_rows.append(f'<p>{detail}<br>{body}<span class="muted">连续录音跨幕使用；在 {esc(speech["primarySceneId"])} 幕统一编辑完整文本。</span></p>')
                continue
            for field, name in [('text', '旁白 / 字幕文字'), ('spokenText', '发音文本')]:
                if field == 'spokenText' and field not in speech:
                    continue
                value = speech.get(field, '')
                speech_rows.append(f'<label class="edit">{detail} · {name}<textarea data-target="speech" data-scene="{esc(sid)}" '
                                   f'data-id="{esc(segment_id)}" data-field="{field}" data-original="{esc(value)}">{esc(value)}</textarea></label>')
            if speech.get('audioFile'):
                speech_rows.append(f'<p class="muted">当前音频：{esc(speech["audioFile"])}；'
                                   f'{"已标明生成来源" if speech["generatedProvenance"] else "未标明生成来源，改旁白时必须指定新音频或明确重新合成"}。</p>')
            speech_rows.append(f'<label class="check"><input type="checkbox" data-regenerate="{esc(segment_id)}"> '
                               '此段改稿后重新合成语音（不复用旧录音）</label>')
        preview = previews.get(sid)
        picture = f'<img class="preview" src="{preview["dataUri"]}" alt="{esc(scene["title"])}">' if preview else ''
        cards.append(f'<section><header><h2>{esc(scene["title"])}</h2><p><span class="id">{esc(sid)}</span> · '
                     f'{seconds(scene["startMs"])}–{seconds(scene["endMs"])}</p></header>{picture}'
                     f'<div class="columns"><div><h3>画面文字与对象</h3>{"".join(rows)}</div>'
                     f'<div><h3>旁白</h3>{"".join(speech_rows) or "<p class=muted>没有分段旁白。</p>"}</div></div></section>')
    embedded = json.dumps(index, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    template = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ · 可视分镜</title><style>
body{margin:0;background:#eeeee8;color:#263333;font:16px/1.5 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1120px;margin:auto;padding:24px}
h1,h2,h3{line-height:1.3}h1{font-size:28px}h2{font-size:22px;margin:0}h3{font-size:17px}.muted{color:#67736f;font-size:13px}
.toolbar{position:sticky;top:0;z-index:2;background:#fdfdf9eF;border-bottom:1px solid #cdd3cb;padding:14px 24px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
button{border:0;border-radius:8px;background:#336d60;color:white;padding:12px 18px;font-size:16px;cursor:pointer}.id{font-family:monospace;font-size:13px;color:#3a685a}
section{background:#fffef8;border-radius:12px;margin:24px 0;padding:22px;box-shadow:0 1px 5px #27352b18}header p{margin:8px 0 16px}.preview{display:block;width:100%;height:auto;border:1px solid #e0e0d5;border-radius:6px}
.columns{display:grid;grid-template-columns:1fr 1fr;gap:24px}.edit,.object{display:block;margin:12px 0;font-size:13px}.object{padding:8px;background:#f4f5ed;border-radius:5px}
textarea{display:block;width:100%;box-sizing:border-box;min-height:65px;font:inherit;font-size:16px;line-height:1.5;padding:9px;border:1px solid #c6cfc3;border-radius:5px;color:#263333;background:white;resize:vertical;margin-top:5px}.check{display:block;font-size:14px;margin:8px 0 18px}
textarea.changed{border:2px solid #b96b32}#status{font-size:14px}@media(max-width:760px){main{padding:12px}.columns{grid-template-columns:1fr}section{padding:14px}.toolbar{padding:10px}}
</style><div class="toolbar"><button id="download">下载修改补丁</button><span id="status">尚未修改</span></div><main><h1>__TITLE__ · 可视分镜</h1>
<p>修改画面或旁白文字，下载补丁后应用到原始项目。这里的预览保留原稿画面，应用改稿并重新生成审阅页后更新。</p>
<p class="muted">原始项目：__SOURCE__<br>补丁绑定当前文件内容；后续源稿变化会使旧补丁失效。缩略图已经包含在此 HTML 中，可离线查看。</p>__CARDS__</main>
<script type="application/json" id="review-index">__INDEX__</script><script>
const index=JSON.parse(document.getElementById('review-index').textContent);
const inputs=[...document.querySelectorAll('textarea[data-target]')];
function operations(){const grouped=new Map();for(const input of inputs){if(input.value===input.dataset.original)continue;
 const key=[input.dataset.target,input.dataset.scene,input.dataset.id].join('/');let op=grouped.get(key);
 if(!op){op={op:'set',target:input.dataset.target,sceneId:input.dataset.scene,set:{}};op[input.dataset.target==='visual'?'elementId':'segmentId']=input.dataset.id;grouped.set(key,op)}op.set[input.dataset.field]=input.value}
 for(const op of grouped.values()){if(op.target==='speech'){const checkbox=[...document.querySelectorAll('input[data-regenerate]')].find(x=>x.dataset.regenerate===op.segmentId);if(checkbox?.checked)op.regenerate=true}}
 return [...grouped.values()]}
function update(){for(const input of inputs)input.classList.toggle('changed',input.value!==input.dataset.original);document.getElementById('status').textContent=`${operations().length} 个对象待修改`}
inputs.forEach(input=>input.addEventListener('input',update));document.querySelectorAll('input[data-regenerate]').forEach(input=>input.addEventListener('change',update));
document.getElementById('download').addEventListener('click',()=>{const ops=operations();if(!ops.length){document.getElementById('status').textContent='请先修改文字';return}
 if(ops.some(op=>Object.values(op.set).some(value=>typeof value==='string'&&!value.trim()))){document.getElementById('status').textContent='文字不能为空';return}
 const patch={schemaVersion:index.patchSchemaVersion,baseProjectSha256:index.baseProjectSha256,operations:ops};if(ops.some(op=>op.target==='speech'))patch.baseAudioPlanSha256=index.baseAudioPlanSha256;
 const blob=new Blob([JSON.stringify(patch,null,2)+'\\n'],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='revision.patch.json';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);document.getElementById('status').textContent='补丁已下载；原始项目尚未修改'});
</script></html>'''
    return template.replace('__TITLE__', esc(index['title'])).replace('__SOURCE__', esc(index['sourceProject'])).replace('__CARDS__', ''.join(cards)).replace('__INDEX__', embedded)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path, help='Canonical authored project.json')
    parser.add_argument('--audio-plan', type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--compiled', type=Path, help='Existing matching compiled build; otherwise make a silent preview build')
    args = parser.parse_args()
    try:
        source, output = args.project.resolve(), args.output_dir.resolve()
        skill = Path(__file__).resolve().parents[1]
        if output == skill or skill in output.parents:
            raise ValueError('Write review artifacts outside the skill directory')
        doc, raw = read_json(source)
        authored_project(doc, source)
        plan_path = args.audio_plan.resolve() if args.audio_plan else None
        if plan_path is None and doc.get('audio', {}).get('plan'):
            plan_path = (source.parent/doc['audio']['plan']).resolve()
        plan, plan_raw = read_json(plan_path) if plan_path else (None, None)
        if plan is not None:
            validate_plan(plan, doc, plan_path)
        output.mkdir(parents=True, exist_ok=True)
        previews, compiled = capture_previews(doc, source, args.compiled.resolve() if args.compiled else None, output)
        index = make_index(doc, source, raw, plan, plan_path, plan_raw, previews)
        (output/'revision-index.json').write_bytes(json_bytes(index))
        (output/'review.md').write_text(markdown_review(index), encoding='utf-8')
        (output/'review.html').write_text(html_review(index, previews), encoding='utf-8')
        print(json.dumps({'review': str(output/'review.html'), 'markdown': str(output/'review.md'),
                          'index': str(output/'revision-index.json'), 'compiled': str(compiled),
                          'sceneCount': len(doc['scenes']), 'baseProjectSha256': index['baseProjectSha256']}, ensure_ascii=False))
    except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        parser.exit(2, f'Review failed: {detail}\n')


if __name__ == '__main__':
    main()
