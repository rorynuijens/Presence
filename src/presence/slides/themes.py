"""
themes.py — Theme dataclass, built-in theme definitions, and aspect ratios.

A Theme is a structured object rather than a plain dict.  All fields have
defaults so theme authors only need to specify what differs from the base.

The BUILTIN_THEMES registry is keyed by slug (lowercase, filesystem-safe).
User-installed themes are loaded by theme_loader.py and merged on top.
"""

from dataclasses import dataclass, field


@dataclass
class Theme:
    # ── Identity ──────────────────────────────────────────────────────────────
    name:    str = "Light"
    slug:    str = "light"
    author:  str = "Built-in"
    version: str = "1.0"
    description: str = ""

    # ── Palette ───────────────────────────────────────────────────────────────
    bg:            str = "#ffffff"
    fg:            str = "#1a1a2e"
    accent:        str = "#E17000"
    accent2:       str = ""
    heading_color: str = ""
    code_bg:       str = "#f0f4f8"

    # ── Title / cover slide ───────────────────────────────────────────────────
    title_bg:     str = "#1a1a2e"
    title_fg:     str = "#ffffff"
    title_accent: str = ""

    # ── Typography ────────────────────────────────────────────────────────────
    body_font:    str = ("'IBM Plex Sans', 'Liberation Sans', "
                         "'Cantarell', Arial, sans-serif")
    heading_font: str = ""
    mono_font:    str = ("'Fira Code', 'Liberation Mono', "
                         "'Courier New', monospace")
    base_size:    int = 34

    # ── Code highlighting ─────────────────────────────────────────────────────
    pygments_style: str = "friendly"

    # ── Callout boxes — (bg, fg, border) colour triples ──────────────────────
    callout_tip:     tuple = ("#e6f4ea", "#2d6a4f", "#52b788")
    callout_info:    tuple = ("#e8f0fe", "#1a56db", "#3b82f6")
    callout_warning: tuple = ("#fff8e1", "#b45309", "#f59e0b")
    callout_danger:  tuple = ("#fdecea", "#b91c1c", "#ef4444")

    # ── Callout icons — per-theme overridable (fixes #99) ─────────────────────
    # Each value is inserted into a CSS `content: '…'` property, so it must
    # be a single character or short Unicode string without single-quotes.
    callout_icon_tip:     str = "✓"
    callout_icon_info:    str = "i"
    callout_icon_warning: str = "!"
    callout_icon_danger:  str = "✕"

    # ── Custom CSS ────────────────────────────────────────────────────────────
    custom_css_path: str = ""

    # ── Internal: resolved font-face CSS (injected by theme_loader) ──────────
    _font_face_css: str = field(default="", repr=False)

    # ── Convenience accessors with fallback ───────────────────────────────────

    @property
    def resolved_heading_color(self) -> str:
        return self.heading_color or self.accent

    @property
    def resolved_heading_font(self) -> str:
        return self.heading_font or self.body_font

    @property
    def resolved_title_accent(self) -> str:
        return self.title_accent or self.accent

    @property
    def resolved_accent2(self) -> str:
        return self.accent2 or self.accent

    @property
    def callout_icons(self) -> dict[str, str]:
        """Return the per-theme callout icon map."""
        return {
            "tip":     self.callout_icon_tip,
            "info":    self.callout_icon_info,
            "warning": self.callout_icon_warning,
            "danger":  self.callout_icon_danger,
        }

    def to_legacy_dict(self) -> dict:
        """Return a dict compatible with code that still uses theme['key']."""
        return {
            "bg":            self.bg,
            "fg":            self.fg,
            "accent":        self.accent,
            "code_bg":       self.code_bg,
            "heading_color": self.resolved_heading_color,
            "title_bg":      self.title_bg,
            "title_fg":      self.title_fg,
            "pygments_style": self.pygments_style,
            "callout_tip":     self.callout_tip,
            "callout_info":    self.callout_info,
            "callout_warning": self.callout_warning,
            "callout_danger":  self.callout_danger,
        }


# ── Built-in themes ───────────────────────────────────────────────────────────

BUILTIN_THEMES: dict[str, Theme] = {
    "light": Theme(
        name="Light",
        slug="light",
        author="Built-in",
        description="Clean light theme with warm accent",
        bg="#ffffff",
        fg="#1a1a2e",
        accent="#E17000",
        code_bg="#f0f4f8",
        title_bg="#1a1a2e",
        title_fg="#ffffff",
        pygments_style="friendly",
    ),
    "dark": Theme(
        name="Dark",
        slug="dark",
        author="Built-in",
        description="Dark theme with warm accent",
        bg="#1a1a2e",
        fg="#e8e8f0",
        accent="#E17000",
        heading_color="#ffffff",
        code_bg="#0d0d1a",
        title_bg="#0d0d1a",
        title_fg="#ffffff",
        pygments_style="monokai",
    ),
}

ASPECT_RATIOS: dict[str, tuple[int, int]] = {
    "16:9":  (1280, 720),
    "4:3":   (1024, 768),
    "16:10": (1280, 800),
}
