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

"""SCC characters utility functions"""

from __future__ import annotations

import unicodedata

from ttconv.scc.codes.extended_characters import SccExtendedCharacter
from ttconv.scc.codes.special_characters import SccSpecialCharacter
from ttconv.scc.codes.standard_characters import TO_SCC_BYTES

# Characters of the basic set that an extended character can be replaced with by a decoder that does not have the extended
# sets (CTA-608-E 6.4.2): the standard does not give them, the others are the letter without its accent
EXTENDED_CHARACTER_FALLBACKS = {
  "\u00DF": "s", "\u00A5": "Y", "\u00A4": "o", "\u2502": "l", "\u00D8": "O", "\u00F8": "o", "\u250C": "+", "\u2510": "+",
  "\u2514": "+", "\u2518": "+", "\u2018": "'", "\u00A1": "!", "*": ".", "\u2014": "-", "\u00A9": "c", "\u2120": "S",
  "\u2022": "o", "\u201C": "\"", "\u201D": "\"", "\u00AB": "<", "\u00BB": ">", "{": "(", "}": ")", "\\": "/", "^": "A",
  "_": "-", "|": "l", "~": "-", "'": "'"
}

def _extended_character_fallback(c: str) -> int:
  """Returns the code of the character of the basic set that is sent before an extended character"""
  return ord(EXTENDED_CHARACTER_FALLBACKS.get(c) or unicodedata.normalize("NFD", c)[0])

def unicode_to_scc(c: str) -> bytes:
  """Convert a Unicode character to an SCC character string. An extended character is preceded by its fallback character,
  which a decoder that has the extended sets replaces (it backs up one column)"""

  if len(c) != 1:
    raise ValueError("Can only convert a single character")

  b = bytearray()

  scc_c = TO_SCC_BYTES.get(c, None)

  if scc_c is not None:
    b.append(scc_c)

  else:
    scc_c = SccSpecialCharacter.from_unicode(c)

    if scc_c is not None:
      b.append(scc_c.get_ch1_value() // 256)
      b.append(scc_c.get_ch1_value() % 256)

    else:
      ext_c = SccExtendedCharacter.from_unicode(c)

      if ext_c is not None:
        b.append(_extended_character_fallback(c))
        b.append(ext_c.get_ch1_value() // 256)
        b.append(ext_c.get_ch1_value() % 256)

      else:
        b.append(0x20)  # Standard

  return b