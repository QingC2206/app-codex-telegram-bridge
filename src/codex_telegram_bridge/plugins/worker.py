"""One invocation per process. stdout is not a protocol channel; results use a file."""
import importlib.util
import json
from pathlib import Path
import sys
import traceback
from codex_telegram_bridge.plugins.api import Context
from . import api
from ..platforms import capture
# Compatibility for the documented v1 plugin API, limited to the worker.
sys.modules["plugin_api"] = api
sys.modules["screen_capture"] = capture


def main():
    request = json.loads(sys.stdin.buffer.read(262144).decode('utf-8'))
    output = Path(request['result_path'])
    folder = Path(request['plugin_dir'])
    try:
        sys.path.insert(0, str(folder))
        spec = importlib.util.spec_from_file_location('local_telegram_plugin', folder / request['entrypoint'])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ctx = Context(folder, Path(request['output_dir']), Path(request['state_dir']), request['config'])
        value = module.handle(request['request'], ctx)
        serialized = json.dumps(value, ensure_ascii=False)
        if len(serialized.encode('utf-8')) > 262144:
            raise ValueError('插件结果超过 256 KB')
        output.write_text(serialized, encoding='utf-8')
    except BaseException:
        # Captured by the host into a local diagnostic; never includes bot credentials.
        output.write_text(json.dumps({'error': traceback.format_exc()[-4000:]}), encoding='utf-8')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
