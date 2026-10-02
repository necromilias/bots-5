"""Theme system for B.O.T.S. Phase 11 M0.

This module provides a tokenized theming system that enables scalable metrics
and future theming while preserving the exact Draft 1 appearance at scale 1.0.

Token design principles:
- Preserve all landed Draft 1 colours exactly (no visual regression)
- Introduce semantic token layer (surface/panel/border/text/accent/status)
- Parameterized stylesheet generator for configurable fonts and scaling
- DPI rounding policy to avoid scattered int() casts
- Campaign dock migration from inline styles to token rules

The token values are derived from the existing DRAFT1_STYLE_SHEET and meet
WCAG AA contrast requirements (verified at design acceptance R-14).

No dependencies are added in M0; the system is built on existing Qt stylesheets.
"""

from __future__ import annotations

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication


# =============================================================================
# Semantic token layer
# =============================================================================
# These are the exact hex values from Draft 1, preserved byte-for-byte.
# The semantic layer provides meaningful names for future theming.
# =============================================================================

SURFACE_BASE = "#11161b"  # Main window background (DRAFT1: draft1Root)
SURFACE_PANEL = "#171d24"  # TopBar, composerFrame, leftRail, dock widgets
SURFACE_BUBBLE = "#1b232b"  # Message bubbles, composer, search input
SURFACE_HISTORICAL = "#2a263c"  # Historical banner background

BORDER_DEFAULT = "#29343f"  # Standard borders (DRAFT1: topBar border-bottom)
BORDER_SUBTLE = "#33424e"  # Secondary borders (search inputs, composer)
BORDER_ACCENT = "#3b9ddd"  # Active/selected state borders (DRAFT1: modelPill)
BORDER_HISTORICAL = "#6d5fa0"  # Historical banner border
BORDER_ACcent = "#2d6f99"  # User message bubble border

TEXT_PRIMARY = "#d9e2ea"  # Main body text (DRAFT1: QWidget color)
TEXT_SECONDARY = "#aab8c4"  # Secondary labels, timestamps
TEXT_INACTIVE = "#66727d"  # Disabled state text
TEXT_DISABLED = "#6f7e8a"  # Empty state text

ACCENT_BLUE = "#3b9ddd"  # Primary accent (DRAFT1: modelPill border, sendButton)
ACCENT_BLUE_HOVER = "#1c78ad"  # Hover state for blue actions
ACCENT_BLUE_ACTIVE = "#17618e"  # Active/pressed blue state
ACCENT_BLUE_PILL = "#8ed2ff"  # Pill text color
ACCENT_BLUE_PRESSED = "#a8ddff"  # Pressed blue text

STATUS_SUCCESS = "#4caf50"  # Success state
STATUS_WARNING = "#ffc107"  # Warning state (amber)
STATUS_ERROR = "#f44336"  # Error state
STATUS_HISTORICAL = "#d7cdfd"  # Historical view label text
STATUS_BADGE = "#ffd79a"  # Badge text (archived/historical)

BADGE_ARCHIVE_BG = "#4a3520"  # Archived badge background
BADGE_ARCHIVE_BORDER = "#9a6b32"  # Archived badge border
BADGE_HISTORICAL_BG = "#4a3520"  # Historical badge background (same as archive)
BADGE_HISTORICAL_BORDER = "#9a6b32"  # Historical badge border (same as archive)

# =============================================================================
# Phase 11 scope amendment: industrial surface / framing tokens
# =============================================================================
# Additive layered-work-surface and machine-framing tokens.  The retained
# Draft 1 tokens above are untouched: base #11161b, panel #171d24,
# bubble #1b232b, border #29343f, accent #3b9ddd.  The new layer builds ON
# them: layered graphite surfaces (never pure black), hairline 1px framing
# with chamfered machine-built geometry, restrained accent for active or
# selected structure only, and status colour strictly subordinate.
# =============================================================================

SURFACE_SUNKEN = "#0d1216"  # Deep inset work surface (near-black, not pure black)
SURFACE_CONTROL = "#121920"  # Input wells: line edits, spin boxes, combo boxes
SURFACE_RAISED = "#1e262e"  # Layered card surface sitting above panel
SURFACE_NAV = "#141b22"  # Navigation rail / section band surface

