"""Screen capture for macOS and X11 via MSS; no network or credentials."""
import struct


def monitors():
    import mss
    with mss.mss() as screen:
        return [{'id': i, 'rect': [x['left'], x['top'], x['left']+x['width'], x['top']+x['height']]}
                for i,x in enumerate(screen.monitors[1:],1)]


def image_result(data):
    if data[:8] != b'\x89PNG\r\n\x1a\n' or len(data) < 24 or len(data) > 49*1024*1024:
        raise ValueError('Invalid or oversized screenshot PNG.')
    width,height = struct.unpack('>II',data[16:24])
    if not 0 < width*height <= 64_000_000:
        raise ValueError('Screenshot exceeds pixel limit.')
    return data, {'width':width, 'height':height}


def capture_rect(rect):
    import mss
    from mss.tools import to_png
    x,y,right,bottom = rect
    if not 0 < (right-x)*(bottom-y) <= 64_000_000:
        raise ValueError('Invalid screenshot area.')
    with mss.mss() as screen:
        image = screen.grab({'left':x,'top':y,'width':right-x,'height':bottom-y})
        return image_result(to_png(image.rgb,image.size))
