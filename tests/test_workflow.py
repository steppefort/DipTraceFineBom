"""Public adapter API integration; no legacy launcher or private CAD fixtures."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from finebom.core import BomError, Config, NS, Template, cell_text
from xml.dom import minidom
from finebom.workflow import run
from finebom import __version__
from test_engine import fixture


class ContextDouble:
    """Only the public context methods used by FineBOM; no process environment scan."""
    mode = 'job'
    plugin_id = 'FineBOM'
    plugin_version = __version__
    adapter_version = '0.1.1'

    def __init__(self, plugin, source, project, run_dir, variables):
        self.plugin_dir, self.exchange_path = plugin, source
        self.project_dir, self.run_dir = project, run_dir
        self.values = variables
        self.requested = []
        self.messages = []

    def read_xml(self):
        return self.exchange_path.read_bytes()

    def environment(self, names):
        self.requested = list(names)
        return {k.casefold(): v for k, v in self.values.items() if k.casefold() in names}

    def log(self, message):
        self.messages.append(message)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.plugin = self.base / 'plugin'; self.plugin.mkdir()
        shutil.copy2(ROOT / 'finebom.ini', self.plugin / 'finebom.ini')
        shutil.copytree(ROOT / 'templates', self.plugin / 'templates')
        self.project = self.base / 'project'; self.project.mkdir()
        self.run_dir = self.base / 'capture'; self.run_dir.mkdir()
        self.source = self.run_dir / 'exchange.xml'
        tree = fixture(self.source)
        schematic = tree.find('Schematic')
        schematic.remove(schematic.find('EnvironmentVariables'))
        ET.SubElement(ET.SubElement(schematic, 'Settings'), 'ProjectDir').text = str(self.project)
        ET.ElementTree(tree).write(self.source, encoding='utf-8')
        self.ctx = ContextDouble(self.plugin, self.source, self.project, self.run_dir,
                                 {'projectname': 'Board01', 'revision': '2', 'Company': 'Компанія',
                                  'author': 'Engineer', 'root_proj': 'Main'})
        self.now = datetime(2026, 9, 6, 21, 20, tzinfo=timezone.utc)

    def test_direct_context_company_diagnostics_and_collision(self):
        before = self.source.read_bytes()
        with patch.dict(os.environ, {'Company': 'WRONG', 'projectname': 'WRONG'}, clear=True):
            report = run(self.ctx, now=self.now)
            second = run(self.ctx, now=self.now)
        self.assertEqual(Path(report['output_path']).name, 'Board01v2bom06.09.2026_21-20.ods')
        self.assertEqual(Path(second['output_path']).name, 'Board01v2bom06.09.2026_21-20_2.ods')
        self.assertEqual(report['environment_values']['company'], 'Компанія')
        self.assertIn('company', self.ctx.requested)
        self.assertNotIn('datetime', self.ctx.requested)
        self.assertEqual(report['unresolved_environment'], [])
        self.assertEqual(report['component_count'], 7)
        self.assertEqual(report['bom_row_count'], 4)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual({p.suffix for p in (self.project / 'Docs').iterdir()}, {'.ods'})
        self.assertTrue((self.run_dir / 'report.json').is_file())
        self.assertFalse((self.run_dir / 'environment.json').exists())
        with zipfile.ZipFile(report['output_path']) as z:
            xml = z.read('content.xml').decode()
            self.assertIn('Компанія', xml)
            self.assertIn('20260906 21:20', " ".join(cell_text(c) for c in minidom.parseString(xml).getElementsByTagNameNS(NS["table"], "table-cell")))
            self.assertNotIn('&lt;Company&gt;', xml)

    def test_environment_precedence(self):
        (self.project / 'finebom.project.ini').write_text('[environment]\nauthor=Project fallback\ncustom=extra\n')
        cfg = self.plugin / 'finebom.ini'
        cfg.write_text(cfg.read_text().replace('[environment]\n', '[environment]\nauthor=Plugin fallback\n'))
        report = run(self.ctx, now=self.now)
        self.assertEqual(report['environment_values']['author'], 'Engineer')
        self.assertEqual(report['environment_values']['custom'], 'extra')
        tree = ET.parse(self.source)
        env = ET.SubElement(tree.find('Schematic'), 'EnvironmentVariables')
        ET.SubElement(env, 'Variable', Name='author', Value='XML author')
        tree.write(self.source)
        report = run(self.ctx, now=self.now)
        self.assertEqual(report['environment_values']['author'], 'XML author')

    def test_mixed_plugin_versions_are_rejected_before_output(self):
        self.ctx.plugin_version = '0.1.0'
        with self.assertRaisesRegex(BomError, 'Reinstall the complete FineBOM folder'):
            run(self.ctx, now=self.now)
        self.assertFalse((self.project / 'Docs').exists())

    def test_missing_variables_strict_no_output(self):
        del self.ctx.values['Company']
        cfg = self.plugin / 'finebom.ini'
        cfg.write_text(cfg.read_text().replace('missing = keep', 'missing = error'))
        with self.assertRaises(BomError): run(self.ctx, now=self.now)
        self.assertEqual(list((self.project / 'Docs').iterdir()), [])

    def test_unavailable_project_not_silently_redirected(self):
        self.ctx.project_dir = self.project / 'missing'
        with self.assertRaises(BomError): run(self.ctx, now=self.now)
        self.assertFalse((self.run_dir / 'Docs').exists())

    def test_template_layout_and_stripes(self):
        report = run(self.ctx, now=self.now)
        self.assertEqual(report['template_row_count'], 2)
        ns = NS
        with zipfile.ZipFile(self.plugin / 'templates/BOM.ots') as z:
            before = ET.fromstring(z.read('styles.xml'))
        with zipfile.ZipFile(report['output_path']) as z:
            after = ET.fromstring(z.read('styles.xml'))
            content = ET.fromstring(z.read('content.xml'))
        properties = './/style:page-layout-properties'
        self.assertEqual([n.attrib for n in before.findall(properties, ns)],
                         [n.attrib for n in after.findall(properties, ns)])
        for header in after.findall('.//style:header', ns):
            self.assertEqual(header.get('{'+ns['style']+'}display'), 'false')
        self.assertTrue(after.findall('.//text:page-number', ns))
        self.assertTrue(after.findall('.//text:page-count', ns))
        table = content.find('.//table:table', ns)
        rows = table.findall('table:table-row', ns)
        idx = next(i for i, row in enumerate(rows) if 'RefDes' in ''.join(row.itertext()))
        data = rows[idx+1:idx+5]
        key = '{'+ns['table']+'}style-name'
        styles = [[c.get(key) for c in r.findall('table:table-cell', ns)] for r in data]
        self.assertEqual(styles[0], styles[2])
        self.assertEqual(styles[1], styles[3])
        self.assertNotEqual(styles[0], styles[1])
        definitions = {s.get('{'+ns['style']+'}name'): s for s in content.findall('.//style:style', ns)}
        for row in styles:
            for name in row:
                style = definitions.get(name)
                if style is not None:
                    cell = style.find('style:table-cell-properties', ns)
                    self.assertIsNotNone(cell)
                    self.assertTrue(any('border' in k for k in cell.attrib))


class AdapterIntegrationTests(unittest.TestCase):
    def test_real_adapter_job_and_lock(self):
        built = Path(os.environ.get('FINEBOM_TEST_PLUGIN', ROOT / 'build' / 'FineBOM'))
        self.assertTrue((built / '.adapter/host.py').is_file(), 'Build FineBOM before running the integration test.')
        dependency = json.loads((ROOT / 'adapter-dependency.json').read_text())
        lock = json.loads((built / 'adapter.lock.json').read_text())
        self.assertEqual(lock['commit'], dependency['commit'])
        self.assertEqual(lock['repository'], dependency['repository'])
        self.assertEqual(lock['adapter_version'], dependency['version'])
        for name, digest in lock['files'].items():
            self.assertEqual(hashlib.sha256((built / name).read_bytes()).hexdigest(), digest, name)
        manifest = ET.parse(built / 'settings.xml').getroot()
        self.assertEqual(manifest.get('Name'), f'FineBOM {__version__}')
        self.assertEqual(manifest.get('ExeFile'), 'FineBOM.exe')
        self.assertEqual(manifest.findtext('Settings/ImpMode'), 'None')
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            plugin = base / 'FineBOM'; shutil.copytree(built, plugin)
            config = plugin / 'adapter.ini'
            config.write_text(config.read_text().replace('capture_dir =', 'capture_dir = '+str(base/'captures')))
            source = base / 'exchange.xml'; tree = fixture(source)
            schematic = tree.find('Schematic')
            schematic.remove(schematic.find('EnvironmentVariables'))
            ET.SubElement(ET.SubElement(schematic, 'Settings'), 'ProjectDir').text = str(base)
            ET.ElementTree(tree).write(source, encoding='utf-8')
            original = source.read_bytes()
            env = dict(os.environ, projectname='Integration', revision='1', author='Engineer', Company='Example Co', root_proj='Main')
            subprocess.run([sys.executable, str(plugin/'.adapter/host.py'), '--config', str(config), '--exchange', str(source)],
                           env=env, check=True, timeout=15)
            capture = Path(json.loads((base/'captures/latest.json').read_text())['run_dir'])
            deadline = time.monotonic()+15
            while time.monotonic()<deadline:
                status = json.loads((capture/'status.json').read_text())
                if status['status'] != 'starting': break
                time.sleep(.05)
            self.assertEqual(status['status'], 'ok', status)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(len(list((base/'Docs').iterdir())), 1)
            self.assertFalse((capture/'result.xml').exists())
            selected = json.loads((capture/'selected_environment.json').read_text())
            self.assertEqual(selected['company'], 'Example Co')
            self.assertNotIn('PATH', selected)
            report = json.loads((capture/'report.json').read_text())
            self.assertEqual(report['environment_origins']['company'], 'adapter')
            self.assertEqual(report['plugin_version'], __version__)
            self.assertEqual(report['adapter_version'], dependency['version'])
            context = json.loads((capture/'context.json').read_text())
            self.assertEqual(context['plugin_id'], 'FineBOM')
            self.assertEqual(context['plugin_version'], __version__)
            self.assertEqual(context['adapter_version'], dependency['version'])
            self.assertIn(f'FineBOM {__version__}', (capture/'plugin.log').read_text())
            self.assertIn(f'DipTraceSchPluginAdapter {dependency["version"]}',
                          (capture/'worker.log').read_text())
            with zipfile.ZipFile(report['output_path']) as ods:
                metadata = ET.fromstring(ods.read('meta.xml'))
                self.assertEqual(metadata.findtext('.//{urn:oasis:names:tc:opendocument:xmlns:meta:1.0}generator'),
                                 f'DipTrace FineBOM {__version__}')
            # A worker failure must reach adapter status.json; never report a successful empty BOM.
            (plugin/'finebom.ini').write_text('[plugin]\ntemplate=missing.ots\n')
            subprocess.run([sys.executable,str(plugin/'.adapter/host.py'),'--config',str(config),'--exchange',str(source)],env=env,check=True,timeout=15)
            failed=Path(json.loads((base/'captures/latest.json').read_text())['run_dir'])
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                status=json.loads((failed/'status.json').read_text())
                if status['status']!='starting':break
                time.sleep(.05)
            self.assertEqual(status['status'],'error',status)
            self.assertEqual(len(list((base/'Docs').iterdir())),1)
            self.assertEqual(source.read_bytes(),original)


if __name__ == '__main__': unittest.main()
