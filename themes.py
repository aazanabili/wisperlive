"""Design tokens: theme palettes + the single shared live color map."""

THEMES = {
    "dark": {
        "background": "#10131A", "surface": "#181D27", "surface_hover": "#202735",
        "border": "#2C3444", "text": "#F4F7FB", "muted": "#A5B0C2",
        "accent": "#7C8CFF", "accent_hover": "#95A2FF", "success": "#49D39A",
        "warning": "#F5C761", "danger": "#FF667D", "success_bg": "#15251F",
        "warning_bg": "#322A17", "danger_bg": "#311B25", "indicator": "#0D1728",
        "indicator_border": "#27466F", "indicator_glow": "#1F4D84", "indicator_accent": "#76ABFF",
        "indicator_text": "#EDF4FF",
    },
    "light": {
        "background": "#F4F6FA", "surface": "#FFFFFF", "surface_hover": "#EDF1F7",
        "border": "#CED6E3", "text": "#172033", "muted": "#58657A",
        "accent": "#4B5FD5", "accent_hover": "#3D4FB6", "success": "#087A52",
        "warning": "#9C6500", "danger": "#C9364E", "success_bg": "#E4F5EC",
        "warning_bg": "#FFF4D8", "danger_bg": "#FDE9ED", "indicator": "#F8FBFF",
        "indicator_border": "#BDD2F2", "indicator_glow": "#DCEAFF", "indicator_accent": "#397BE0",
        "indicator_text": "#172F54",
    },
}
COLORS = THEMES["dark"].copy()  # THE single shared live token map (one instance for the whole app)
