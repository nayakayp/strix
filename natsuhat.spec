# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

project_root = Path(SPECPATH)
natsuhat_root = project_root / 'natsuhat'

tui_name = 'natsuhat-tui.exe' if sys.platform == 'win32' else 'natsuhat-tui'
tui_binary = project_root / 'build' / 'sidecar' / tui_name
if not tui_binary.is_file():
    raise FileNotFoundError(
        f'Missing Go TUI sidecar at {tui_binary}; run `make tui-build` first'
    )
binaries = [(str(tui_binary), 'natsuhat/bin')]

datas = []

for md_file in natsuhat_root.rglob('skills/**/*.md'):
    rel_path = md_file.relative_to(project_root)
    datas.append((str(md_file), str(rel_path.parent)))

for jinja_file in natsuhat_root.rglob('agents/**/*.jinja'):
    rel_path = jinja_file.relative_to(project_root)
    datas.append((str(jinja_file), str(rel_path.parent)))

for xml_file in natsuhat_root.rglob('*.xml'):
    rel_path = xml_file.relative_to(project_root)
    datas.append((str(xml_file), str(rel_path.parent)))

# Prebuilt local-viewer SPA (served by `natsuhat view`).
viewer_static = natsuhat_root / 'interface' / 'viewer' / 'static'
for asset in viewer_static.rglob('*'):
    if asset.is_file():
        rel_path = asset.relative_to(project_root)
        datas.append((str(asset), str(rel_path.parent)))

datas += collect_data_files('tiktoken')
datas += collect_data_files('tiktoken_ext')

datas += collect_data_files('litellm')

datas += collect_data_files('agents', includes=['**/*.md', '**/*.jinja', '**/*.json'])

hiddenimports = [
    # Core dependencies
    'litellm',
    'litellm.llms',
    'litellm.llms.openai',
    'litellm.llms.anthropic',
    'litellm.llms.vertex_ai',
    'litellm.llms.bedrock',
    'litellm.utils',
    'litellm.caching',

    # Rich console
    'rich',
    'rich.console',
    'rich.panel',
    'rich.text',
    'rich.markup',
    'rich.style',
    'rich.align',
    'rich.live',

    # Pydantic
    'pydantic',
    'pydantic.fields',
    'pydantic_core',
    'email_validator',

    # Docker
    'docker',
    'docker.api',
    'docker.models',
    'docker.errors',

    # HTTP/Networking
    'httpx',
    'httpcore',
    'requests',
    'urllib3',
    'certifi',

    # Jinja2 templating
    'jinja2',
    'jinja2.ext',
    'markupsafe',

    # XML parsing
    'xmltodict',
    'defusedxml',
    'defusedxml.ElementTree',

    # Syntax highlighting
    'pygments',
    'pygments.lexers',
    'pygments.styles',
    'pygments.util',

    # Tiktoken (for token counting)
    'tiktoken',
    'tiktoken_ext',
    'tiktoken_ext.openai_public',

    # Tenacity retry
    'tenacity',

    # CVSS scoring
    'cvss',

    # Natsuhat modules
    'natsuhat',
    'natsuhat.interface',
    'natsuhat.interface.main',
    'natsuhat.interface.cli',
    'natsuhat.interface.tui',
    'natsuhat.interface.tui.runtime',
    'natsuhat.interface.tui.history',
    'natsuhat.interface.tui.live_view',
    'natsuhat.interface.tui.backend',
    'natsuhat.interface.tui.backend.controller',
    'natsuhat.interface.tui.backend.messages',
    'natsuhat.interface.tui.backend.protocol',
    'natsuhat.interface.tui.backend.server',
    'natsuhat.interface.utils',
    'natsuhat.agents',
    'natsuhat.agents.factory',
    'natsuhat.agents.prompt',
    'natsuhat.config.loader',
    'natsuhat.config.settings',
    'natsuhat.config.codex',
    'natsuhat.core',
    'natsuhat.core.agents',
    'natsuhat.core.execution',
    'natsuhat.core.inputs',
    'natsuhat.core.paths',
    'natsuhat.core.runner',
    'natsuhat.core.sessions',
    'natsuhat.report',
    'natsuhat.report.dedupe',
    'natsuhat.report.state',
    'natsuhat.report.writer',
    'natsuhat.interface.viewer',
    'natsuhat.interface.viewer.auth',
    'natsuhat.interface.viewer.cli',
    'natsuhat.interface.viewer.report_pdf',
    'natsuhat.interface.viewer.server',
    'natsuhat.interface.viewer.transcript',

    # PDF report generation + encryption
    'reportlab',
    'reportlab.pdfgen',
    'reportlab.pdfbase',
    'reportlab.lib',
    'reportlab.platypus',
    'pypdf',
    'cryptography',
    'natsuhat.runtime',
    'natsuhat.runtime.backends',
    'natsuhat.runtime.caido_bootstrap',
    'natsuhat.runtime.docker_client',
    'natsuhat.runtime.session_manager',
    'natsuhat.telemetry',
    'natsuhat.telemetry.logging',
    'natsuhat.telemetry.posthog',
    'natsuhat.tools',
    'natsuhat.tools.agents_graph.tools',
    'natsuhat.tools.finish.tool',
    'natsuhat.tools.notes.tools',
    'natsuhat.tools.proxy._calls',
    'natsuhat.tools.proxy.tools',
    'natsuhat.tools.python.tool',
    'natsuhat.tools.reporting.tool',
    'natsuhat.tools.thinking.tool',
    'natsuhat.tools.todo.tools',
    'natsuhat.tools.web_search.tool',
    'natsuhat.skills',
]

hiddenimports += collect_submodules('litellm')
hiddenimports += collect_submodules('google.auth')
hiddenimports += collect_submodules('google.oauth2')
hiddenimports += collect_submodules('rich')
hiddenimports += collect_submodules('pydantic')
hiddenimports += collect_submodules('pygments')
# reportlab loads renderers/fonts dynamically, so pull its whole tree in.
hiddenimports += collect_submodules('reportlab')

# reportlab ships bundled fonts (.pfb/.afm) it needs at runtime.
datas += collect_data_files('reportlab')

# reportlab imports PIL (pillow) lazily for image handling, so it must be
# bundled explicitly and kept out of the excludes list below.
hiddenimports += collect_submodules('PIL')
datas += collect_data_files('PIL')

excludes = [
    # Sandbox-only packages
    'playwright',
    'playwright.sync_api',
    'playwright.async_api',
    'IPython',
    'ipython',
    'libtmux',
    'pyte',
    'openhands_aci',
    'openhands-aci',
    'numpydoc',

    # Google Cloud / Vertex AI
    'google.cloud',
    'google.cloud.aiplatform',
    'google.api_core',
    'google.protobuf',
    'grpc',
    'grpcio',
    'grpcio_status',

    # Test frameworks
    'pytest',
    'pytest_asyncio',
    'pytest_cov',
    'pytest_mock',

    # Development tools
    'mypy',
    'ruff',
    'black',
    'isort',
    'pylint',
    'pyright',
    'bandit',
    'pre_commit',

    # Unnecessary for runtime
    'tkinter',
    'matplotlib',
    'numpy',
    'pandas',
    'scipy',
    'cv2',
]

a = Analysis(
    ['natsuhat/interface/main.py'],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='natsuhat',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