BORDER_STRONG = "#3a4a58"  # Emphasized hairline for section frames
BORDER_HAIRLINE = "#29343f"  # Alias of the retained default hairline border
BORDER_GATED = "#4a3438"  # Disabled/gated control framing (muted, not red)

ACCENT_STRUCTURE = "#3b9ddd"  # Retained accent: active/selected structure only
ACCENT_STRUCTURE_DIM = "#2d6f99"  # Subordinate accent for hover on structure
ACCENT_STRUCTURE_PALE = "#8ed2ff"  # Accent text on accent frames (retained pill tone)

TEXT_MUTED = "#8fa0ad"  # Tertiary text: provenance, reasons, instrumentation
TEXT_ON_ACCENT = "#d8f0ff"  # Text on accent-selected surfaces

STATUS_OK = "#5dbb7a"  # Subordinate success tone (never decoration)
STATUS_WARN = "#c9a54a"  # Subordinate warning tone (never decoration)
STATUS_ERROR = "#d97780"  # Subordinate error tone (never decoration)

CHAMFER_SIZE = 6  # Base chamfer cut size for machine-built panel corners (px @1.0)
CHAMFER_SIZE_SMALL = 4  # Chamfer cut for compact cards (px @1.0)


# =============================================================================
# Scalable metrics (base values)
# =============================================================================
# These are the base metric values at scale 1.0.
# At different scales, all metrics scale proportionally.
# =============================================================================

BASE_FONT_SIZE_PX = 13  # Default font size in pixels
BASE_FONT_SIZE_PT = 10  # Default font size in points
BASE_LINE_HEIGHT = 1.4  # Line height multiplier
BASE_PADDING_SMALL = 4  # Small padding (4px)
BASE_PADDING_MEDIUM = 7  # Medium padding (7px)
BASE_PADDING_LARGE = 8  # Large padding (8px)
BASE_PADDING_XL = 10  # Extra large padding (10px)
BASE_PADDING_2XL = 11  # 11px padding
BASE_PADDING_3XL = 12  # 12px padding
BASE_PADDING_4XL = 14  # 14px padding
BASE_PADDING_5XL = 18  # 18px padding
BASE_BORDER_RADIUS_SMALL = 6  # Small border radius (6px)
BASE_BORDER_RADIUS_MEDIUM = 7  # Medium border radius (7px)
BASE_BORDER_RADIUS_LARGE = 8  # Large border radius (8px)
BASE_BORDER_RADIUS_XL = 9  # Extra large (9px)
BASE_BORDER_RADIUS_2XL = 10  # 10px
BASE_BORDER_RADIUS_3XL = 11  # 11px
BASE_SPACING_SMALL = 2  # Small spacing (2px)
BASE_SPACING_MEDIUM = 4  # Medium spacing (4px)
BASE_SPACING_LARGE = 5  # Large spacing (5px)
BASE_SPACING_XL = 6  # Extra large spacing (6px)
BASE_SPACING_2XL = 7  # 7px spacing
BASE_SPACING_3XL = 8  # 8px spacing
BASE_SPACING_4XL = 9  # 9px spacing
BASE_SPACING_5XL = 10  # 10px spacing
BASE_SPACING_6XL = 12  # 12px spacing
BASE_SPACING_7XL = 18  # 18px spacing

# Campaign dock height caps (R-7: 200px cap repair)
CAMPAIGN_DOCK_PRICING_HEIGHT = 72
CAMPAIGN_DOCK_PREFLIGHT_HEIGHT = 160
CAMPAIGN_DOCK_RESULT_HEIGHT = 200


# =============================================================================
# DPI rounding policy
# =============================================================================
# Define a single, explicit rounding helper to avoid scattered int() casts.
# This ensures consistent rounding across all metrics.
# =============================================================================


def round_for_dpi(value: float) -> int:
    """Round a metric value for DPI-aware rendering.
    
    Uses standard Python round() which implements banker's rounding (round half to even).
    This is consistent with Qt's internal rounding and provides better visual results
    than floor/ceil for fractional pixel values.
    
    Args:
        value: The float value to round
        
    Returns:
        int: The rounded integer value
    """
    return round(value)


