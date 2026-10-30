"""Bounded source observations, never proof of live reader/writer completeness."""
import ast
from pathlib import Path


def sink_calls(root: Path, paths: list[str]) -> list[dict]:
    """Enumerate a fixed conservative sink-name policy without importing source.

    String replace calls are retained as false positives. Dynamic calls and
    other runtimes remain outside this finite source observation.
    """
    calls = []

    class Scan(ast.NodeVisitor):
        def __init__(self, path):
            self.path = path
            self.functions = []

        def visit_FunctionDef(self, node):
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else (
                function.id if isinstance(function, ast.Name) else '')
            filesystem = name in {'unlink', 'rename', 'replace', 'write_text',
                                  'write_bytes', 'atomic_write_text', 'atomic_write_bytes'}
            shutil = (isinstance(function, ast.Attribute)
                      and isinstance(function.value, ast.Name)
                      and function.value.id in {'shutil', '_shutil'}
                      and name in {'move', 'rmtree'})
            database = (isinstance(function, ast.Attribute)
                        and isinstance(function.value, ast.Name)
                        and function.value.id == 'db'
                        and name in {'insert_virtual_item', 'delete_virtual_item',
                                     'delete_virtual_item_by_strm_path',
                                     'upsert_monitored_series', 'update_monitored_series'})
            if filesystem or shutil or database:
                calls.append({'path': self.path, 'line': node.lineno,
                              'function': '.'.join(self.functions),
                              'call': ast.unparse(function)})
            self.generic_visit(node)

    for path in sorted(paths):
        relative = Path(path)
        if relative.is_absolute() or '..' in relative.parts or relative.suffix != '.py':
            raise ValueError('Inventory requires explicit relative Python files')
        Scan(path).visit(ast.parse((root / relative).read_text(), filename=path))
    return sorted(calls, key=lambda item: (item['path'], item['line'], item['call']))
