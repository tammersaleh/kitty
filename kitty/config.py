#!/usr/bin/env python
# vim:fileencoding=utf-8
# License: GPL v3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

import json
import os
import re
import sys
from collections import namedtuple
from contextlib import contextmanager

from . import fast_data_types as defines
from .conf.definition import as_conf_file
from .conf.utils import (
    init_config, key_func, load_config as _load_config, merge_dicts,
    parse_config_base, positive_float, positive_int, python_string, to_bool,
    to_cmdline, to_color, unit_float
)
from .config_data import all_options
from .constants import cache_dir, defconf
from .fast_data_types import CURSOR_BEAM, CURSOR_BLOCK, CURSOR_UNDERLINE
from .layout import all_layouts
from .rgb import color_as_int, color_from_int
from .utils import log_error

cshapes = {
    'block': CURSOR_BLOCK,
    'beam': CURSOR_BEAM,
    'underline': CURSOR_UNDERLINE
}


def to_cursor_shape(x):
    try:
        return cshapes[x.lower()]
    except KeyError:
        raise ValueError(
            'Invalid cursor shape: {} allowed values are {}'.format(
                x, ', '.join(cshapes)
            )
        )


mod_map = {'CTRL': 'CONTROL', 'CMD': 'SUPER', '⌘': 'SUPER', '⌥': 'ALT', 'OPTION': 'ALT', 'KITTY_MOD': 'KITTY'}


def parse_mods(parts, sc):

    def map_mod(m):
        return mod_map.get(m, m)

    mods = 0
    for m in parts:
        try:
            mods |= getattr(defines, 'GLFW_MOD_' + map_mod(m.upper()))
        except AttributeError:
            log_error('Shortcut: {} has unknown modifier, ignoring'.format(sc))
            return

    return mods


named_keys = {
    "'": 'APOSTROPHE',
    ',': 'COMMA',
    '-': 'MINUS',
    '.': 'PERIOD',
    '/': 'SLASH',
    ';': 'SEMICOLON',
    '=': 'EQUAL',
    '[': 'LEFT_BRACKET',
    ']': 'RIGHT_BRACKET',
    '`': 'GRAVE_ACCENT'
}


def parse_shortcut(sc):
    parts = sc.split('+')
    mods = parse_mods(parts[:-1], sc)
    if mods is None:
        return None, None
    key = parts[-1].upper()
    key = getattr(defines, 'GLFW_KEY_' + named_keys.get(key, key), None)
    if key is not None:
        return mods, key
    return mods, None


KeyAction = namedtuple('KeyAction', 'func args')
func_with_args, args_funcs = key_func()


@func_with_args(
    'pass_selection_to_program', 'new_window', 'new_tab', 'new_os_window',
    'new_window_with_cwd', 'new_tab_with_cwd', 'new_os_window_with_cwd'
    )
def shlex_parse(func, rest):
    return func, to_cmdline(rest)


@func_with_args('combine')
def combine_parse(func, rest):
    sep, rest = rest.split(' ', 1)
    parts = re.split(r'\s*' + re.escape(sep) + r'\s*', rest)
    args = tuple(map(parse_key_action, filter(None, parts)))
    return func, args


@func_with_args('send_text')
def send_text_parse(func, rest):
    args = rest.split(' ', 1)
    if len(args) > 0:
        try:
            args[1] = parse_send_text_bytes(args[1])
        except Exception:
            log_error('Ignoring invalid send_text string: ' + args[1])
            args[1] = ''
    return func, args


@func_with_args('run_kitten', 'run_simple_kitten', 'kitten')
def kitten_parse(func, rest):
    if func == 'kitten':
        args = rest.split(' ', 1)
    else:
        args = rest.split(' ', 2)[1:]
        func = 'kitten'
    return func, args


@func_with_args('goto_tab')
def goto_tab_parse(func, rest):
    args = (max(0, int(rest)), )
    return func, args


@func_with_args('set_background_opacity', 'goto_layout', 'kitty_shell')
def simple_parse(func, rest):
    return func, [rest]


@func_with_args('set_font_size')
def float_parse(func, rest):
    return func, (float(rest),)


@func_with_args('change_font_size')
def parse_change_font_size(func, rest):
    vals = rest.split(' ', 1)
    if len(vals) != 2:
        log_error('Invalid change_font_size specification: {}, treating it as default'.format(rest))
        args = [True, None, 0]
    else:
        args = [vals[0].lower() == 'all', None, 0]
        amt = vals[1]
        if amt[0] in '+-':
            args[1] = amt[0]
            amt = amt[1:]
        args[2] = float(amt)
    return func, args