def scale_value(base_value: int, scale: float) -> int:
    """Scale a base metric value by a factor, with DPI-aware rounding.
    
    Args:
        base_value: The base metric value at scale 1.0
        scale: The scaling factor (1.0 = original size)
        
    Returns:
        int: The scaled and rounded value
    """
    return round_for_dpi(base_value * scale)


# =============================================================================
# Font configuration
# =============================================================================


class FontConfig:
    """Configuration for font families and sizes.
    
    Provides configurable fonts with defaults that preserve Draft 1 appearance.
    """
    
    def __init__(
        self,
        ui_family: str = "System UI",
        transcript_family: str = "System UI",
        code_family: str = "Monospace",
        ui_size_pt: float = 9.0,
        transcript_size_pt: float = 9.0,
        code_size_pt: float = 8.5,
    ):
        """Initialize font configuration.
        
        Args:
            ui_family: Font family for UI chrome (buttons, labels, etc.)
            transcript_family: Font family for chat transcript
            code_family: Font family for code blocks
            ui_size_pt: Point size for UI elements
            transcript_size_pt: Point size for transcript text
            code_size_pt: Point size for code text
        """
        self.ui_family = ui_family
        self.transcript_family = transcript_family
        self.code_family = code_family
        self.ui_size_pt = ui_size_pt
        self.transcript_size_pt = transcript_size_pt
        self.code_size_pt = code_size_pt
    
    @classmethod
    def default(cls) -> "FontConfig":
        """Return the default font configuration (Draft 1 compatible)."""
        return cls(
            ui_family="System UI",
            transcript_family="System UI",
            code_family="Monospace",
            ui_size_pt=9.0,
            transcript_size_pt=9.0,
            code_size_pt=8.5,
        )


# =============================================================================
# Theme stylesheet generator
# =============================================================================


