"""
presence.slides — Convert Markdown to a PDF slideshow.

Public API:
    from presence.slides import convert
    convert(input_path, output_path, theme_name, ratio, logo_path, thumbnails)

CLI usage:
    python -m presence.slides input.md output.pdf [--theme dark] [--watch] …
"""

from .convert import convert

__all__ = ["convert"]
