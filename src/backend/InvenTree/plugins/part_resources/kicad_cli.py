"""KiCad CLI backend.

This module is intentionally self contained:

- On Windows it looks for ``kicad-cli.exe`` in the standard KiCad install dirs.
- On Debian / Linux it uses ``shutil.which('kicad-cli')`` first,
  then common paths such as ``/usr/bin/kicad-cli``.
- The configured value may be a single executable path, or a full command
  prefix (e.g. ``flatpak run --command=kicad-cli org.kicad.KiCad``).

It builds temporary ``.kicad_pcb`` / ``.kicad_sch`` projects around the
uploaded ``.kicad_mod`` / ``.kicad_sym`` file and asks KiCad to export SVG.
The browser can display the resulting SVG directly.
"""

from __future__ import annotations

import io
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from PIL import Image

from . import render as _render

_DEFAULT_TIMEOUT = 90


class KicadCliError(RuntimeError):
    """Raised when kicad-cli is unavailable or returns an error."""


def _candidate_names():
    if os.name == 'nt':
        return ['kicad-cli.exe', 'kicad-cli']
    return ['kicad-cli']


def _windows_candidates():
    roots = [
        os.environ.get('ProgramFiles', r'C:\Program Files'),
        os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
    ]
    versions = ['10.0', '9.0', '8.0', '7.0', '6.0']
    return [
        str(Path(root) / 'KiCad' / version / 'bin' / 'kicad-cli.exe')
        for root in roots
        for version in versions
    ]


def _linux_candidates():
    return [
        '/usr/bin/kicad-cli',
        '/usr/local/bin/kicad-cli',
        '/snap/bin/kicad-cli',
        '/opt/kicad/bin/kicad-cli',
        '/var/lib/flatpak/exports/bin/org.kicad.KiCad',
    ]


def detect(preferred: str = ''):
    """Return a command prefix (list[str]) for kicad-cli, or None.

    ``preferred`` may be:
      - empty: auto detect
      - an executable path: ``C:\\Program Files\\KiCad\\10.0\\bin\\kicad-cli.exe``
      - a command prefix with arguments, e.g.
        ``flatpak run --command=kicad-cli org.kicad.KiCad``
    """

    preferred = (preferred or '').strip()

    if preferred:
        candidate = Path(preferred)
        if candidate.is_file():
            return [str(candidate)]

        found = shutil.which(preferred)
        if found:
            return [found]

        try:
            parts = shlex.split(preferred, posix=(os.name != 'nt'))
        except ValueError:
            parts = []
        if parts:
            first = parts[0]
            if Path(first).is_file() or shutil.which(first):
                return parts

    for name in _candidate_names():
        found = shutil.which(name)
        if found:
            return [found]

    if os.name == 'nt':
        candidates = _windows_candidates()
    else:
        candidates = _linux_candidates()

    for candidate in candidates:
        if Path(candidate).is_file():
            return [candidate]

    return None


def version(prefix) -> str:
    """Return the kicad-cli version string (best effort)."""
    for args in (['version'], ['--version']):
        try:
            result = _run(prefix, args, timeout=20)
        except Exception:
            continue
        output = (result.stdout or '') + (result.stderr or '')
        for line in output.splitlines():
            line = line.strip()
            if line:
                return line
    return ''


def _base_env(home: Path):
    env = os.environ.copy()
    env.setdefault('KICAD_CONFIG_HOME', str(home / 'kicad-config'))
    env.setdefault('KICAD_USER_TEMPLATE_DIR', str(home / 'kicad-templates'))
    env['HOME'] = str(home)
    if os.name != 'nt':
        env.setdefault('LANG', 'C.UTF-8')
    return env


def _run(prefix, args, cwd=None, home=None, timeout=_DEFAULT_TIMEOUT):
    if not prefix:
        raise KicadCliError('kicad-cli not found')

    home = Path(home or tempfile.gettempdir())
    cmd = list(prefix) + list(args)

    env = _base_env(home)
    executable = Path(str(prefix[0]))
    if executable.is_file():
        env['PATH'] = str(executable.parent) + os.pathsep + env.get('PATH', '')

    # On headless Debian, `pcb render` may need an X server.
    if os.name != 'nt' and not os.environ.get('DISPLAY'):
        xvfb = shutil.which('xvfb-run')
        if xvfb and any('render' in str(arg) for arg in args):
            cmd = [xvfb, '-a'] + cmd

    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise KicadCliError(f'kicad-cli not executable: {exc}') from exc
    except subprocess.TimeoutExpired as exc:
        raise KicadCliError('kicad-cli timed out') from exc

    return result


