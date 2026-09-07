"""FineBOM: OpenDocument BOM generation for DipTrace Schematic."""
import json
from pathlib import Path

_build_info = json.loads(Path(__file__).with_name('build_info.json').read_text(encoding='utf-8'))
__version__ = _build_info['version']