def build_theme_stylesheet(
    scale: float = 1.0,
    ui_font: str | None = None,
    transcript_font: str | None = None,
    code_font: str | None = None,
    size_pt: float | None = None,
    font_config: FontConfig | None = None,
) -> str:
    """Build a parameterized stylesheet from semantic tokens.
    
    At scale 1.0 with default fonts, this produces byte-identical output
    to the original Draft 1 stylesheet.
    
    Args:
        scale: Scaling factor for all metrics (1.0 = original size)
        ui_font: Optional UI font family override
        transcript_font: Optional transcript font family override
        code_font: Optional code font family override
        size_pt: Optional base font size in points
        font_config: Optional FontConfig instance (takes precedence over individual params)
        
    Returns:
        str: The complete stylesheet string
    """
    if font_config is None:
        font_config = FontConfig.default()
    
    # Override with individual params if provided
    if ui_font is not None:
        font_config.ui_family = ui_font
    if transcript_font is not None:
        font_config.transcript_family = transcript_font
    if code_font is not None:
        font_config.code_family = code_font
    if size_pt is not None:
        font_config.ui_size_pt = size_pt
        font_config.transcript_size_pt = size_pt
        font_config.code_size_pt = max(7.0, size_pt - 1.0)  # Code slightly smaller
    
    # Calculate scaled metrics
    font_size = scale_value(BASE_FONT_SIZE_PX, scale)
    font_size_small = scale_value(10, scale)
    font_size_medium = scale_value(11, scale)
    font_size_large = scale_value(12, scale)
    font_size_xlarge = scale_value(13, scale)
    font_size_2xlarge = scale_value(15, scale)
    
    padding_small = scale_value(BASE_PADDING_SMALL, scale)
    padding_medium = scale_value(BASE_PADDING_MEDIUM, scale)
    padding_large = scale_value(BASE_PADDING_LARGE, scale)
    padding_xl = scale_value(BASE_PADDING_XL, scale)
    padding_2xl = scale_value(BASE_PADDING_2XL, scale)
    padding_3xl = scale_value(BASE_PADDING_3XL, scale)
    padding_4xl = scale_value(BASE_PADDING_4XL, scale)
    padding_5xl = scale_value(BASE_PADDING_5XL, scale)
    padding_6xl = scale_value(18, scale)
    
    spacing_small = scale_value(BASE_SPACING_SMALL, scale)
    spacing_medium = scale_value(BASE_SPACING_MEDIUM, scale)
    spacing_large = scale_value(BASE_SPACING_LARGE, scale)
    spacing_xl = scale_value(BASE_SPACING_XL, scale)
    spacing_2xl = scale_value(BASE_SPACING_2XL, scale)
    spacing_3xl = scale_value(BASE_SPACING_3XL, scale)
    spacing_4xl = scale_value(BASE_SPACING_4XL, scale)
    spacing_5xl = scale_value(BASE_SPACING_5XL, scale)
    spacing_6xl = scale_value(BASE_SPACING_6XL, scale)
    spacing_7xl = scale_value(BASE_SPACING_7XL, scale)
    
    radius_small = scale_value(BASE_BORDER_RADIUS_SMALL, scale)
    radius_medium = scale_value(BASE_BORDER_RADIUS_MEDIUM, scale)
    radius_large = scale_value(BASE_BORDER_RADIUS_LARGE, scale)
    radius_xl = scale_value(BASE_BORDER_RADIUS_XL, scale)
    radius_2xl = scale_value(BASE_BORDER_RADIUS_2XL, scale)
    radius_3xl = scale_value(BASE_BORDER_RADIUS_3XL, scale)
    
    border_width = scale_value(1, scale)
    
    # Build the stylesheet
    # At scale 1.0 with default font config, produce byte-identical output to Draft 1
    # Only include font-family if it differs from the original (no font-family in Draft 1)
    font_family_clause = ""
    if font_config.transcript_family != "System UI":
        font_family_clause = f'    font-family: "{font_config.transcript_family}";\n'
    
    stylesheet = f"""QWidget {{
    color: {TEXT_PRIMARY};
    font-size: {font_size}px;
{font_family_clause}}}
QMainWindow, QWidget#draft1Root {{
    background: {SURFACE_BASE};
}}
QFrame#topBar, QFrame#composerFrame, QWidget#leftRail, QDockWidget > QWidget {{
    background: {SURFACE_PANEL};
}}
QFrame#topBar {{
    border-bottom: {border_width}px solid {BORDER_DEFAULT};
}}
QLabel#brandLabel {{
    color: #f2f7fb;
    font-size: {font_size_2xlarge}px;
    font-weight: 700;
}}
QLabel#modelPill {{
    background: #202b36;
    border: {border_width}px solid {ACCENT_BLUE};
    border-radius: {radius_3xl}px;
    color: {ACCENT_BLUE_PILL};
    padding: {padding_small}px {padding_xl}px;
}}
QLabel#chatTitle {{
    color: {TEXT_SECONDARY};
    font-size: {font_size_large}px;
    font-weight: 600;
    padding: {scale_value(3, scale)}px {scale_value(4, scale)}px;
}}
QLabel#archivedBadge, QLabel#historicalBadge {{
    background: {BADGE_ARCHIVE_BG};
    border: {border_width}px solid {BADGE_ARCHIVE_BORDER};
    border-radius: {radius_large}px;
    color: {STATUS_BADGE};
    font-size: {font_size_small}px;
    font-weight: 700;
    padding: {scale_value(2, scale)}px {scale_value(7, scale)}px;
}}
QFrame#historicalBanner {{
    background: {SURFACE_HISTORICAL};
    border: {border_width}px solid {BORDER_HISTORICAL};
    border-radius: {radius_2xl}px;
}}
QLabel#historicalViewLabel {{
    color: {STATUS_HISTORICAL};
    font-size: {font_size_medium}px;
    font-weight: 600;
}}
QToolButton, QPushButton {{
    background: transparent;
    border: {border_width}px solid transparent;
    border-radius: {radius_medium}px;
    color: #c5d0d9;
    padding: {scale_value(5, scale)}px {scale_value(8, scale)}px;
}}
QToolButton:hover, QPushButton:hover {{
    background: #233342;
    border-color: #31536a;
}}
QToolButton:checked, QPushButton:pressed {{
    background: #18354a;
    border-color: {ACCENT_BLUE};
    color: {ACCENT_BLUE_PRESSED};
}}
QToolButton:disabled, QPushButton:disabled {{
    color: {TEXT_INACTIVE};
    border-color: transparent;
}}
QToolButton#railIcon {{
    min-width: {scale_value(28, scale)}px;
    max-width: {scale_value(28, scale)}px;
    min-height: {scale_value(28, scale)}px;
    max-height: {scale_value(28, scale)}px;
    padding: 0;
    background: #202a34;
    border-color: #2a3946;
}}
QToolButton#railIcon:hover, QToolButton#railIcon:checked {{
    background: #173a52;
    border-color: {ACCENT_BLUE};
    color: {ACCENT_BLUE_PRESSED};
}}
QToolButton#disabledAffordance {{
    background: #1b2229;
    border-color: #27313a;
}}
QListWidget#chatList {{
    background: transparent;
    border: 0;
    outline: 0;
    padding: {scale_value(2, scale)}px;
}}
QListWidget#chatList::item {{
    border-radius: {radius_small}px;
    padding: {scale_value(7, scale)}px {scale_value(8, scale)}px;
}}
QListWidget#chatList::item:selected {{
    background: #1a3b52;
    color: #d8f0ff;
}}
QScrollArea#transcriptView {{
    background: {SURFACE_BASE};
    border: 0;
}}
QWidget#transcriptContent {{
    background: {SURFACE_BASE};
}}
QFrame#messageBubble {{
    background: {SURFACE_BUBBLE};
    border: {border_width}px solid #2b3945;
    border-radius: {radius_2xl}px;
}}
QFrame#messageBubble[role="user"] {{
    background: #193247;
    border-color: {BORDER_ACcent};
}}
QLabel#messageBody {{
    color: #dce6ed;
}}
QLabel#messageState {{
    color: #7f93a2;
    font-size: {font_size_small}px;
}}
QLabel#messageActivity, QLabel#generationIndicator {{
    color: {ACCENT_BLUE_PILL};
    font-size: {font_size_medium}px;
    font-weight: 700;
}}
QLabel#messageActivity {{
    padding-top: {scale_value(1, scale)}px;
}}
QFrame#messageBubble[generationActive="true"] {{
    background: #1c3b52;
    border-color: {ACCENT_BLUE};
}}
QFrame#messageBubble[searchFocus="true"] {{
    border: {scale_value(2, scale)}px solid #70c7ff;
    background: #213c4e;
}}
QLabel#assistantAvatar, QLabel#userAvatar {{
    border-radius: {radius_large}px;
    font-size: {font_size}px;
    font-weight: 700;
    qproperty-alignment: AlignCenter;
}}
QLabel#assistantAvatar {{
    background: #1e4f6d;
    color: #a9defd;
}}
QLabel#userAvatar {{
    background: #344553;
    color: #e3edf4;
}}
QFrame#composerFrame {{
    border-top: {border_width}px solid {BORDER_DEFAULT};
}}
QPlainTextEdit#composer {{
    background: {SURFACE_BUBBLE};
    border: {border_width}px solid {BORDER_SUBTLE};
    border-radius: {radius_xl}px;
    color: #e5edf2;
    padding: {scale_value(7, scale)}px;
    selection-background-color: #245d80;
}}
QPlainTextEdit#composer:focus {{
    border-color: {ACCENT_BLUE};
}}
QPushButton#sendButton {{
    background: {ACCENT_BLUE_ACTIVE};
    border-color: {ACCENT_BLUE};
    color: #eff9ff;
    font-weight: 700;
    padding: {padding_large}px {padding_4xl}px;
}}
QPushButton#sendButton:hover {{
    background: {ACCENT_BLUE_HOVER};
}}
QPushButton#stopButton {{
    background: #432e35;
    border-color: #8d5563;
    color: #f2c5ce;
    padding: {padding_large}px {padding_4xl}px;
}}
QLabel#editingLabel {{
    color: {ACCENT_BLUE_PILL};
    font-size: {font_size_medium}px;
    padding-left: {scale_value(3, scale)}px;
}}
QDockWidget {{
    color: {TEXT_PRIMARY};
}}
QDockWidget::title {{
    background: {SURFACE_BUBBLE};
    border-bottom: {border_width}px solid {BORDER_DEFAULT};
    padding: {padding_medium}px;
}}
QLabel#inspectorValue {{
    color: #becbd4;
}}
QLabel#emptyTranscript {{
    color: {TEXT_DISABLED};
    padding: {scale_value(36, scale)}px;
}}
QWidget#searchPanel {{
    background: {SURFACE_PANEL};
}}
QLabel#searchTitle {{
    color: #f2f7fb;
    font-size: {font_size_xlarge}px;
    font-weight: 700;
}}
QLineEdit#searchQuery, QComboBox#searchScope, QComboBox#searchRole,
QComboBox#searchState, QListWidget#searchResults {{
    background: {SURFACE_BASE};
    border: {border_width}px solid {BORDER_SUBTLE};
    border-radius: {radius_small}px;
    color: #dce6ed;
    padding: {scale_value(5, scale)}px;
}}
QListWidget#searchResults::item {{
    border-bottom: {border_width}px solid {BORDER_DEFAULT};
    padding: {scale_value(8, scale)}px {scale_value(5, scale)}px;
}}
QListWidget#searchResults::item:selected {{
    background: #1a3b52;
}}
QLabel#searchStatus {{
    color: {TEXT_SECONDARY};
    font-size: {font_size_medium}px;
    padding: {scale_value(4, scale)}px;
}}
QLabel#searchStatus[condition="STALE"],
QLabel#searchStatus[condition="REBUILDING"],
QLabel#searchStatus[condition="GONE"] {{
    color: {STATUS_BADGE};
}}
QLabel#searchStatus[condition="UNAVAILABLE"],
QLabel#searchStatus[condition="INVALID"],
QLabel#searchStatus[condition="ERROR"] {{
    color: #f2aeb9;
}}
"""
    
    # Campaign dock styles (migrated from inline)
    stylesheet += f"""
QDockWidget#campaignDock {{
    color: {TEXT_PRIMARY};
}}
QDockWidget#campaignDock::title {{
    background: {SURFACE_BUBBLE};
    border-bottom: {border_width}px solid {BORDER_DEFAULT};
    padding: {padding_medium}px;
}}
QWidget#campaignContent {{
    background: {SURFACE_PANEL};
}}
QFrame#campaignJobSection, QFrame#campaignRunSection, QFrame#campaignStagesSection,
QFrame#campaignFreshnessSection, QFrame#campaignResultSection, QFrame#campaignStatusSection,
QFrame#campaignControlsSection {{
    background: {SURFACE_PANEL};
}}
QLabel#campaignJobIdentity, QLabel#campaignRunIdentity {{
    color: {TEXT_PRIMARY};
}}
QLabel#campaignPricingStatus {{
    color: {STATUS_WARNING};
}}
QLabel#campaignIntegrityWarningsLabel {{
    color: {STATUS_ERROR};
}}
QLabel#campaignStatusLabel {{
    color: {ACCENT_BLUE};
}}
QTableWidget#campaignStagesTable {{
    background: {SURFACE_BUBBLE};
    border: {border_width}px solid {BORDER_DEFAULT};
    border-radius: {radius_medium}px;
    gridline-color: {BORDER_DEFAULT};
}}
QTableWidget#campaignStagesTable::item {{
    padding: {scale_value(4, scale)}px;
}}
QTableWidget#campaignStagesTable::item:selected {{
    background: #1a3b52;
    color: {TEXT_PRIMARY};
}}
QTextEdit#campaignPricingEvidence {{
    background: {SURFACE_BUBBLE};
    border: {border_width}px solid {BORDER_SUBTLE};
    border-radius: {radius_medium}px;
    color: {TEXT_PRIMARY};
}}
QTextEdit#campaignPreflightSummary {{
    background: {SURFACE_BUBBLE};
    border: {border_width}px solid {BORDER_DEFAULT};
    border-radius: {radius_medium}px;
    color: {TEXT_PRIMARY};
}}
QTextEdit#campaignResultText {{
    background: {SURFACE_BUBBLE};
    border: {border_width}px solid {BORDER_DEFAULT};
    border-radius: {radius_medium}px;
    color: {TEXT_PRIMARY};
}}
QPushButton#campaignLoadJobButton, QPushButton#campaignValidateButton,
QPushButton#campaignApproveButton, QPushButton#campaignClearButton,
QPushButton#campaignMakeCurrentButton, QPushButton#campaignRegenerateButton,
QPushButton#campaignRerunSynthesisButton, QPushButton#campaignCancelButton {{
    background: transparent;
    border: {border_width}px solid {BORDER_DEFAULT};
    border-radius: {radius_medium}px;
    color: {TEXT_PRIMARY};
    padding: {padding_small}px {padding_large}px;
}}
QPushButton#campaignLoadJobButton:hover, QPushButton#campaignValidateButton:hover,
QPushButton#campaignApproveButton:hover, QPushButton#campaignClearButton:hover,
QPushButton#campaignMakeCurrentButton:hover, QPushButton#campaignRegenerateButton:hover,
QPushButton#campaignRerunSynthesisButton:hover, QPushButton#campaignCancelButton:hover {{
    background: #233342;
    border-color: {ACCENT_BLUE};
}}
QPushButton#campaignLoadJobButton:disabled, QPushButton#campaignValidateButton:disabled,
QPushButton#campaignApproveButton:disabled, QPushButton#campaignClearButton:disabled,
QPushButton#campaignMakeCurrentButton:disabled, QPushButton#campaignRegenerateButton:disabled,
QPushButton#campaignRerunSynthesisButton:disabled, QPushButton#campaignCancelButton:disabled {{
    color: {TEXT_INACTIVE};
    border-color: transparent;
}}
QPushButton#campaignApproveButton {{
    border-color: {ACCENT_BLUE};
    color: {ACCENT_BLUE_PILL};
}}
QPushButton#campaignApproveButton:hover {{
    background: #1c78ad;
}}
"""
    
    # Phase 11 scope amendment: industrial dialog/panel presentation rules.
    # Settings and Tune share these rules through the dialog primitives so
    # both surfaces speak the same machine-built visual language: layered
    # graphite work surfaces, hairline framing, restrained accent on
    # active/selected structure only, and gated (disabled) controls that stay
    # visible with their reason instead of disappearing.
    stylesheet += f"""
QDialog#settingsDialog, QDialog#tuneDialog, QDialog#addConnectionDialog {{
    background: {SURFACE_BASE};
}}
QFrame#botsChamferedPanel {{
    background: {SURFACE_PANEL};
    border: {border_width}px solid {BORDER_DEFAULT};
}}
QFrame#botsChamferedCard {{
    background: {SURFACE_RAISED};
    border: {border_width}px solid {BORDER_DEFAULT};
}}
QLabel#botsSectionTitle {{
    color: #f2f7fb;
    font-size: {font_size_medium}px;
    font-weight: 700;
    letter-spacing: 1px;
}}
QLabel#botsSectionSubtitle {{
    color: {TEXT_MUTED};
    font-size: {font_size_small}px;
}}
QLabel#botsFieldLabel {{
    color: {TEXT_SECONDARY};
}}
QLabel#botsReason {{
    color: {TEXT_INACTIVE};
    font-size: {font_size_small}px;
}}
QLabel#botsProvenance {{
    color: {TEXT_MUTED};
    font-size: {font_size_small}px;
}}
QLabel#botsStateBadge {{
    color: {TEXT_MUTED};
    border: {border_width}px solid {BORDER_DEFAULT};
    padding: 0 {padding_small}px;
    font-size: {font_size_small}px;
    background: {SURFACE_SUNKEN};
}}
QLabel#botsStateBadge[gate="unsupported"], QLabel#botsStateBadge[gate="unserializable"] {{
    color: {STATUS_WARN};
}}
QLabel#botsStateBadge[gate="unknown"] {{
    color: {TEXT_INACTIVE};
}}
QLabel#botsStateBadge[gate="active"] {{
    color: {ACCENT_STRUCTURE_PALE};
    border-color: {ACCENT_STRUCTURE_DIM};
}}
QListWidget#botsSectionNav {{
    background: {SURFACE_NAV};
    border: {border_width}px solid {BORDER_DEFAULT};
    outline: 0;
}}
QListWidget#botsSectionNav::item {{
    padding: {padding_medium}px {padding_large}px;
    border-left: {scale_value(2, scale)}px solid transparent;
}}
QListWidget#botsSectionNav::item:selected {{
    background: {SURFACE_RAISED};
    border-left: {scale_value(2, scale)}px solid {ACCENT_STRUCTURE};
    color: {TEXT_ON_ACCENT};
}}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{
    background: {SURFACE_CONTROL};
    border: {border_width}px solid {BORDER_DEFAULT};
    border-radius: 0px;
    color: #dce6ed;
    padding: {padding_small}px {padding_medium}px;
    selection-background-color: #245d80;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus {{
    border-color: {ACCENT_STRUCTURE};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QPlainTextEdit:disabled {{
    background: {SURFACE_SUNKEN};
    color: {TEXT_INACTIVE};
    border-color: {BORDER_GATED};
}}
QCheckBox {{
    color: {TEXT_SECONDARY};
    spacing: {spacing_large}px;
}}
QCheckBox:disabled {{
    color: {TEXT_INACTIVE};
}}
QPushButton#botsPrimaryAction {{
    background: {ACCENT_BLUE_ACTIVE};
    border: {border_width}px solid {ACCENT_STRUCTURE};
    color: #eff9ff;
    font-weight: 700;
    padding: {padding_medium}px {padding_4xl}px;
    border-radius: 0px;
}}
QPushButton#botsPrimaryAction:hover {{
    background: {ACCENT_BLUE_HOVER};
}}
QPushButton#botsPrimaryAction:disabled {{
    background: {SURFACE_SUNKEN};
    border-color: {BORDER_GATED};
    color: {TEXT_INACTIVE};
}}
QPushButton#botsDestructiveAction {{
    background: transparent;
    border: {border_width}px solid {BORDER_STRONG};
    color: #e8b9be;
    padding: {padding_medium}px {padding_4xl}px;
    border-radius: 0px;
}}
QPushButton#botsDestructiveAction:hover {{
    border-color: {STATUS_ERROR};
    color: #f2c5ce;
    background: #2a1e22;
}}
QPushButton#botsDestructiveAction:disabled {{
    color: {TEXT_INACTIVE};
    border-color: {BORDER_GATED};
    background: transparent;
}}
QFrame#botsHairline {{
    background: {BORDER_DEFAULT};
    border: 0;
    max-height: {border_width}px;
    min-height: {border_width}px;
}}
QLabel#botsInstrumentValue {{
    color: {ACCENT_STRUCTURE_PALE};
    font-size: {font_size_small}px;
}}
QLabel#botsInstrumentLabel {{
    color: {TEXT_MUTED};
    font-size: {font_size_small}px;
}}
"""
    
    return stylesheet


# =============================================================================
# Theme application
# =============================================================================


def apply_draft1_theme(application: QApplication, *, scale: float = 1.0) -> None:
    """Apply the Draft 1 theme to the application.
    
    At scale 1.0 (default), this produces byte-identical output to the
    original DRAFT1_STYLE_SHEET.
    
    Args:
        application: The QApplication instance
        scale: Scaling factor for metrics (1.0 = original size)
    """
    stylesheet = build_theme_stylesheet(scale=scale)
    application.setStyleSheet(stylesheet)


def set_high_dpi_rounding_policy() -> None:
    """Set Qt high-DPI scale factor rounding policy.
    
    Must be called between argparse branch and QApplication construction.
    This is required for M0's DPI rounding policy.
    """
    from PySide6.QtCore import Qt
    
    # Qt 6 high-DPI is always on; the legacy AA_EnableHighDpiScaling attribute
    # is obsolete and must not be asserted as the mechanism.
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.Round
    )