def _run_ok(prefix, args, cwd, home, timeout=_DEFAULT_TIMEOUT):
    result = _run(prefix, args, cwd=cwd, home=home, timeout=timeout)
    if result.returncode != 0:
        detail = ((result.stdout or '') + (result.stderr or '')).strip()
        raise KicadCliError(detail[-1500:] or 'kicad-cli command failed')
    return result


def _extract_symbol_node(text: str, ref=None):
    """Extract a top-level ``(symbol "name" ...)`` node and its name.

    When ``ref`` is provided, the symbol whose name matches it is returned;
    otherwise the first symbol in the file is used.
    """
    import re

    candidates = []
    pattern = re.compile(r'\(\s*symbol\s+"')

    for match in pattern.finditer(text):
        start = match.start()
        depth = 0
        in_string = False
        escaped = False

        for index in range(start, len(text)):
            ch = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == '\\':
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    node = text[start:index + 1]
                    parsed = _render.parse_sexpr(node)
                    name = None
                    if isinstance(parsed, list) and len(parsed) > 1:
                        name = str(parsed[1])
                    candidates.append((node, name))
                    break

    if not candidates:
        return None, None

    if ref:
        wanted = str(ref).split(':')[-1].lower()
        for node, name in candidates:
            if name and name.split(':')[-1].lower() == wanted:
                return node, name

    node, name = candidates[0]
    return node, (name or 'PartResourcesSymbol')


def _symbol_name(node_text: str):
    parsed = _render.parse_sexpr(node_text)
    if isinstance(parsed, list) and len(parsed) > 1 and isinstance(parsed[1], str):
        return parsed[1]
    return None


def _pin_numbers(symbol_node_text: str):
    parsed = _render.parse_sexpr(symbol_node_text)
    numbers = []
    if not isinstance(parsed, list):
        return numbers
    for pin in _render.find_all_deep(parsed, 'pin'):
        number = _render.find_one(pin, 'number')
        if number and len(number) > 1 and isinstance(number[1], str) and number[1]:
            if number[1] not in numbers:
                numbers.append(number[1])
    return numbers


def _new_uuid():
    return str(uuid.uuid4())


def _find_svg(output_dir: Path):
    svgs = sorted(Path(output_dir).glob('*.svg'))
    if not svgs:
        svgs = sorted(Path(output_dir).glob('**/*.svg'))
    return svgs[0].read_bytes() if svgs else None


