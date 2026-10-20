#!/usr/bin/env python
# -*- coding: UTF-8 -*-

# Copyright (c) 2025, Sandflow Consulting LLC
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""SCC writer"""

from __future__ import annotations

from enum import Enum
import logging
import math
from fractions import Fraction
import re
from typing import List, Optional, Sequence

import ttconv.model as model
from ttconv.isd import ISD
from ttconv.scc.codes.characters import unicode_to_scc, unicode_to_scc_or_none, transliterate_unicode
from ttconv.scc.codes.preambles_address_codes import SccPreambleAddressCode
from ttconv.scc.config import SccWriterConfiguration
from ttconv.style_properties import StyleProperties, NamedColors, TextAlignType, FontStyleType, ColorType, DisplayAlignType, LengthType
from ttconv.scc.codes.control_codes import SccControlCode
from ttconv.scc.codes.mid_row_codes import SccMidRowCode
from ttconv.time_code import FPS_29_97, FPS_30, SmpteTimeCode

LOGGER = logging.getLogger(__name__)

# The characters that are not in CEA-608 and for which a warning was logged since the conversion started (a stream can have
# thousands of them and one warning for each would flood the log)
_UNSUPPORTED_CHARACTERS_WARNED = set()

def _to_cc608_text(text: str) -> str:
  """Returns text that CEA-608 can represent. Best effort: CEA-608 has 176 characters and the standard says nothing about the
  others, so a character that is not in the sets is replaced by plain characters that stand for it if there are obvious ones
  (see ttconv.scc.codes.characters.transliterate_unicode), otherwise by a space, with a warning, once for each such character
  of a conversion. Not every text can be represented."""
  result = []
  for c in text:
    if unicode_to_scc_or_none(c) is not None:
      result.append(c)
      continue

    replacement = transliterate_unicode(c)
    if replacement is None:
      replacement = " "
      if c not in _UNSUPPORTED_CHARACTERS_WARNED:
        _UNSUPPORTED_CHARACTERS_WARNED.add(c)
        LOGGER.warning("The character %r (U+%04X) cannot be represented in CEA-608, it is replaced with a space (the other "
                       "occurrences are replaced silently)", c, ord(c))

    result.append(replacement)

  return "".join(result)

# The foreground colors of CEA-608 (CTA-608-E 6.2), in the order of the mid-row codes
_COLORS = [NamedColors.white, NamedColors.green, NamedColors.blue, NamedColors.cyan, NamedColors.red, NamedColors.yellow,
           NamedColors.magenta]
_RGB_COLORS = [(255, 255, 255), (0, 255, 0), (0, 0, 255), (0, 255, 255), (255, 0, 0), (255, 255, 0), (255, 0, 255)]

# (color index, italic, underline) of a character; italics are white italics in CEA-608
_DEFAULT_STYLE = (0, False, False)

def _closest_color_index(color: Optional[ColorType]) -> int:
  """Returns the index in _COLORS of the CEA-608 color that is the closest to the color"""
  if color is None:
    return 0
  rgb = color.components[:3]
  return min(range(len(_RGB_COLORS)), key=lambda i: sum((a - b) ** 2 for a, b in zip(rgb, _RGB_COLORS[i])))

def _style_of(element: model.Span) -> tuple:
  """Returns the CEA-608 style (color index, italic, underline) of the content of an element"""
  color = _closest_color_index(element.get_style(StyleProperties.Color))
  italic = element.get_style(StyleProperties.FontStyle) in (FontStyleType.italic, FontStyleType.oblique)
  decoration = element.get_style(StyleProperties.TextDecoration)
  underline = bool(decoration is not None and decoration.underline)

  if italic and color != 0:
    # italics are white in CEA-608: the color wins
    italic = False

  return (color, italic, underline)

def _mid_row_code(style: tuple) -> int:
  """Returns the mid-row code (channel 1) that sets the style"""
  color, italic, underline = style
  base = 0x112E if italic else 0x1120 + 2 * color
  return base + (1 if underline else 0)

# The 15 rows and 32 columns of CEA-608 are the safe area, which is 80% of the root area, in the 40 by 19 cell grid that the SCC
# reader writes (ttconv.scc.reader): the 32 columns start at the 5th cell, the 15 rows at the 3rd
_ROOT_COLUMNS = 40
_ROOT_ROWS = 19
_FIRST_COLUMN_CELL = 4
_FIRST_ROW_CELL = 2
_ROWS = 15

class _Position:
  """Region of a caption, in percentages of the root container"""

  def __init__(self, x: float, y: float, width: float, height: float, display_align: DisplayAlignType):
    self.x = x
    self.y = y
    self.width = width
    self.height = height
    self.display_align = display_align

  def get_column(self) -> int:
    """The column (starting at 0) of the left of the region"""
    return min(max(round(self.x * _ROOT_COLUMNS / 100) - _FIRST_COLUMN_CELL, 0), 31)

  def get_width(self) -> int:
    """The width of the region in columns"""
    return min(max(round(self.width * _ROOT_COLUMNS / 100), 1), 32)

  def get_rows(self, nb_lines: int) -> int:
    """The row of the first of lines, which are laid out in the region (a line is a row of the grid)"""
    top = round(self.y * _ROOT_ROWS / 100) - _FIRST_ROW_CELL + 1
    bottom = round((self.y + self.height) * _ROOT_ROWS / 100) - _FIRST_ROW_CELL

    if self.display_align is DisplayAlignType.after:
      first = bottom - nb_lines + 1
    elif self.display_align is DisplayAlignType.center:
      first = top + (bottom - top + 1 - nb_lines) // 2
    else:
      first = top

    return min(max(first, 1), max(_ROWS - nb_lines + 1, 1))

  def get_last_row(self, nb_lines: int) -> int:
    """The row of the last of lines, which are laid out in the region"""
    return min(self.get_rows(nb_lines) + nb_lines - 1, _ROWS)

def _position_of(region: model.Region) -> Optional[_Position]:
  """The position of a region of an ISD, unless it is the default region or has no usable position"""
  if region.get_id() == ISD.DEFAULT_REGION_ID:
    return None

  origin = region.get_style(StyleProperties.Origin)
  extent = region.get_style(StyleProperties.Extent)
  if origin is None or extent is None:
    return None

  if any(v.units is not LengthType.Units.rw for v in (origin.x, extent.width)) or \
     any(v.units is not LengthType.Units.rh for v in (origin.y, extent.height)):
    return None

  return _Position(origin.x.value, origin.y.value, extent.width.value, extent.height.value,
                   region.get_style(StyleProperties.DisplayAlign))

class _Caption:
  """Collection of lines of text with a begin and end time and an alignment"""

  def __init__(self):
    self._begin: Fraction = 0
    self._end: Optional[Fraction] = None
    self._lines: List[str] = []
    self._styles: List[List[tuple]] = []  # the style of every character of every line
    self._alignment: TextAlignType = TextAlignType.start
    self._position: Optional[_Position] = None

  def set_begin(self, begin: Fraction):
    """Sets the begin time of the lines"""
    self._begin = begin

  def get_begin(self) -> Fraction:
    """Returns the begin time of the lines"""
    return self._begin

  def set_end(self, end: Fraction):
    """Sets the end time of the lines"""
    self._end = end

  def get_end(self) -> Optional[Fraction]:
    """Returns the end time of the lines"""
    return self._end

  def set_alignment(self, alignment: TextAlignType):
    """Sets the alignment of the lines"""
    self._alignment = alignment

  def get_alignment(self) -> TextAlignType:
    """Returns the alignment of the lines"""
    return self._alignment

  def set_lines(self, lines: List[str]):
    """Sets the lines of text (without styles)"""
    self._lines = lines
    self._styles = [[_DEFAULT_STYLE] * len(line) for line in lines]

  def set_position(self, position: Optional[_Position]):
    """Sets the position of the region of the lines"""
    self._position = position

  def get_position(self) -> Optional[_Position]:
    """Returns the position of the region of the lines, None if it has none"""
    return self._position

  def get_styles(self, index: int) -> List[tuple]:
    """Returns the style of every character of a line"""
    return self._styles[index]

  def __len__(self):
    return len(self._lines)

  def __getitem__(self, index: int) -> str:
    return self._lines[index]

  def __setitem__(self, index: int, value: str):
    self._lines[index] = value

  def __iter__(self):
    return iter(self._lines)

  def append(self, line: str):
    self._lines.append(line)
    self._styles.append([_DEFAULT_STYLE] * len(line))

  def append_text(self, text: str, style: tuple):
    """Appends text with a style to the last line"""
    text = _to_cc608_text(text)
    self._lines[-1] = self._lines[-1] + text
    self._styles[-1] = self._styles[-1] + [style] * len(text)

  @staticmethod
  def from_regions(regions: Sequence[model.Region]) -> _Caption:
    """Returns the list of lines that are flowed into the Region"""
    _lines: _Caption = _Caption()

    def _process_element(element: model.ContentElement, style: tuple = _DEFAULT_STYLE):

      if isinstance(element, model.Div):
        for elem in element:
          _process_element(elem, style)
        return

      if isinstance(element, model.P):
        _lines.append("")
        _lines.set_alignment(element.get_style(StyleProperties.TextAlign))
        for elem in element:
          _process_element(elem, style)
        return

      if isinstance(element, model.Span):
        span_style = _style_of(element)
        for elem in element:
          _process_element(elem, span_style)
        return

      if isinstance(element, model.Br):
        _lines.append("")
        return

      if isinstance(element, model.Text):
        _lines.append_text(element.get_text(), style)
        return

    for region in regions:
      for body in region:
        for div in body:
          _process_element(div)

    return _lines

class _Chunk:

  ODD_PARITY_OCTETS = [
    128, 1, 2, 131, 4, 133, 134, 7, 8, 137, 138, 11, 140, 13, 14, 143,
    16, 145, 146, 19, 148, 21, 22, 151, 152, 25, 26, 155, 28, 157, 158, 31,
    32, 161, 162, 35, 164, 37, 38, 167, 168, 41, 42, 171, 44, 173, 174, 47,
    176, 49, 50, 179, 52, 181, 182, 55, 56, 185, 186, 59, 188, 61, 62, 191,
    64, 193, 194, 67, 196, 69, 70, 199, 200, 73, 74, 203, 76, 205, 206, 79,
    208, 81, 82, 211, 84, 213, 214, 87, 88, 217, 218, 91, 220, 93, 94, 223,
    224, 97, 98, 227, 100, 229, 230, 103, 104, 233, 234, 107, 236, 109, 110, 239,
    112, 241, 242, 115, 244, 117, 118, 247, 248, 121, 122, 251, 124, 253, 254, 127
  ]

  def __init__(self):
    self._octet_buffer : bytearray = bytearray()
    self._begin_frame : Optional[int] = None

  def set_begin(self, frame: Optional[int]):
    self._begin_frame = frame

  def get_begin(self) -> Optional[int]:
    return self._begin_frame

  def get_dur(self) -> int:
    return (len(self._octet_buffer) + 1) // 2

  def get_end(self) -> Optional[int]:
    if self._begin_frame is None:
      return None
    return self._begin_frame + self.get_dur()

  def push_control_code(self, cc: int):
    hi_octet = cc // 256
    lo_octet = cc % 256
    if (hi_octet > 127 or lo_octet > 127):
      raise RuntimeError("Line 21 octet is out of range")
    if len(self._octet_buffer) % 2 == 1:
      self._octet_buffer.append(0)
    self._octet_buffer.append(hi_octet)
    self._octet_buffer.append(lo_octet)

    # per spec, always double up control codes
    if 0x10 <= hi_octet <= 0x1F:
      self._octet_buffer.append(hi_octet)
      self._octet_buffer.append(lo_octet)

  def push_char(self, char: str):
    if len(char) != 1:
      raise ValueError("Length of string must be exactly 1")
    
    scc_bytes = unicode_to_scc(char)

    if len(scc_bytes) == 3:
      # A fallback character and an extended character: the fallback is the second octet of a pair, so that the code of the
      # extended character starts a pair
      self._octet_buffer.append(scc_bytes[0])
      if len(self._octet_buffer) % 2 == 1:
        self._octet_buffer.append(0)
      scc_bytes = scc_bytes[1:]
    elif len(scc_bytes) > 1 and len(self._octet_buffer) % 2 == 1:
      self._octet_buffer.append(0)

    for b in scc_bytes:
      self._octet_buffer.append(b)

  def overlap(self, other: _Chunk) -> bool:
    """Checks if the other chunk overlaps with the current chunk"""
    return other.get_begin() <= self.get_end() and other.get_end() >= self.get_begin()

  def insert(self, other: _Chunk):
    """Inserts the other chunk while preserving the begin time of the current chunk"""
    if other.get_begin() is None or other.get_end() is None:
      raise RuntimeError("Cannot insert chunk with no begin time")

    if not self.overlap(other):
      raise RuntimeError("Cannot insert chunk that does not overlap")

    loc = 2 * max(0, other.get_begin() - self.get_begin())
    self._octet_buffer[loc:loc] = other._octet_buffer

    # adjust the begin time preserving the end time
    self.set_begin(self.get_begin() - other.get_dur())

  def __len__(self):
    return len(self._octet_buffer)

  def to_string(self, fps : Fraction, is_df: bool, start_offset: int) -> str:
    if fps not in (FPS_29_97, FPS_30):
      raise ValueError(f"Frame rate {fps} out-of-range")

    if fps != FPS_29_97 and is_df:
      raise ValueError("Frame rate must be fractional if drop frame is true")

    def _octet2hex(octet):
      return format(_Chunk.ODD_PARITY_OCTETS[octet], 'x')

    packets = []
    for i in range(len(self._octet_buffer) // 2):
      packets.append(_octet2hex(self._octet_buffer[2 * i]) + _octet2hex(self._octet_buffer[2 * i + 1]))
    if len(self._octet_buffer) % 2 == 1:
      packets.append(_octet2hex(self._octet_buffer[-1]) + _octet2hex(0))

    return str(SmpteTimeCode.from_frames(self.get_begin() + start_offset, fps, is_df)) + "\t" + " ".join(packets)

MAX_LINEWIDTH = 32

def _line_indent(caption: _Caption, line: str, align: bool = True) -> tuple:
  """Returns the indent of the PAC and the number of spaces that move the first character of a line to its column. The column is
  the one of the region of the caption, plus the alignment of the text in it, or the alignment in the 32 columns when
  the caption has no position"""
  position = caption.get_position()
  alignment = caption.get_alignment() if align else TextAlignType.start

  if position is None:
    if alignment == TextAlignType.center:
      column = (32 - len(line)) // 2
    elif alignment == TextAlignType.end:
      column = 32 - len(line)
    else:
      return None, 0

    return 4 * (column // 4), column % 4

  if alignment == TextAlignType.center:
    center = position.get_column() + position.get_width() / 2
    if abs(center - 16) <= 1:
      # a caption centered on the screen: the convention of the encoders, that the rounding of the region does not blur
      column = (32 - len(line)) // 2
    else:
      column = math.floor(center - len(line) / 2 + 0.5)
  elif alignment == TextAlignType.end:
    column = position.get_column() + position.get_width() - len(line)
  else:
    column = position.get_column()

  column = min(max(column, 0), max(32 - len(line), 0))
  return (4 * (column // 4) if column > 0 else None), column % 4

def _push_chars(chunk: _Chunk, text: str, styles: List[tuple], previous_style: tuple):
  """Pushes characters, with the mid-row codes that change the style. A mid-row code is displayed as a space and takes a
  column: it replaces the space where the style changes (the space that precedes the characters of the new style or the
  first space of them), otherwise it is an additional column"""
  for i, (c, style) in enumerate(zip(text, styles)):
    if c == " " and i + 1 < len(text) and styles[i + 1] != style and styles[i + 1] != previous_style:
      # the cell of this space is the mid-row code of the style that follows
      chunk.push_control_code(_mid_row_code(styles[i + 1]))
      previous_style = styles[i + 1]
      continue

    if style != previous_style:
      chunk.push_control_code(_mid_row_code(style))
      previous_style = style
      if c == " ":
        continue

    chunk.push_char(c)

def _push_line(chunk: _Chunk, row: int, indent: Optional[int], spaces: int, text: str, styles: List[tuple]):
  """Pushes a PAC for the row, the spaces that move the first character to its column, and the text with its styles"""
  style = styles[0] if len(text) > 0 else _DEFAULT_STYLE
  column = (indent or 0) + spaces

  if style == _DEFAULT_STYLE:
    pac = SccPreambleAddressCode(1, row, NamedColors.white.value, indent, False, False)
    chunk.push_control_code(pac.get_ch1_packet())
    for _ in range(spaces):
      chunk.push_char(" ")

  elif column == 0:
    # the PACs without an indent have a color, italics and underline
    color, italic, underline = style
    pac = SccPreambleAddressCode(1, row, _COLORS[color].value, None, italic, underline)
    chunk.push_control_code(pac.get_ch1_packet())

  else:
    # a mid-row code, that takes the column before the first character, sets the style
    base = 4 * ((column - 1) // 4)
    pac = SccPreambleAddressCode(1, row, NamedColors.white.value, base, False, False)
    chunk.push_control_code(pac.get_ch1_packet())
    for _ in range(column - 1 - base):
      chunk.push_char(" ")
    chunk.push_control_code(_mid_row_code(style))

  _push_chars(chunk, text, styles, style)

class _RollupKind(Enum):
  START = 0
  ROLL_UP = 1
  PAINT_ON = 2

def _reflow(text: str) -> List[str]:
  """Splits `text` into rows of at most MAX_LINEWIDTH characters (CTA-608-E C.13). A row is broken
  at the last space that fits, and that space is dropped. A word longer than a row is cut at the last
  column. Trailing spaces are dropped, so that no row is empty."""
  text = text.rstrip(' ')

  rows: List[str] = []
  while len(text) > MAX_LINEWIDTH:
    break_i = text.rfind(' ', 1, MAX_LINEWIDTH + 1)

    if break_i > 0:
      rows.append(text[:break_i])
      text = text[break_i + 1:]
    else:
      rows.append(text[:MAX_LINEWIDTH])
      text = text[MAX_LINEWIDTH:]

  rows.append(text)

  return rows

def _process_as_rollup(captions, config, progress_callback) -> List[_Chunk]:
  chunks: List[_Chunk] = []

  for i, caption in enumerate(captions):
    # 25% for SCC writing
    progress_callback(0.75 + (i + 1) / len(captions) / 4)

    ru_chunk: _Chunk = _Chunk()

    # what kind of roll-up caption are we handling
    kind: _RollupKind = _RollupKind.START
    if i > 0:
      if captions[i][-1].startswith(captions[i - 1][-1]):
        kind = _RollupKind.PAINT_ON
      elif len(captions[i]) > 1 and captions[i][-2] == captions[i - 1][-1]:
        kind = _RollupKind.ROLL_UP

    begin_f = int(caption.get_begin() * config.frame_rate.fps)

    if kind is _RollupKind.PAINT_ON:
      ru_chunk.set_begin(begin_f)
      shown = len(captions[i - 1][-1])
      previous_style = caption.get_styles(-1)[shown - 1] if shown > 0 else _DEFAULT_STYLE
      _push_chars(ru_chunk, caption[-1][shown:], caption.get_styles(-1)[shown:], previous_style)
    else:
      # determine the first line to be emitted
      first_line_index = 0 if kind is _RollupKind.START else -1

      first_line = first_line_index % len(caption)
      for line_index in range(first_line, len(caption)):
        line = caption[line_index]

        if config.rollup_lines == 2:
          ru_chunk.push_control_code(SccControlCode.RU2.get_ch1_value())
        elif config.rollup_lines == 3:
          ru_chunk.push_control_code(SccControlCode.RU3.get_ch1_value())
        else:
          ru_chunk.push_control_code(SccControlCode.RU4.get_ch1_value())

        # the caption begins when the CR code is received
        begin_f = begin_f - ru_chunk.get_dur()
        ru_chunk.push_control_code(SccControlCode.CR.get_ch1_value())
        # The base row of the roll-up caption is the last row of its region, row 15 without a region (CTA-608-E C.7, C.15).
        # TODO: the TeleCaption I and II decoders do not recognize the PACs for the mid-screen rows 5-11 (B.2, B.5), they
        # display the caption on row 15. If you care about these decoders, see the alternate-row scheme for mid-screen PACs
        # (CTA-608-E B.2.2). Patches welcome.
        position = caption.get_position()
        base_row = max(position.get_last_row(len(caption)), config.rollup_lines) if position is not None else 15
        # the rows of a roll-up caption start at the column of the region, whatever the alignment of the text
        indent, spaces = _line_indent(caption, line, False) if position is not None else (0, 0)
        _push_line(ru_chunk, base_row, indent or 0, spaces, line, caption.get_styles(line_index))

        if len(chunks) > 0 and begin_f < chunks[-1].get_end():
          begin_f = chunks[-1].get_end()
          LOGGER.warning("Overlapping roll-up text at %s", SmpteTimeCode.from_seconds(caption.get_begin(), config.frame_rate.fps))
        ru_chunk.set_begin(begin_f)

    chunks.append(ru_chunk)

    # erase the display if there is a gap between roll-up captions or if it is the last caption
    if caption.get_end() is not None and \
      (i == len(captions) - 1 or captions[i + 1].get_begin() - caption.get_end() > config.rollup_gap_tolerance):
      edm_chunk = _Chunk()
      edm_chunk.push_control_code(SccControlCode.EDM.get_ch1_value())
      edm_chunk.set_begin(int(caption.get_end() * config.frame_rate.fps))
      chunks.append(edm_chunk)

  return chunks

def _process_as_popon(captions, config, progress_callback) -> List[_Chunk]:
  chunks : List[_Chunk] = []

  for i, caption in enumerate(captions):
    # 25% for SCC writing
    progress_callback(0.75 + (i + 1) / len(captions) / 4)

    enm_chunk: _Chunk = _Chunk()

    enm_chunk.push_control_code(SccControlCode.RCL.get_ch1_value())
    enm_chunk.push_control_code(SccControlCode.ENM.get_ch1_value())
    # The rows of the caption are those of its region, the bottom rows of the screen without one.
    # TODO: the TeleCaption I and II decoders do not recognize the PACs for the mid-screen rows 5-11 (CTA-608-E B.2), the
    # alternate-row scheme for mid-screen PACs (B.2.2) keeps a caption readable on these decoders. Patches welcome.
    position = caption.get_position()
    first_row = position.get_rows(len(caption)) if position is not None else 15 - len(caption)
    for index, line in enumerate(caption):
      indent, spaces = _line_indent(caption, line)
      _push_line(enm_chunk, first_row + index, indent, spaces, line, caption.get_styles(index))
    enm_chunk.push_control_code(SccControlCode.EOC.get_ch1_value())

    enm_chunk.set_begin(int(caption.get_begin() * config.frame_rate.fps - enm_chunk.get_dur() + 2))
    # check if there is an overlap with the previous chunk
    if len(chunks) > 0:
      if chunks[-2].get_end() + chunks[-1].get_dur() > enm_chunk.get_begin():
        LOGGER.warning("Skipping ISD at %s due to overlap in line 21 packets with previous ISD", float(caption.get_begin()))
        continue

      if enm_chunk.overlap(chunks[-1]):
        enm_chunk.insert(chunks[-1])
        chunks.pop()

    chunks.append(enm_chunk)

    # initialize the EDM chunk
    if caption.get_end() is not None:
      edm_chunk = _Chunk()
      edm_chunk.push_control_code(SccControlCode.EDM.get_ch1_value())
      edm_chunk.set_begin(int(caption.get_end() * config.frame_rate.fps))
      chunks.append(edm_chunk)

  return chunks

#
# scc writer
#
def from_model(doc: model.ContentDocument, config: Optional[SccWriterConfiguration] = None, progress_callback=lambda _: None) -> str:
  """Converts the data model to an SCC document"""

  # split progress between ISD construction and SCC writing
  def _isd_progress(progress: float):
    progress_callback(progress / 2)

  config : SccWriterConfiguration = config if config is not None else SccWriterConfiguration()
  _UNSUPPORTED_CHARACTERS_WARNED.clear()
  isds = ISD.generate_isd_sequence(doc, _isd_progress)

  # generate list of captions
  captions: List[_Caption] = []
  for i, (begin, isd) in enumerate(isds):

    # 25% for creating captions
    progress_callback(0.5 + (i + 1) / len(isds) / 4)
    LOGGER.debug("Processing ISD at %ss to SCC content", float(begin))

    if len(captions) > 0 and captions[-1].get_end() is None:
      captions[-1].set_end(begin)

    non_empty_region_cnt = 0
    for r in isd.iter_regions():
      if len(r) > 0:
        non_empty_region_cnt += 1
        if non_empty_region_cnt > 1:
          # SCC can only handle one region at a time
          LOGGER.warning("Merging multiple regions at %ss; errors may result", float(begin))
          break

    if non_empty_region_cnt == 0:
      # skip empty ISD
      continue

    caption: _Caption = _Caption.from_regions(list(isd.iter_regions()))
    caption.set_begin(begin)
    caption.set_position(next((_position_of(r) for r in isd.iter_regions() if len(r) > 0), None))

    if len(caption) == 0:
      # skip ISD with no text
      continue

    if any(len(e) > MAX_LINEWIDTH for e in caption):
      if config.allow_reflow:
        LOGGER.warning("Line width exceeded at %ss", float(begin))
      else:
        raise RuntimeError(f"Line width exceeded at {float(begin)}s, reflow disabled")
      # merge the lines of the caption, remove duplicate spaces and reflow
      reflowed_lines = _reflow(re.sub(' +', ' ', ' '.join(caption)))

      caption.set_lines(reflowed_lines)

    captions.append(caption)

  is_rollup = False
  if not config.force_popon:
    # detect roll-up captions
    rollup_count = 0
    for i in range(1, len(captions)):
      # do not detect roll-up if successive captions are more than 1 frame apart
      # ideally we would ignore successive captions unless they are contiguous,
      # but many tools incorrectly set end times to be inclusive instead of exclusive
      if abs(captions[i - 1].get_end() - captions[i].get_begin()) > config.rollup_gap_tolerance:
        continue

      if captions[i][-1].startswith(captions[i - 1][-1]) or \
        len(captions[i]) > 1 and captions[i][-2] == captions[i - 1][-1]:
        rollup_count = rollup_count + 1

    if len(captions) > 10:
      is_rollup = 100 * rollup_count / (len(captions) + 1) > config.rollup_detection_pct
    else:
      is_rollup = rollup_count > 0

  if is_rollup:
    chunks = _process_as_rollup(captions, config, progress_callback)
  else:
    chunks = _process_as_popon(captions, config, progress_callback)

  start_offset = 0
  if config.start_tc is not None:
    start_tc = SmpteTimeCode.parse(config.start_tc, config.frame_rate.fps)
    if start_tc.is_drop_frame() != config.frame_rate.df:
      raise RuntimeError("The drop-frame status of the specified start_timecode does not match the drop-frame status of the specified frame_rate")
    start_offset = start_tc.to_frames()

  for chunk in chunks:
    if start_offset + chunk.get_begin() < 0:
      raise RuntimeError("The SCC stream would start earlier than the specified start timecode")

  return "Scenarist_SCC V1.0\n\n" + "\n\n".join(map(lambda e: e.to_string(config.frame_rate.fps, config.frame_rate.df, start_offset), chunks))