def parse_key_action(action):
    parts = action.split(' ', 1)
    func = parts[0]
    if len(parts) == 1:
        return KeyAction(func, ())
    rest = parts[1]
    parser = args_funcs.get(func)
    if parser is not None:
        try:
            func, args = parser(func, rest)
        except Exception:
            log_error('Ignoring invalid key action: {}'.format(action))
    return KeyAction(func, args)


all_key_actions = set()
sequence_sep = '>'


class KeyDefinition:

    def __init__(self, is_sequence, action, mods, key, rest=()):
        self.is_sequence = is_sequence
        self.action = action
        self.trigger = mods, key
        self.rest = rest

    def resolve(self, kitty_mod):
        self.trigger = defines.resolve_key_mods(kitty_mod, self.trigger[0]), self.trigger[1]
        self.rest = tuple((defines.resolve_key_mods(kitty_mod, mods), key) for mods, key in self.rest)


def parse_key(val, key_definitions):
    sc, action = val.partition(' ')[::2]
    sc, action = sc.strip().strip(sequence_sep), action.strip()
    if not sc or not action:
        return
    is_sequence = sequence_sep in sc
    if is_sequence:
        trigger = None
        rest = []
        for part in sc.split(sequence_sep):
            mods, key = parse_shortcut(part)
            if key is None:
                if mods is not None:
                    log_error('Shortcut: {} has unknown key, ignoring'.format(sc))
                return
            if trigger is None:
                trigger = mods, key
            else:
                rest.append((mods, key))
        rest = tuple(rest)
    else:
        mods, key = parse_shortcut(sc)
        if key is None:
            if mods is not None:
                log_error('Shortcut: {} has unknown key, ignoring'.format(sc))
            return
    try:
        paction = parse_key_action(action)
    except Exception:
        log_error('Invalid shortcut action: {}. Ignoring.'.format(
            action))
    else:
        if paction is not None:
            all_key_actions.add(paction.func)
            if is_sequence:
                key_definitions.append(KeyDefinition(True, paction, trigger[0], trigger[1], rest))
            else:
                key_definitions.append(KeyDefinition(False, paction, mods, key))


def parse_symbol_map(val):
    parts = val.split(' ')
    symbol_map = {}

    def abort():
        log_error('Symbol map: {} is invalid, ignoring'.format(
            val))
        return {}

    if len(parts) < 2:
        return abort()
    family = ' '.join(parts[1:])

    def to_chr(x):
        if not x.startswith('U+'):
            raise ValueError()
        x = int(x[2:], 16)
        return x

    for x in parts[0].split(','):
        a, b = x.partition('-')[::2]
        b = b or a
        try:
            a, b = map(to_chr, (a, b))
        except Exception:
            return abort()
        if b < a or max(a, b) > sys.maxunicode or min(a, b) < 1:
            return abort()
        symbol_map[(a, b)] = family
    return symbol_map


def parse_send_text_bytes(text):
    return python_string(text).encode('utf-8')


def parse_send_text(val, key_definitions):
    parts = val.split(' ')

    def abort(msg):
        log_error('Send text: {} is invalid ({}), ignoring'.format(
            val, msg))
        return {}

    if len(parts) < 3:
        return abort('Incomplete')
    mode, sc = parts[:2]
    text = ' '.join(parts[2:])
    key_str = '{} send_text {} {}'.format(sc, mode, text)
    return parse_key(key_str, key_definitions)


def to_modifiers(val):
    return parse_mods(val.split('+'), val) or 0


def uniq(vals, result_type=list):
    seen = set()
    seen_add = seen.add
    return result_type(x for x in vals if x not in seen and not seen_add(x))


def to_layout_names(raw):
    parts = [x.strip().lower() for x in raw.split(',')]
    ans = []
    for p in parts:
        if p == '*':
            ans.extend(sorted(all_layouts))
            continue
        name = p.partition(':')[0]
        if name not in all_layouts:
            raise ValueError('The window layout {} is unknown'.format(p))
        ans.append(p)
    return uniq(ans)


def adjust_line_height(x):
    if x.endswith('%'):
        return float(x[:-1].strip()) / 100.0
    return int(x)


def macos_titlebar_color(x):
    x = x.strip('"')
    if x == 'system':
        return 0
    if x == 'background':
        return 1
    return (color_as_int(to_color(x)) << 8) | 2


def box_drawing_scale(x):
    ans = tuple(float(x.strip()) for x in x.split(','))
    if len(ans) != 4:
        raise ValueError('Invalid box_drawing scale, must have four entries')
    return ans


