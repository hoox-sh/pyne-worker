# Copyright (C) 2024-2026 jango_blockchained
#
# This file is part of pynescript.
#
# pynescript is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pynescript is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with pynescript.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Document colors — ``textDocument/documentColor`` and ``colorPresentation``.

Public handlers: :func:`handle_document_color` and
:func:`handle_color_presentation`. Reported swatches:

- lexer ``COLOR`` tokens (``#rgb`` / ``#rrggbb`` / ``#rrggbbaa``) as
  :class:`~pynescript.ast.node.Constant` with ``kind="#"``
- quoted ``"#rrggbb"`` / ``"#rrggbbaa"`` string Constants (``kind`` is ``None``)
- named ``color.*`` constants (``color.red``, …) as :class:`~pynescript.ast.node.Attribute`
  on ``Name("color")``
- ``color.rgb(r, g, b[, transp])`` with numeric literals (Pine transp 0-100,
  0 = opaque)
- ``color.new(color, transp)`` and the Pine alias ``color(color, transp)``
  when both args are literals (named ``color.*``, ``#hex`` token, or quoted
  hex; transp 0-100)
- ``color.from_gradient(value, min, max, color1, color2)`` when all five
  args are literals (numeric value/min/max plus named ``color.*`` /
  ``#hex`` token / quoted hex)

Each becomes a 0-1 RGBA :class:`~lsprotocol.types.ColorInformation`.
``colorPresentation`` rewrites the range as ``#rrggbb`` (or ``#rrggbbaa`` when
alpha != 1), including when the original text was a ``color.*`` name. Empty
source or parse failure returns ``[]``.
"""

from __future__ import annotations

import logging
import re

from typing import Any

from lsprotocol import types as lsp

from pynescript.ast import node as ast
from pynescript.ast.evaluator.builtins.color import _NAMED_COLORS
from pynescript.ast.evaluator.builtins.color import Color as _PineColor
from pynescript.ast.evaluator.builtins.color import color_from_gradient
from pynescript.ast.helper import parse
from pynescript.ast.helper import walk

from ._ranges import node_to_range as _node_to_range


logger = logging.getLogger(__name__)

_HEX_LITERAL = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_HEX_RGB_LEN = 3
_HEX_RRGGBB_LEN = 6
_OPAQUE_ALPHA = 255
_CHANNEL_MAX = 255.0
_TRANSP_MAX = 100.0
_COLOR_NS = "color"
_RGB_ATTR = "rgb"
_NEW_ATTR = "new"
_RGB_POS = ("red", "green", "blue", "transp")
_RGB_ALIASES = {"r": "red", "g": "green", "b": "blue", "t": "transp"}
_RGB_KEYS = frozenset(_RGB_POS)
_NEW_POS = ("color", "transp")
_NEW_KEYS = frozenset(_NEW_POS)
_GRAD_ATTR = "from_gradient"
_GRAD_POS = ("value", "bottom_value", "top_value", "bottom_color", "top_color")
_GRAD_ALIASES = {
    "value": "value",
    "bottom_value": "bottom_value",
    "bottom": "bottom_value",
    "min_val": "bottom_value",
    "min": "bottom_value",
    "top_value": "top_value",
    "top": "top_value",
    "max_val": "top_value",
    "max": "top_value",
    "bottom_color": "bottom_color",
    "color1": "bottom_color",
    "top_color": "top_color",
    "color2": "top_color",
}
_GRAD_NUM_KEYS = frozenset({"value", "bottom_value", "top_value"})
_GRAD_COLOR_KEYS = frozenset({"bottom_color", "top_color"})


def handle_document_color(
    params: lsp.DocumentColorParams,
    source: str | None,
    tree: Any | None = ...,
) -> list[lsp.ColorInformation]:
    """Handle textDocument/documentColor request.

    Args:
        params: Client document-color params.
        source: Document text, or ``None``.
        tree: Pre-parsed AST from the workspace cache. Pass ``None`` when the
            workspace already failed to parse (skips a redundant re-parse).
            Omit (default ``...``) to parse from *source*.

    Returns:
        Color information for each hex literal, quoted ``"#hex"`` string,
        ``color.*`` name, and literal ``color.rgb`` / ``color.new`` /
        ``color()`` / ``color.from_gradient`` call (possibly empty). Never
        ``None``.
    """
    if tree is ...:
        if not source:
            return []
        try:
            tree = parse(source, filename=params.text_document.uri)
        except Exception as exc:
            logger.debug("document_color: parse failed: %s", exc)
            return []
    if tree is None:
        return []

    results: list[lsp.ColorInformation] = []
    for node in walk(tree):
        info = _color_info_for_node(node)
        if info is not None:
            results.append(info)
    return results


def handle_color_presentation(
    params: lsp.ColorPresentationParams,
    source: str | None = None,
) -> list[lsp.ColorPresentation]:
    """Handle textDocument/colorPresentation request.

    Rewrites *params.range* as ``#rrggbb`` (opaque) or ``#rrggbbaa``. Named
    ``color.*`` ranges become hex. *source* is unused (the client already
    supplied the color and range).
    """
    del source
    hex_str = _color_to_hex(params.color)
    return [
        lsp.ColorPresentation(
            label=hex_str,
            text_edit=lsp.TextEdit(range=params.range, new_text=hex_str),
        )
    ]


def _color_info_for_node(node: Any) -> lsp.ColorInformation | None:
    """Return ColorInformation for a hex / named / rgb / new / ``color()`` / from_gradient node."""
    color = None
    if isinstance(node, ast.Constant):
        color = _hex_constant_color(node)
    elif isinstance(node, ast.Attribute):
        color = _named_attr_color(node)
    elif isinstance(node, ast.Call):
        color = _rgb_call_color(node) or _new_call_color(node) or _from_gradient_call_color(node)
    if color is None:
        return None
    rng = _node_to_range(node)
    if rng is None:
        return None
    return lsp.ColorInformation(range=rng, color=color)


def _hex_constant_color(node: ast.Constant) -> lsp.Color | None:
    """Parse a ``kind="#"`` hex Constant or quoted ``"#hex"`` string, or ``None``."""
    value = node.value
    if not isinstance(value, str):
        return None
    kind = getattr(node, "kind", None)
    if kind == "#":
        return _hex_to_color(value)
    if kind is not None:
        return None
    if not value.startswith("#"):
        return None
    return _hex_to_color(value)


def _named_attr_color(node: ast.Attribute) -> lsp.Color | None:
    """Parse ``color.red`` / … (Attribute on Name ``color``), or ``None``."""
    value = node.value
    if not isinstance(value, ast.Name) or value.id != _COLOR_NS:
        return None
    hex_str = _NAMED_COLORS.get(node.attr)
    if hex_str is None:
        return None
    return _hex_to_color(hex_str)


def _rgb_call_color(node: ast.Call) -> lsp.Color | None:
    """Parse ``color.rgb`` with numeric-literal args, or ``None``."""
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != _RGB_ATTR:
        return None
    if not isinstance(func.value, ast.Name) or func.value.id != _COLOR_NS:
        return None
    channels = _rgb_literal_channels(node.args)
    if channels is None:
        return None
    r, g, b, a = channels
    return lsp.Color(
        red=r / _CHANNEL_MAX,
        green=g / _CHANNEL_MAX,
        blue=b / _CHANNEL_MAX,
        alpha=a / _CHANNEL_MAX,
    )


def _new_call_color(node: ast.Call) -> lsp.Color | None:
    """Parse ``color.new`` / ``color()`` with literal args, or ``None``.

    Pine registers ``color()`` as an alias of ``color.new()``.
    """
    if not _is_color_new_func(node.func):
        return None
    pair = _new_literal_args(node.args)
    if pair is None:
        return None
    base, transp = pair
    return lsp.Color(
        red=base.red,
        green=base.green,
        blue=base.blue,
        alpha=_transp_to_alpha_byte(transp) / _CHANNEL_MAX,
    )


def _is_color_new_func(func: Any) -> bool:
    """True for ``color.new`` Attribute or ``color`` Name (Pine alias)."""
    if isinstance(func, ast.Name):
        return func.id == _COLOR_NS
    if isinstance(func, ast.Attribute):
        return func.attr == _NEW_ATTR and isinstance(func.value, ast.Name) and func.value.id == _COLOR_NS
    return False


def _from_gradient_call_color(node: ast.Call) -> lsp.Color | None:
    """Parse ``color.from_gradient`` with five literal args, or ``None``."""
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != _GRAD_ATTR:
        return None
    if not isinstance(func.value, ast.Name) or func.value.id != _COLOR_NS:
        return None
    parsed = _from_gradient_literal_args(node.args)
    if parsed is None:
        return None
    value, min_val, max_val, color1, color2 = parsed
    result = color_from_gradient(
        value,
        min_val,
        max_val,
        _lsp_color_to_pine(color1),
        _lsp_color_to_pine(color2),
    )
    return lsp.Color(
        red=result.r / _CHANNEL_MAX,
        green=result.g / _CHANNEL_MAX,
        blue=result.b / _CHANNEL_MAX,
        alpha=result.a / _CHANNEL_MAX,
    )


def _lsp_color_to_pine(color: lsp.Color) -> _PineColor:
    """Convert a 0-1 LSP Color to a 0-255 Pine :class:`Color`."""
    return _PineColor(
        _channel_byte(color.red),
        _channel_byte(color.green),
        _channel_byte(color.blue),
        _channel_byte(color.alpha),
    )


def _from_gradient_literal_args(
    args: list[Any],
) -> tuple[float, float, float, lsp.Color, lsp.Color] | None:
    """Merge positional/keyword ``from_gradient`` literals to (value, min, max, c1, c2)."""
    nums: dict[str, float] = {}
    colors: dict[str, lsp.Color] = {}
    pos_i = 0
    for arg in args:
        if not isinstance(arg, ast.Arg):
            return None
        key = _grad_arg_key(arg.name, pos_i)
        if key is None:
            return None
        if not arg.name:
            pos_i += 1
        if key in nums or key in colors:
            return None
        if not _store_grad_arg(key, arg.value, nums, colors):
            return None
    if set(nums) != _GRAD_NUM_KEYS or set(colors) != _GRAD_COLOR_KEYS:
        return None
    return (
        nums["value"],
        nums["bottom_value"],
        nums["top_value"],
        colors["bottom_color"],
        colors["top_color"],
    )


def _grad_arg_key(raw_name: str | None, pos_i: int) -> str | None:
    """Resolve a ``from_gradient`` arg name or next positional slot."""
    if raw_name:
        return _GRAD_ALIASES.get(raw_name)
    if pos_i >= len(_GRAD_POS):
        return None
    return _GRAD_POS[pos_i]


def _store_grad_arg(
    key: str,
    value: Any,
    nums: dict[str, float],
    colors: dict[str, lsp.Color],
) -> bool:
    """Store one literal ``from_gradient`` arg; False if the value is not a literal."""
    if key in _GRAD_NUM_KEYS:
        num = _literal_number(value)
        if num is None:
            return False
        nums[key] = num
        return True
    parsed = _literal_color(value)
    if parsed is None:
        return False
    colors[key] = parsed
    return True


def _literal_color(node: Any) -> lsp.Color | None:
    """Color from a hex / quoted-hex Constant or named ``color.*`` Attribute."""
    if isinstance(node, ast.Constant):
        return _hex_constant_color(node)
    if isinstance(node, ast.Attribute):
        return _named_attr_color(node)
    return None


def _new_literal_args(args: list[Any]) -> tuple[lsp.Color, float] | None:
    """Merge positional/keyword ``color.new`` / ``color()`` literals to (color, transp)."""
    color: lsp.Color | None = None
    transp: float | None = None
    pos_i = 0
    for arg in args:
        if not isinstance(arg, ast.Arg):
            return None
        raw_name = arg.name
        if raw_name:
            if raw_name not in _NEW_KEYS:
                return None
            key = raw_name
        else:
            if pos_i >= len(_NEW_POS):
                return None
            key = _NEW_POS[pos_i]
            pos_i += 1
        if key == "color":
            if color is not None:
                return None
            parsed = _literal_color(arg.value)
            if parsed is None:
                return None
            color = parsed
            continue
        if transp is not None:
            return None
        num = _literal_number(arg.value)
        if num is None:
            return None
        transp = num
    if color is None or transp is None:
        return None
    return color, transp


def _literal_number(node: Any) -> float | None:
    """Return a numeric Constant (or unary ``+/-`` of one) as float."""
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            num = _literal_number(node.operand)
            return None if num is None else -num
        if isinstance(node.op, ast.UAdd):
            return _literal_number(node.operand)
        return None
    if not isinstance(node, ast.Constant):
        return None
    value = node.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _rgb_literal_channels(args: list[Any]) -> tuple[int, int, int, int] | None:
    """Merge positional/keyword rgb literals to clamped (r, g, b, a) bytes."""
    values: dict[str, float] = {}
    pos_i = 0
    for arg in args:
        if not isinstance(arg, ast.Arg):
            return None
        num = _literal_number(arg.value)
        if num is None:
            return None
        raw_name = arg.name
        if raw_name:
            key = _RGB_ALIASES.get(raw_name, raw_name)
            if key not in _RGB_KEYS:
                return None
            values[key] = num
            continue
        if pos_i >= len(_RGB_POS):
            return None
        values[_RGB_POS[pos_i]] = num
        pos_i += 1
    if not {"red", "green", "blue"} <= values.keys():
        return None
    r = _clamp_channel(values["red"])
    g = _clamp_channel(values["green"])
    b = _clamp_channel(values["blue"])
    a = _transp_to_alpha_byte(values.get("transp", 0.0))
    return r, g, b, a


def _clamp_channel(value: float) -> int:
    """Clamp a 0-255 RGB component (Pine ``color.rgb`` / ``Color``)."""
    return max(0, min(_OPAQUE_ALPHA, int(value)))


def _transp_to_alpha_byte(transp: float) -> int:
    """Convert Pine transparency 0-100 (0 opaque) to an 8-bit alpha."""
    t = max(0.0, min(_TRANSP_MAX, transp))
    return round(_CHANNEL_MAX * (1.0 - t / _TRANSP_MAX))


def _hex_to_color(value: str) -> lsp.Color | None:
    """Parse a ``#rgb`` / ``#rrggbb`` / ``#rrggbbaa`` literal to 0-1 RGBA."""
    raw = value.strip()
    match = _HEX_LITERAL.fullmatch(raw)
    if match is None:
        return None
    hexpart = match.group(1)
    if len(hexpart) == _HEX_RGB_LEN:
        hexpart = "".join(ch * 2 for ch in hexpart) + "ff"
    elif len(hexpart) == _HEX_RRGGBB_LEN:
        hexpart = hexpart + "ff"
    r = int(hexpart[0:2], 16)
    g = int(hexpart[2:4], 16)
    b = int(hexpart[4:6], 16)
    a = int(hexpart[6:8], 16)
    return lsp.Color(
        red=r / _CHANNEL_MAX,
        green=g / _CHANNEL_MAX,
        blue=b / _CHANNEL_MAX,
        alpha=a / _CHANNEL_MAX,
    )


def _channel_byte(value: float) -> int:
    """Clamp a 0-1 channel to an 8-bit component."""
    return max(0, min(_OPAQUE_ALPHA, round(value * _CHANNEL_MAX)))


def _color_to_hex(color: lsp.Color) -> str:
    """Encode *color* as ``#rrggbb`` or ``#rrggbbaa`` when alpha != 1."""
    r = _channel_byte(color.red)
    g = _channel_byte(color.green)
    b = _channel_byte(color.blue)
    a = _channel_byte(color.alpha)
    if a == _OPAQUE_ALPHA:
        return f"#{r:02x}{g:02x}{b:02x}"
    return f"#{r:02x}{g:02x}{b:02x}{a:02x}"