def export_footprint_svg(footprint_bytes: bytes, layers=None, timeout=_DEFAULT_TIMEOUT, preferred='', file_name=None):
    """Render a ``.kicad_mod`` footprint to SVG using kicad-cli.

    KiCad 10+ has a direct ``fp export svg`` command. Older Debian/KiCad
    releases fall back to a temporary PCB + ``pcb export svg``.
    """
    prefix = detect(preferred)
    if not prefix:
        raise KicadCliError('kicad-cli not found')

    source = footprint_bytes.decode('utf-8', 'replace').strip()
    if not source:
        raise KicadCliError('empty footprint')

    direct_layers = [str(item) for item in (layers or []) if item]
    if not direct_layers:
        direct_layers = ['F.Cu', 'F.SilkS', 'F.Mask', 'F.Paste', 'F.Fab', 'F.CrtYd', 'Edge.Cuts']

    source_name = Path(file_name).name if file_name else 'part-resources.kicad_mod'
    if not source_name.lower().endswith('.kicad_mod'):
        source_name += '.kicad_mod'

    # --- KiCad 10+ direct footprint library export ---------------------
    try:
        with tempfile.TemporaryDirectory(prefix='kicad-fp-direct-') as direct_temp:
            direct_path = Path(direct_temp)
            home = direct_path / 'home'
            home.mkdir()
            library = direct_path / 'part-resources.pretty'
            library.mkdir()
            (library / source_name).write_text(source, encoding='utf-8')
            output_dir = direct_path / 'svg'
            output_dir.mkdir()

            if 'F.Fab' not in direct_layers:
                direct_layers.append('F.Fab')

            base_direct = [
                'fp', 'export', 'svg',
                '--output', str(output_dir),
                '--layers', ','.join(direct_layers),
            ]
            direct_variants = [
                base_direct + ['--sketch-pads-on-fab-layers', str(library)],
                base_direct + [str(library)],
            ]

            for direct_args in direct_variants:
                try:
                    _run_ok(prefix, direct_args, cwd=direct_path, home=home, timeout=timeout)
                except KicadCliError:
                    for old_svg in output_dir.glob('*.svg'):
                        old_svg.unlink(missing_ok=True)
                    continue
                svg = _find_svg(output_dir)
                if svg:
                    return svg
    except Exception:
        pass
    if not source:
        raise KicadCliError('empty footprint')

    # KiCad 6+ uses (footprint ...); old .mod files use (module ...)
    if source.startswith('(module '):
        source = '(footprint ' + source[len('(module '):]
    if not source.startswith('(footprint '):
        raise KicadCliError('unsupported footprint format')

    if '(layer ' not in source:
        source = source.replace('(footprint ', '(footprint ', 1)

    layer_names = [str(item) for item in (layers or []) if item]
    if not layer_names:
        layer_names = ['F.Cu', 'F.SilkS', 'F.Mask', 'F.Paste', 'F.Fab', 'F.CrtYd', 'Edge.Cuts']

    # Ensure the selected layers can be emitted even if the source footprint
    # does not declare them explicitly.
    board_layers = [
        '(0 "F.Cu" signal)',
        '(31 "B.Cu" signal)',
        '(34 "B.Paste" user)',
        '(35 "F.Paste" user)',
        '(36 "B.SilkS" user)',
        '(37 "F.SilkS" user)',
        '(38 "B.Mask" user)',
        '(39 "F.Mask" user)',
        '(40 "Dwgs.User" user)',
        '(41 "Cmts.User" user)',
        '(42 "Eco1.User" user)',
        '(43 "Eco2.User" user)',
        '(44 "Edge.Cuts" user)',
        '(45 "Margin" user)',
        '(46 "B.Fab" user)',
        '(47 "F.Fab" user)',
        '(48 "B.CrtYd" user)',
        '(49 "F.CrtYd" user)',
        '(50 "B.Adhes" user)',
        '(51 "F.Adhes" user)',
        '(52 "B.Adhesive" user)',
        '(53 "F.Adhesive" user)',
    ]

    with tempfile.TemporaryDirectory(prefix='kicad-fp-') as temp:
        temp_path = Path(temp)
        home = temp_path / 'home'
        home.mkdir()
        board_path = temp_path / 'part-resources.kicad_pcb'
        output_path = temp_path / 'footprint.svg'

        board = (
            '(kicad_pcb (version 20240108) (generator pcbnew)\n'
            '  (general (thickness 1.6))\n'
            '  (paper "A4")\n'
            '  (layers\n    ' + '\n    '.join(board_layers) + '\n  )\n'
            '  (setup (pad_to_mask_clearance 0))\n'
            '  (net 0 "")\n'
            '  ' + source + '\n'
            ')\n'
        )
        board_path.write_text(board, encoding='utf-8')

        base_args = [
            'pcb', 'export', 'svg',
            '--layers', ','.join(layer_names),
            '--output', str(output_path),
            str(board_path),
        ]

        attempts = [
            base_args,
            [item for item in base_args if item != '--page-size-mode'],  # harmless placeholder
        ]

        last_error = ''
        for args in attempts:
            try:
                _run_ok(prefix, args, cwd=temp_path, home=home, timeout=timeout)
            except KicadCliError as exc:
                last_error = str(exc)
                continue
            if output_path.is_file():
                return output_path.read_bytes()

        # Some versions ignore the file extension and create a directory.
        svgs = list(temp_path.glob('**/*.svg'))
        if svgs:
            return svgs[0].read_bytes()

        raise KicadCliError(last_error or 'kicad-cli produced no footprint SVG')


