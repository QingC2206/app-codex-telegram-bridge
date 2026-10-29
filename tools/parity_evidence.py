"""Read-only, shared definitions for regression and parity evidence fingerprints."""
import ast
import copy
import hashlib
import json
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def canonical_ast(node):
    """Ignore empty optional AST fields introduced by newer Python versions."""
    if isinstance(node, ast.AST):
        return [type(node).__name__, [[name, canonical_ast(value)]
                for name, value in ast.iter_fields(node) if value is not None and value != []]]
    if isinstance(node, list):
        return [canonical_ast(value) for value in node]
    if isinstance(node, bytes):
        return {'bytes': node.hex()}
    if isinstance(node, complex):
        return {'complex': repr(node)}
    if node is Ellipsis:
        return {'ellipsis': True}
    return node


def mapping_payload(ledger):
    # Result records are outputs, not inputs. Excluding these three envelopes
    # avoids a report -> ledger -> report hash cycle, never source exclusions.
    return {key: value for key, value in ledger.items()
            if key not in {'final_review', 'native_evidence', 'historical_evidence'}}


def source_manifest(root, ledger=None):
    root = Path(root)
    result = {}
    for folder in ('src', 'tests', 'tools', 'docs', '.github'):
        for path in (root / folder).rglob('*'):
            relative = path.relative_to(root).as_posix()
            if (not path.is_file() or '__pycache__' in path.parts
                    or path.suffix in {'.pyc', '.pyo'} or path.name == '.DS_Store'
                    or relative == 'docs/parity-tests.json'
                    or (relative.startswith('docs/parity-native-') and path.suffix == '.json')):
                continue
            if relative == 'docs/parity.json':
                contents = ledger if ledger is not None else json.loads(path.read_text())
                result[relative] = digest(mapping_payload(contents))
            else:
                result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in root.iterdir():
        if path.is_file() and (path.suffix in {'.py', '.json', '.toml', '.md', '.in', '.yml', '.yaml'}
                               or path.name in {'VERSION', 'LICENSE', '.gitignore', '.gitattributes'}):
            result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(result.items()))


def test_definitions(root):
    """Discover selectors without importing tests or touching a live profile."""
    definitions = {}
    for path in sorted((Path(root) / 'tests').rglob('test_*.py')):
        module = path.relative_to(Path(root) / 'tests').with_suffix('').as_posix().replace('/', '.')
        tree = ast.parse(path.read_text())
        support = copy.deepcopy(tree)
        for cls in support.body:
            if isinstance(cls, ast.ClassDef):
                cls.body = [node for node in cls.body if not (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'))]
        support_tree = canonical_ast(support)
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef):
                continue
            for node in cls.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'):
                    selector = '.'.join((module, cls.name, node.name))
                    definitions[selector] = digest([support_tree, canonical_ast(node)])
    return definitions