def tab_separator(x):
    for q in '\'"':
        if x.startswith(q) and x.endswith(q):
            x = x[1:-1]
            break
    if not x.strip():
        x = ('\xa0' * len(x)) if x else defaults.tab_separator
    return x


def tab_font_style(x):
    return {
        'bold-italic': (True, True),
        'bold': (True, False),
        'italic': (False, True)
    }.get(x.lower().replace('_', '-'), (False, False))


def tab_bar_edge(x):
    return {'top': 1, 'bottom': 3}.get(x.lower(), 3)


def url_style(x):
    return url_style.map.get(x, url_style.map['curly'])


def window_size(val):
    val = val.lower()
    unit = 'cells' if val.endswith('c') else 'px'
    return positive_int(val.rstrip('c')), unit


url_style.map = dict(
    ((v, i) for i, v in enumerate('none single double curly'.split()))
)

type_map = {
    'allow_remote_control': to_bool,
    'adjust_line_height': adjust_line_height,
    'adjust_column_width': adjust_line_height,
    'scrollback_lines': positive_int,
    'scrollback_pager': to_cmdline,
    'open_url_with': to_cmdline,
    'focus_follows_mouse': to_bool,
    'cursor_shape': to_cursor_shape,
    'open_url_modifiers': to_modifiers,
    'rectangle_select_modifiers': to_modifiers,
    'repaint_delay': positive_int,
    'input_delay': positive_int,
    'sync_to_monitor': to_bool,
    'close_on_child_death': to_bool,
    'window_border_width': positive_float,
    'window_margin_width': positive_float,
    'tab_bar_margin_width': positive_float,
    'window_padding_width': positive_float,
    'wheel_scroll_multiplier': float,
    'visual_bell_duration': positive_float,
    'enable_audio_bell': to_bool,
    'click_interval': positive_float,
    'mouse_hide_wait': positive_float,
    'cursor_blink_interval': positive_float,
    'cursor_stop_blinking_after': positive_float,
    'enabled_layouts': to_layout_names,
    'remember_window_size': to_bool,
    'initial_window_width': window_size,
    'initial_window_height': window_size,
    'macos_hide_titlebar': to_bool,
    'macos_hide_from_tasks': to_bool,
    'macos_option_as_alt': to_bool,
    'macos_titlebar_color': macos_titlebar_color,
    'box_drawing_scale': box_drawing_scale,
    'dynamic_background_opacity': to_bool,
    'background_opacity': unit_float,
    'dim_opacity': unit_float,
    'tab_separator': tab_separator,
    'active_tab_font_style': tab_font_style,
    'inactive_tab_font_style': tab_font_style,
    'inactive_text_alpha': unit_float,
    'url_style': url_style,
    'copy_on_select': to_bool,
    'window_alert_on_bell': to_bool,
    'tab_bar_edge': tab_bar_edge,
    'bell_on_tab': to_bool,
    'kitty_mod': to_modifiers,
    'clear_all_shortcuts': to_bool,
    'clipboard_control': lambda x: frozenset(x.lower().split()),
    'window_resize_step_cells': int,
    'window_resize_step_lines': int,
}

for name in (
    'foreground background cursor active_border_color inactive_border_color'
    ' selection_foreground selection_background url_color bell_border_color'
).split():
    type_map[name] = to_color
for i in range(256):
    type_map['color{}'.format(i)] = to_color
for a in ('active', 'inactive'):
    for b in ('foreground', 'background'):
        type_map['%s_tab_%s' % (a, b)] = to_color


def special_handling(key, val, ans):
    if key == 'map':
        parse_key(val, ans['key_definitions'])
        return True
    if key == 'symbol_map':
        ans['symbol_map'].update(parse_symbol_map(val))
        return True
    if key == 'send_text':
        # For legacy compatibility
        parse_send_text(val, ans['key_definitions'])
        return True
    if key == 'clear_all_shortcuts':
        if to_bool(val):
            ans['key_definitions'] = [None]
        return


defaults = None
default_config_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'kitty.conf'
)


def parse_config(lines, check_keys=True):
    ans = {'symbol_map': {}, 'keymap': {}, 'sequence_map': {}, 'key_definitions': []}
    parse_config_base(
        lines,
        defaults,
        type_map,
        special_handling,
        ans,
        check_keys=check_keys
    )
    return ans


def parse_defaults(lines, check_keys=False):
    ans = parse_config(lines, check_keys)
    dfctl = defines.default_color_table()

    for i in range(16, 256):
        k = 'color{}'.format(i)
        ans.setdefault(k, color_from_int(dfctl[i]))
    return ans