def export_symbol_svg(symbol_bytes: bytes, layers=None, timeout=_DEFAULT_TIMEOUT, preferred='', ref=None):
    """Render a ``.kicad_sym`` symbol to SVG using ``kicad-cli sch export svg``."""
    prefix = detect(preferred)
    if not prefix:
        raise KicadCliError('kicad-cli not found')

    text = symbol_bytes.decode('utf-8', 'replace')
    symbol_node, symbol_name = _extract_symbol_node(text, ref=ref)
    if not symbol_node or not symbol_name:
        raise KicadCliError('could not parse symbol node')

    # --- KiCad 10+ direct symbol export --------------------------------
    try:
        with tempfile.TemporaryDirectory(prefix='kicad-sym-direct-') as direct_temp:
            direct_path = Path(direct_temp)
            home = direct_path / 'home'
            home.mkdir()
            source_path = direct_path / 'part-resources.kicad_sym'
            source_path.write_text(text, encoding='utf-8')
            output_dir = direct_path / 'svg'
            output_dir.mkdir()

            variants = [
                ['sym', 'export', 'svg', '--output', str(output_dir),
                 '--symbol', symbol_name, '--include-hidden-pins', str(source_path)],
                ['sym', 'export', 'svg', '--output', str(output_dir),
                 '--symbol', symbol_name, str(source_path)],
                ['sym', 'export', 'svg', '--output', str(output_dir), str(source_path)],
            ]

            for direct_args in variants:
                try:
                    _run_ok(prefix, direct_args, cwd=direct_path, home=home, timeout=timeout)
                except KicadCliError:
                    continue
                svg = _find_svg(output_dir)
                if svg:
                    return svg
    except Exception:
        pass

    if '(extends ' in symbol_node:
        raise KicadCliError('derived symbols (extends) are not supported, use base symbol')

    pin_numbers = _pin_numbers(symbol_node)
    lib_id = f'PartResources:{symbol_name}'
    renamed_node = symbol_node.replace(f'(symbol "{symbol_name}"', f'(symbol "{lib_id}"', 1)

    root_uuid = _new_uuid()
    symbol_uuid = _new_uuid()
    project_name = 'part-resources'

    pin_entries = ''.join(f'    (pin "{number}" (uuid "{_new_uuid()}"))\n' for number in pin_numbers)

    sch = (
        '(kicad_sch (version 20231120) (generator eeschema)\n'
        f'  (uuid "{root_uuid}")\n'
        '  (paper "A4")\n'
        '  (lib_symbols\n'
        f'{renamed_node}\n'
        '  )\n'
        '  (symbol\n'
        f'    (lib_id "{lib_id}")\n'
        '    (at 148.5 105 0)\n'
        '    (unit 1)\n'
        '    (exclude_from_sim no)\n'
        '    (in_bom yes)\n'
        '    (on_board yes)\n'
        '    (dnp no)\n'
        f'    (uuid "{symbol_uuid}")\n'
        '    (property "Reference" "U" (at 148.5 70 0)\n'
        '      (effects (font (size 1.27 1.27)))\n'
        '    )\n'
        '    (property "Value" "' + symbol_name.replace('"', '\\"') + '" (at 148.5 73 0)\n'
        '      (effects (font (size 1.27 1.27)))\n'
        '    )\n'
        '    (property "Footprint" "" (at 148.5 105 0)\n'
        '      (effects (font (size 1.27 1.27)) hide)\n'
        '    )\n'
        '    (property "Datasheet" "" (at 148.5 105 0)\n'
        '      (effects (font (size 1.27 1.27)) hide)\n'
        '    )\n'
        f'{pin_entries}'
        '    (instances\n'
        f'      (project "{project_name}"\n'
        f'        (path "/{root_uuid}" (reference "U") (unit 1))\n'
        '      )\n'
        '    )\n'
        '  )\n'
        '  (sheet_instances (path "/" (page "1")))\n'
        ')\n'
    )

    with tempfile.TemporaryDirectory(prefix='kicad-sch-') as temp:
        temp_path = Path(temp)
        home = temp_path / 'home'
        home.mkdir()
        sch_path = temp_path / 'part-resources.kicad_sch'
        output_path = temp_path / 'symbol.svg'
        sch_path.write_text(sch, encoding='utf-8')

        output_dir = temp_path / 'svg-output'
        output_dir.mkdir(parents=True, exist_ok=True)

        variants = [
            ['sch', 'export', 'svg', '--output', str(output_path), str(sch_path)],
            ['sch', 'export', 'svg', '--output', str(output_dir), str(sch_path)],
        ]

        last_error = ''
        for args in variants:
            try:
                _run_ok(prefix, args, cwd=temp_path, home=home, timeout=timeout)
            except KicadCliError as exc:
                last_error = str(exc)
                continue

            if output_path.is_file():
                return output_path.read_bytes()

            svgs = list(temp_path.glob('**/*.svg'))
            if svgs:
                return svgs[0].read_bytes()

        raise KicadCliError(last_error or 'kicad-cli produced no symbol SVG')


