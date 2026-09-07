"""Build an installable FineBOM folder using the pinned upstream scaffold tool."""
import argparse
import configparser
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


def adapter_source(dependency, supplied=None):
    source = Path(supplied).resolve() if supplied else ROOT / '.cache' / 'adapter' / dependency['commit']
    if not source.exists() and supplied:
        raise FileNotFoundError(source)
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=source.parent) as temporary:
            checkout = Path(temporary) / 'checkout'
            subprocess.run(['git', 'init', '--quiet', str(checkout)], check=True)
            git(checkout, 'fetch', '--quiet', '--depth=1', '--', dependency['repository'], dependency['commit'])
            git(checkout, 'checkout', '--quiet', '--detach', 'FETCH_HEAD')
            checkout.rename(source)
    if git(source, 'rev-parse', 'HEAD') != dependency['commit']:
        raise ValueError('Adapter checkout does not match adapter-dependency.json')
    if git(source, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('Adapter checkout contains local changes or untracked files')
    binary = source / 'dist' / 'DipTraceSchPluginAdapter.exe'
    if hashlib.sha256(binary.read_bytes()).hexdigest() != dependency['exe_sha256']:
        raise ValueError('Adapter EXE checksum does not match adapter-dependency.json')
    info = json.loads((source / 'build_info.json').read_text(encoding='utf-8'))
    if info['version'] != dependency['version'] or info['api_version'] != dependency['api_version']:
        raise ValueError('Adapter version or API does not match adapter-dependency.json')
    return source


def build(destination, supplied=None):
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(f'Destination already exists: {destination}; select a new build directory.')
    dependency = json.loads((ROOT / 'adapter-dependency.json').read_text())
    plugin_info = json.loads((ROOT / 'finebom' / 'build_info.json').read_text(encoding='utf-8'))
    version = plugin_info['version']
    source = adapter_source(dependency, supplied)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        stage = Path(temporary) / 'FineBOM'
        subprocess.run([sys.executable, str(source / 'tools' / 'new_plugin.py'), str(stage),
                        '--name', 'FineBOM', '--mode', 'job', '--plugin-version', version], check=True)
        for name in ('plugin.py', 'finebom.ini', 'adapter.ini', 'LICENSE', 'NOTICE.md', 'README.md', 'README_UA.md', 'CHANGELOG.md', 'CHANGELOG_UA.md'):
            shutil.copy2(ROOT / name, stage / name)
        for name in ('finebom', 'templates', 'docs', 'licenses'):
            shutil.copytree(ROOT / name, stage / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        # Restore the independent plugin version after copying its configuration.
        config = configparser.ConfigParser(interpolation=None)
        config.read(stage / 'adapter.ini', encoding='utf-8')
        config.set('plugin', 'version', version)
        with (stage / 'adapter.ini').open('w', encoding='utf-8') as stream:
            config.write(stream)
        settings = ET.parse(stage / 'settings.xml')
        settings.getroot().set('Hint', 'Generate an OpenDocument BOM from a spreadsheet template')
        settings.write(stage / 'settings.xml', encoding='utf-8', xml_declaration=True)
        # The upstream scaffold records a local checkout; supply its verified remote provenance.
        lock_path = stage / 'adapter.lock.json'
        lock = json.loads(lock_path.read_text())
        if lock['commit'] != dependency['commit']:
            raise ValueError('Generated dependency lock has an unexpected commit')
        if lock['adapter_version'] != dependency['version']:
            raise ValueError('Generated dependency lock has an unexpected adapter version')
        lock['repository'] = dependency['repository']
        lock_path.write_text(json.dumps(lock, indent=2) + '\n', encoding='utf-8')
        stage.rename(destination)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dest', type=Path, default=ROOT / 'build' / 'FineBOM')
    parser.add_argument('--adapter-source', type=Path, help='Clean checkout of the pinned commit, for offline builds')
    args = parser.parse_args()
    print(build(args.dest, args.adapter_source))