Options, defaults = init_config(default_config_path, parse_defaults)
actions = frozenset(all_key_actions) | frozenset(
    'run_simple_kitten combine send_text goto_tab goto_layout set_font_size new_tab_with_cwd new_window_with_cwd new_os_window_with_cwd'.
    split()
)
no_op_actions = frozenset({'noop', 'no-op', 'no_op'})


def merge_configs(defaults, vals):
    ans = {}
    for k, v in defaults.items():
        if isinstance(v, dict):
            newvals = vals.get(k, {})
            ans[k] = merge_dicts(v, newvals)
        elif k == 'key_definitions':
            ans['key_definitions'] = v + vals.get('key_definitions', [])
        else:
            ans[k] = vals.get(k, v)
    return ans


def build_ansi_color_table(opts=defaults):

    def as_int(x):
        return (x[0] << 16) | (x[1] << 8) | x[2]

    def col(i):
        return as_int(getattr(opts, 'color{}'.format(i)))

    return list(map(col, range(256)))


def atomic_save(data, path):
    import tempfile
    fd, p = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.rename(p, path)
    finally:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except Exception as err:
            log_error('Failed to delete temp file {} for atomic save with error: {}'.format(
                p, err))


@contextmanager
def cached_values_for(name):
    cached_path = os.path.join(cache_dir(), name + '.json')
    cached_values = {}
    try:
        with open(cached_path, 'rb') as f:
            cached_values.update(json.loads(f.read().decode('utf-8')))
    except FileNotFoundError:
        pass
    except Exception as err:
        log_error('Failed to load cached in {} values with error: {}'.format(
            name, err))

    yield cached_values

    try:
        data = json.dumps(cached_values).encode('utf-8')
        atomic_save(data, cached_path)
    except Exception as err:
        log_error('Failed to save cached values with error: {}'.format(
            err))


def initial_window_size_func(opts, cached_values):

    if 'window-size' in cached_values and opts.remember_window_size:
        ws = cached_values['window-size']
        try:
            w, h = map(int, ws)

            def initial_window_size(*a):
                return w, h
            return initial_window_size
        except Exception:
            log_error('Invalid cached window size, ignoring')

    w, w_unit = opts.initial_window_width
    h, h_unit = opts.initial_window_height

    def get_window_size(cell_width, cell_height, dpi_x, dpi_y):
        if w_unit == 'cells':
            width = cell_width * w + (dpi_x / 72) * (opts.window_margin_width + opts.window_padding_width) + 1
        else:
            width = w
        if h_unit == 'cells':
            height = cell_height * h + (dpi_y / 72) * (opts.window_margin_width + opts.window_padding_width) + 1
        else:
            height = h
        return width, height

    return get_window_size


def commented_out_default_config():
    ans = []
    for line in as_conf_file(all_options.values()):
        if line and line[0] != '#':
            line = '# ' + line
        ans.append(line)
    return '\n'.join(ans)


def prepare_config_file_for_editing():
    if not os.path.exists(defconf):
        d = os.path.dirname(defconf)
        try:
            os.makedirs(d)
        except FileExistsError:
            pass
        with open(defconf, 'w', encoding='utf-8') as f:
            f.write(commented_out_default_config())
    return defconf


def finalize_keys(opts):
    defns = []
    for d in opts.key_definitions:
        if d is None:  # clear_all_shortcuts
            defns = []
        else:
            defns.append(d)
    for d in defns:
        d.resolve(opts.kitty_mod)
    keymap = {}
    sequence_map = {}

    for defn in defns:
        is_no_op = defn.action.func in no_op_actions
        if defn.is_sequence:
            keymap.pop(defn.trigger, None)
            s = sequence_map.setdefault(defn.trigger, {})
            if is_no_op:
                s.pop(defn.rest, None)
                if not s:
                    del sequence_map[defn.trigger]
            else:
                s[defn.rest] = defn.action
        else:
            sequence_map.pop(defn.trigger, None)
            if is_no_op:
                keymap.pop(defn.trigger, None)
            else:
                keymap[defn.trigger] = defn.action
    opts.keymap = keymap
    opts.sequence_map = sequence_map


def load_config(*paths, overrides=None):
    opts = _load_config(Options, defaults, parse_config, merge_configs, *paths, overrides=overrides)
    finalize_keys(opts)
    if opts.background_opacity < 1.0 and opts.macos_titlebar_color:
        log_error('Cannot use both macos_titlebar_color and background_opacity')
        opts.macos_titlebar_color = 0
    return opts
