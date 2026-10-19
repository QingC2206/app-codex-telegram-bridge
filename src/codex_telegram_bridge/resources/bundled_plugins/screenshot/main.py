"""Capture locally; Telegram credentials and delivery belong to the host."""
from codex_telegram_bridge.i18n import tr as _t
from datetime import datetime
from plugin_api import result, photos, message
import screen_capture


def handle(request, ctx):
    windows = screen_capture.codex_windows()
    displays = screen_capture.monitors()
    pictures, errors = [], []
    if not windows and not any(x.get('portal') for x in displays): errors.append(_t('未找到 Codex 主窗口'))
    if not displays: errors.append(_t('未找到显示器'))
    targets = [(dict(x, kind='monitor'), _t('显示器 {p0}', p0=format(x['id'], '')), len(windows)+i)
               for i,x in enumerate(displays,1)]
    targets += [(dict(x, kind='codex'), _t('Codex 窗口 {p0}', p0=format(i, '')) + (_t('（可见区域）') if x.get('visible_region') else ''), i) for i,x in enumerate(windows,1)]
    # Screens first: restoring minimized Codex must not change desktop snapshots.
    for target, label, order in targets:
        try:
            data, meta = screen_capture.capture(target)
            if len(data)>49*1024*1024: raise ValueError(_t('PNG 超过文件限制'))
            path=ctx.output_dir/f'{order}.png'; path.write_bytes(data)
            stamp=datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')
            pictures.append({'path':str(path),'width':meta['width'],'height':meta['height'],'order':order,
                'caption':_t('📷 {p0} · {p1}×{p2}\n{p3}\n完整分辨率上传 · 打开照片查看', p0=format(label, ''), p1=format(meta['width'], ''), p2=format(meta['height'], ''), p3=format(stamp, ''))})
        except Exception as exc:
            errors.append(label+'：'+str(exc)[:160])
    pictures.sort(key=lambda x:x['order'])
    actions = [photos(pictures)] if pictures else []
    if errors: actions.append(message(_t('未截取：\n\n')+'\n\n'.join(errors)))
    return result(*actions, ok=not errors)
