"""Capture locally; Telegram credentials and delivery belong to the host."""
from datetime import datetime
from plugin_api import result, photos, message
import screen_capture


def handle(request, ctx):
    windows = screen_capture.codex_windows()
    displays = screen_capture.monitors()
    pictures, errors = [], []
    if not windows: errors.append('未找到 Codex 主窗口')
    if not displays: errors.append('未找到显示器')
    targets = [(dict(x, kind='monitor'), f"显示器 {x['id']}", len(windows)+i)
               for i,x in enumerate(displays,1)]
    targets += [(dict(x, kind='codex'), f'Codex 窗口 {i}', i) for i,x in enumerate(windows,1)]
    # Screens first: restoring minimized Codex must not change desktop snapshots.
    for target, label, order in targets:
        try:
            data, meta = screen_capture.capture(target)
            if len(data)>49*1024*1024: raise ValueError('PNG 超过文件限制')
            path=ctx.output_dir/f'{order}.png'; path.write_bytes(data)
            stamp=datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')
            pictures.append({'path':str(path),'width':meta['width'],'height':meta['height'],'order':order,
                'caption':f"📷 {label} · {meta['width']}×{meta['height']}\n{stamp}\n完整分辨率上传 · 打开照片查看"})
        except Exception as exc:
            errors.append(label+'：'+str(exc)[:160])
    pictures.sort(key=lambda x:x['order'])
    actions = [photos(pictures)] if pictures else []
    if errors: actions.append(message('未截取：\n\n'+'\n\n'.join(errors)))
    return result(*actions, ok=not errors)