def _find_rsvg(prefix=None):
    """Find rsvg-convert (Debian: librsvg2-bin)."""
    found = shutil.which('rsvg-convert')
    if found:
        return found

    if prefix:
        executable = Path(str(prefix[0]))
        if executable.is_file():
            for name in ('rsvg-convert.exe', 'rsvg-convert'):
                candidate = executable.parent / name
                if candidate.is_file():
                    return str(candidate)

    return None


def _fit_square(image: Image.Image, size: int):
    """Fit an image into a square, preserving aspect ratio."""
    image = image.convert('RGB')
    image.thumbnail((size, size), Image.LANCZOS)
    canvas = Image.new('RGB', (size, size), (250, 250, 250))
    canvas.paste(
        image,
        ((size - image.width) // 2, (size - image.height) // 2),
    )
    return canvas


def svg_to_image(svg_bytes: bytes, size: int = 256, prefix=None, timeout=60):
    """Rasterize an SVG to a square PIL image.

    Uses ``rsvg-convert`` when available (Debian package: librsvg2-bin),
    then falls back to the optional ``cairosvg`` Python package.
    """
    if not svg_bytes:
        return None

    size = max(32, int(size))

    # 1) rsvg-convert
    rsvg = _find_rsvg(prefix)
    if rsvg:
        try:
            with tempfile.TemporaryDirectory(prefix='kicad-raster-') as temp:
                temp_path = Path(temp)
                svg_path = temp_path / 'input.svg'
                png_path = temp_path / 'output.png'
                svg_path.write_bytes(svg_bytes)

                png_size = size * 3
                result = subprocess.run(
                    [
                        rsvg, '--width', str(png_size), '--height', str(png_size),
                        '--keep-aspect-ratio',
                        '--background-color', '#fafafa',
                        '--output', str(png_path),
                        str(svg_path),
                    ],
                    capture_output=True,
                    timeout=timeout,
                )
                if result.returncode == 0 and png_path.is_file():
                    return _fit_square(Image.open(png_path), size)
        except Exception:
            pass

    # 2) optional cairosvg
    try:
        import cairosvg

        png_bytes = cairosvg.svg2png(
            bytestring=svg_bytes,
            output_width=size * 3,
            background_color='#fafafa',
        )
        return _fit_square(Image.open(io.BytesIO(png_bytes)), size)
    except Exception:
        pass

    return None


def render_thumbnail(file_name: str, data: bytes, size: int = 256, layers=None,
                     timeout=_DEFAULT_TIMEOUT, preferred=''):
    """Render a KiCad file to a cached square thumbnail using official CLI + rsvg."""
    svg = render_preview(
        file_name,
        data,
        layers=layers,
        timeout=timeout,
        preferred=preferred,
    )
    if not svg:
        return None
    return svg_to_image(svg, size=size, prefix=detect(preferred), timeout=timeout)


def render_preview(file_name: str, data: bytes, layers=None, timeout=_DEFAULT_TIMEOUT, preferred=''):
    """Render an uploaded KiCad file to SVG bytes."""
    name = (file_name or '').lower()
    ref = Path(file_name or '').stem if file_name else None

    if name.endswith(('.kicad_mod', '.pretty', '.mod')):
        return export_footprint_svg(data, layers=layers, timeout=timeout, preferred=preferred)
    if name.endswith('.kicad_sym'):
        return export_symbol_svg(
            data, layers=layers, timeout=timeout, preferred=preferred, ref=ref
        )
    raise KicadCliError('unsupported file type')
