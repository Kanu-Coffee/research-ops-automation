"""Presentation-only decimal file sizes; stored byte counts stay exact."""

from html import escape
from itertools import count


SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")
_size_ids = count(1)


def format_size(value):
    if type(value) is not int or value < 0:
        return "—"
    unit, scale = 0, 1
    while unit < len(SIZE_UNITS) - 1 and value >= scale * 1000:
        unit, scale = unit + 1, scale * 1000
    if unit == 0:
        return f"{value:,} B"
    tenths = (value * 10 + scale // 2) // scale
    if tenths >= 10000 and unit < len(SIZE_UNITS) - 1:
        unit, scale = unit + 1, scale * 1000
        tenths = (value * 10 + scale // 2) // scale
    whole, fraction = divmod(tenths, 10)
    number = f"{whole:,}" + (f".{fraction}" if fraction else "")
    return f"{number} {SIZE_UNITS[unit]}"


def render_size(value):
    short = format_size(value)
    if short == "—" or value < 1000:
        return short
    exact = f"{value:,} B"
    target = f"size-exact-{next(_size_ids)}"
    return (f'<span class="size-value" title="{exact}"><button type="button" class="size-toggle" '
            f'data-size-toggle data-size-bytes="{value}" aria-expanded="false" aria-controls="{target}"><span aria-hidden="true">{escape(short)}</span>'
            f'<span class="sr-only">{escape(short)} (정확히 {exact})</span></button>'
            f'<span class="size-exact" id="{target}" data-size-exact hidden>({exact})</span></span>')


SIZE_SCRIPT = r"""
window.researchopsFormatSize = value => {
  if (!Number.isSafeInteger(value) || value < 0) return '—';
  const units = ['B','KB','MB','GB','TB'];
  let unit = 0, scale = 1;
  while (unit < units.length - 1 && value >= scale * 1000) {unit++; scale *= 1000;}
  if (!unit) return value.toLocaleString('en-US') + ' B';
  let tenths = Math.round(value / scale * 10);
  if (tenths >= 10000 && unit < units.length - 1) {unit++; scale *= 1000; tenths = Math.round(value / scale * 10);}
  return (tenths / 10).toLocaleString('en-US', {maximumFractionDigits:1}) + ' ' + units[unit];
};
"""
