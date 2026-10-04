"""Theme system for B.O.T.S. Phase 11 M0.

This module provides a tokenized theming system that enables scalable metrics
and shared screenshot-authorized colour refinement.

Token design principles:
- Keep surface, frame, text, accent and status colours in shared tokens
- Introduce semantic token layer (surface/panel/border/text/accent/status)
- Parameterized stylesheet generator for configurable fonts and scaling
- DPI rounding policy to avoid scattered int() casts
- Campaign dock migration from inline styles to token rules

The structural palette follows Mick's colour-refinement reference. Semantic
status colours are retained; dimensions and scaling remain unchanged.

No dependencies are added in M0; the system is built on existing Qt stylesheets.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication
from . import ui_icons  # Register bundled glyphs; no external asset lookup.


# =============================================================================
# Semantic token layer
# =============================================================================
# Shared palette and compatibility aliases for existing desktop widgets.
# =============================================================================

# Mick's screenshot colour authority; all geometry and metrics remain unchanged.
BASE_CANVAS = "#040B12"
DEEP_PANEL = "#031120"
RAISED_PANEL = "#05101B"
HEADER_SURFACE = "#0B1A2A"
HEADER_HIGH = "#1E344D"
BORDER_SUBTLE = "#21364C"
BORDER_STRONG = "#28405C"
ACCENT_BLUE = "#0E67A5"
ACCENT_BLUE_DARK = "#12395D"
TEXT_PRIMARY = "#D9DBDB"
TEXT_SECONDARY = "#A0A9B2"
TEXT_MUTED = "#6B6E71"
WARNING_SURFACE = "#34250D"
WARNING_BORDER = "#96743A"

# Shared role aliases keep existing widgets on the same palette.
SURFACE_BASE = BASE_CANVAS
SURFACE_PANEL = DEEP_PANEL
SURFACE_BUBBLE = RAISED_PANEL
SURFACE_SUNKEN = BASE_CANVAS
SURFACE_CONTROL = RAISED_PANEL
SURFACE_RAISED = RAISED_PANEL
SURFACE_NAV = RAISED_PANEL
BORDER_DEFAULT = BORDER_SUBTLE
BORDER_HAIRLINE = BORDER_SUBTLE
BORDER_ACCENT = ACCENT_BLUE
BORDER_ACcent = ACCENT_BLUE_DARK  # Retained public spelling.
TEXT_INACTIVE = TEXT_MUTED
TEXT_DISABLED = TEXT_MUTED
TEXT_ON_ACCENT = TEXT_PRIMARY
ACCENT_BLUE_HOVER = ACCENT_BLUE_DARK
ACCENT_BLUE_ACTIVE = ACCENT_BLUE
ACCENT_BLUE_PILL = TEXT_PRIMARY
ACCENT_BLUE_PRESSED = TEXT_PRIMARY
ACCENT_STRUCTURE = ACCENT_BLUE
ACCENT_STRUCTURE_DIM = ACCENT_BLUE_DARK
ACCENT_STRUCTURE_PALE = TEXT_PRIMARY

# Preserve semantic status colours independently of the structural palette.
SURFACE_HISTORICAL = "#2a263c"
BORDER_HISTORICAL = "#6d5fa0"
BORDER_GATED = "#4a3438"
STATUS_SUCCESS = "#4caf50"
STATUS_WARNING = "#ffc107"
STATUS_HISTORICAL = "#d7cdfd"
STATUS_BADGE = "#ffd79a"
BADGE_ARCHIVE_BG = "#4a3520"
BADGE_ARCHIVE_BORDER = "#9a6b32"
BADGE_HISTORICAL_BG = "#4a3520"
BADGE_HISTORICAL_BORDER = "#9a6b32"
STATUS_OK = "#5dbb7a"
STATUS_WARN = "#c9a54a"
STATUS_ERROR = "#d97780"
WARNING_TEXT = "#fff2cf"

CHAMFER_SIZE = 4  # Base chamfer cut size for machine-built panel corners (px @1.0)
CHAMFER_SIZE_SMALL = 4  # Chamfer cut for compact cards (px @1.0)


# Presentation metrics shared by the desktop and secondary surfaces.
PANEL_INSET = 12
ROW_GAP = 7
SECTION_GAP = 16
CONTROL_HEIGHT = 30
DIALOG_INSET = 16
MAIN_SIZE = (1280, 800)
SETTINGS_SIZE = (1080, 760)
TUNE_SIZE = (880, 700)
PICKER_SIZE = (820, 620)

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
    
    Phase 11 recomposition adds shared shell and dialog rules to the landed
    palette, with configurable metrics and fonts.
    
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
    # Default font-family remains platform-selected unless explicitly configured.
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
QLabel#developerProviderTestBanner {{
    background: {WARNING_SURFACE}; color: {WARNING_TEXT}; border: 2px solid {WARNING_BORDER}; padding: 8px; font-weight: bold;
}}
QFrame#topBar, QFrame#composerFrame, QWidget#leftRail, QDockWidget > QWidget {{
    background: {SURFACE_PANEL};
}}
QFrame#topBar {{
    border-bottom: {border_width}px solid {BORDER_DEFAULT};
}}
QLabel#brandLabel {{
    color: {TEXT_PRIMARY};
    font-size: {font_size_2xlarge}px;
    font-weight: 700;
}}
QLabel#modelPill {{
    background: {HEADER_SURFACE};
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
    color: {TEXT_SECONDARY};
    padding: {scale_value(5, scale)}px {scale_value(8, scale)}px;
}}
QToolButton:hover, QPushButton:hover {{
    background: {HEADER_SURFACE};
    border-color: {BORDER_STRONG};
}}
QToolButton:checked, QPushButton:pressed {{
    background: {ACCENT_BLUE_DARK};
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
    background: {HEADER_SURFACE};
    border-color: {BORDER_STRONG};
}}
QToolButton#railIcon:hover, QToolButton#railIcon:checked {{
    background: {ACCENT_BLUE_DARK};
    border-color: {ACCENT_BLUE};
    color: {ACCENT_BLUE_PRESSED};
}}
QToolButton#disabledAffordance {{
    background: {RAISED_PANEL};
    border-color: {BORDER_STRONG};
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
    background: {ACCENT_BLUE_DARK};
    color: {TEXT_PRIMARY};
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
    border: {border_width}px solid {BORDER_STRONG};
    border-radius: {radius_2xl}px;
}}
QFrame#messageBubble[role="user"] {{
    background: {ACCENT_BLUE_DARK};
    border-color: {BORDER_ACcent};
}}
QLabel#messageBody {{
    color: {TEXT_PRIMARY};
}}
QLabel#messageState {{
    color: {TEXT_SECONDARY};
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
    background: {ACCENT_BLUE_DARK};
    border-color: {ACCENT_BLUE};
}}
QFrame#messageBubble[searchFocus="true"] {{
    border: {scale_value(2, scale)}px solid {ACCENT_BLUE};
    background: {ACCENT_BLUE_DARK};
}}
QLabel#assistantAvatar, QLabel#userAvatar {{
    border-radius: {radius_large}px;
    font-size: {font_size}px;
    font-weight: 700;
    qproperty-alignment: AlignCenter;
}}
QLabel#assistantAvatar {{
    background: {ACCENT_BLUE_DARK};
    color: {TEXT_PRIMARY};
}}
QLabel#userAvatar {{
    background: {HEADER_SURFACE};
    color: {TEXT_PRIMARY};
}}
QFrame#composerFrame {{
    border-top: {border_width}px solid {BORDER_DEFAULT};
}}
QPlainTextEdit#composer {{
    background: {SURFACE_BUBBLE};
    border: {border_width}px solid {BORDER_SUBTLE};
    border-radius: {radius_xl}px;
    color: {TEXT_PRIMARY};
    padding: {scale_value(7, scale)}px;
    selection-background-color: {ACCENT_BLUE_DARK};
}}
QPlainTextEdit#composer:focus {{
    border-color: {ACCENT_BLUE};
}}
QPushButton#sendButton {{
    background: {ACCENT_BLUE_ACTIVE};
    border-color: {ACCENT_BLUE};
    color: {TEXT_PRIMARY};
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
    color: {TEXT_SECONDARY};
}}
QLabel#emptyTranscript {{
    color: {TEXT_DISABLED};
    padding: {scale_value(36, scale)}px;
}}
QWidget#searchPanel {{
    background: {SURFACE_PANEL};
}}
QLabel#searchTitle {{
    color: {TEXT_PRIMARY};
    font-size: {font_size_xlarge}px;
    font-weight: 700;
}}
QLineEdit#searchQuery, QComboBox#searchScope, QComboBox#searchRole,
QComboBox#searchState, QListWidget#searchResults {{
    background: {SURFACE_BASE};
    border: {border_width}px solid {BORDER_SUBTLE};
    border-radius: {radius_small}px;
    color: {TEXT_PRIMARY};
    padding: {scale_value(5, scale)}px;
}}
QListWidget#searchResults::item {{
    border-bottom: {border_width}px solid {BORDER_DEFAULT};
    padding: {scale_value(8, scale)}px {scale_value(5, scale)}px;
}}
QListWidget#searchResults::item:selected {{
    background: {ACCENT_BLUE_DARK};
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
    background: {ACCENT_BLUE_DARK};
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
    background: {HEADER_SURFACE};
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
    background: {ACCENT_BLUE};
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
    color: {TEXT_PRIMARY};
    font-size: {font_size_medium}px;
    font-weight: 700;
    letter-spacing: 1px;
}}
QLabel#botsSectionSubtitle {{
    color: {TEXT_SECONDARY};
    font-size: {font_size_small}px;
}}
QLabel#botsFieldLabel {{
    color: {TEXT_SECONDARY};
}}
QLabel#botsReason {{
    color: {TEXT_SECONDARY};
    font-size: {font_size_small}px;
}}
QLabel#botsProvenance {{
    color: {TEXT_SECONDARY};
    font-size: {font_size_small}px;
}}
QLabel#botsStateBadge {{
    color: {TEXT_SECONDARY};
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
    color: {TEXT_PRIMARY};
    padding: {padding_small}px {padding_medium}px;
    selection-background-color: {ACCENT_BLUE_DARK};
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
    color: {TEXT_PRIMARY};
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
    color: {TEXT_SECONDARY};
    font-size: {font_size_small}px;
}}
"""
    
    # Complete the common Qt surface palette. App-owned pop-outs and item
    # views must share the working-panel language, including Qt popup views.
    stylesheet += f"""
QPlainTextEdit#botsConfirmationText {{ background: transparent; border: none; padding: 0; }}
QDialog, QMessageBox, QFileDialog {{ background: {SURFACE_BASE}; }}
QDialog[botsOwnedDialog="true"] {{ border: 1px solid {BORDER_STRONG}; }}
QWidget#workspace, QWidget#leftRail, QWidget#inspectorPanel {{
    background: {SURFACE_PANEL}; border: 1px solid {BORDER_DEFAULT};
}}
QFrame#topBar {{ background: {SURFACE_NAV}; border: 1px solid {BORDER_DEFAULT}; }}
QLabel#brandLabel {{ font-size: {scale_value(18, scale)}px; }}
QLabel#shellContext {{ color: {TEXT_SECONDARY}; }}
QLabel#chatTitle {{ color: {TEXT_PRIMARY}; font-weight: 600; }}
QLabel#emptyTranscript {{ color: {TEXT_SECONDARY}; font-size: {font_size_large}px; padding: {scale_value(12, scale)}px; }}
QListWidget, QTreeView, QTableView, QAbstractItemView {{
    background: {SURFACE_CONTROL}; alternate-background-color: {SURFACE_PANEL};
    color: {TEXT_PRIMARY}; border: 1px solid {BORDER_DEFAULT};
    selection-background-color: {ACCENT_BLUE_DARK}; selection-color: {TEXT_ON_ACCENT}; outline: 0;
}}
QListWidget::item {{ padding: {scale_value(7, scale)}px; }}
QListWidget::item:selected {{ background: {ACCENT_BLUE_DARK}; color: {TEXT_ON_ACCENT}; }}
QHeaderView::section {{ background: {SURFACE_RAISED}; color: {TEXT_SECONDARY};
    border: 0; border-bottom: 1px solid {BORDER_DEFAULT}; padding: {scale_value(7, scale)}px;
}}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {SURFACE_CONTROL}; color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_SUBTLE}; border-radius: 2px;
    padding: {scale_value(5, scale)}px; selection-background-color: {ACCENT_BLUE_DARK};
}}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT_STRUCTURE_DIM};
}}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    color: {TEXT_INACTIVE}; background: {SURFACE_BASE}; border-color: {BORDER_DEFAULT};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url(:/bots/icons/up.svg); width: 10px; height: 6px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url(:/bots/icons/down.svg); width: 10px; height: 6px; }}
QComboBox QAbstractItemView {{ background: {SURFACE_CONTROL}; color: {TEXT_PRIMARY}; }}
QToolButton, QPushButton {{ border-radius: 2px; }}
QPushButton {{ background: {SURFACE_RAISED}; border: 1px solid {BORDER_STRONG}; }}
QPushButton:disabled {{ background: {SURFACE_PANEL}; border-color: {BORDER_DEFAULT}; color: {TEXT_INACTIVE}; }}
QPushButton[role="primary"] {{ background: {ACCENT_BLUE_DARK}; border-color: {ACCENT_STRUCTURE_DIM}; color: {TEXT_ON_ACCENT}; }}
QPushButton[role="destructive"] {{ background: #302226; border-color: #74515a; color: #e8b9be; }}
QScrollBar:vertical {{ background: {SURFACE_BASE}; width: {scale_value(10, scale)}px; margin: 0; }}
QScrollBar:horizontal {{ background: {SURFACE_BASE}; height: {scale_value(10, scale)}px; margin: 0; }}
QScrollBar::handle {{ background: {BORDER_STRONG}; min-height: {scale_value(24, scale)}px; min-width: {scale_value(24, scale)}px; }}
QScrollBar::handle:hover {{ background: {TEXT_INACTIVE}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QMenu, QMenuBar {{ background: {SURFACE_NAV}; color: {TEXT_SECONDARY}; border: 0; }}
QMenu::item {{ padding: {scale_value(7, scale)}px {scale_value(18, scale)}px; }}
QMenu::item:selected, QMenuBar::item:selected {{ background: {ACCENT_BLUE_DARK}; color: {TEXT_PRIMARY}; }}
QMenu::item:disabled, QMenu::item:disabled:selected {{ color: {TEXT_INACTIVE}; background: {SURFACE_NAV}; }}
QMainWindow#botsMainWindow {{ border: 1px solid {BORDER_STRONG}; background: {SURFACE_BASE}; }}
QToolButton[iconOnly="true"], QPushButton[iconOnly="true"] {{ padding: 4px; min-width: 20px; min-height: 20px; }}
QToolButton[iconOnly="true"]:hover, QPushButton[iconOnly="true"]:hover {{ background: {ACCENT_BLUE_DARK}; border-color: {ACCENT_STRUCTURE_DIM}; }}
QToolButton[iconOnly="true"]:focus, QPushButton[iconOnly="true"]:focus {{ border: 1px solid {ACCENT_STRUCTURE}; }}
QToolButton[iconOnly="true"]:pressed, QPushButton[iconOnly="true"]:pressed {{ background: {ACCENT_BLUE_DARK}; }}
QToolButton#modelTuneCog {{ background: transparent; border-color: transparent; }}
QToolButton#modelTuneCog:hover, QToolButton#modelTuneCog:focus {{ background: {ACCENT_BLUE_DARK}; border-color: {ACCENT_STRUCTURE_DIM}; }}
QToolButton#windowClose:hover, QToolButton#dialogClose:hover {{ background: #62333d; border-color: #97515e; }}
QPushButton#stopButton:enabled {{ background: #42272e; border-color: #995562; }}

QMenu::separator {{ height: 1px; background: {BORDER_DEFAULT}; margin: 4px; }}
QStatusBar {{ background: {SURFACE_NAV}; color: {TEXT_SECONDARY}; border-top: 1px solid {BORDER_DEFAULT}; }}
QFrame#messageBubble {{ border-radius: 2px; background: {SURFACE_PANEL}; }}
QFrame#messageBubble[role="user"] {{ background: {SURFACE_RAISED}; border-color: {BORDER_STRONG}; }}
QPlainTextEdit#composer {{ border-radius: 2px; background: {SURFACE_CONTROL}; }}
QFrame#composerFrame {{ border: 1px solid {BORDER_DEFAULT}; }}
QPushButton#sendButton:disabled, QPushButton[role="primary"]:disabled, QPushButton[role="destructive"]:disabled {{
    background: {SURFACE_PANEL}; border-color: {BORDER_DEFAULT}; color: {TEXT_INACTIVE}; font-weight: 400;
}}
QPushButton#stopButton:disabled {{ background: transparent; border-color: transparent; color: {TEXT_INACTIVE}; }}
QDockWidget::title {{ background: {SURFACE_NAV}; padding: {scale_value(9, scale)}px; border: 1px solid {BORDER_DEFAULT}; }}
QLabel#botsDialogTitle {{ color: {TEXT_PRIMARY}; font-size: {font_size_xlarge}px; font-weight: 600; }}
QLabel#botsDialogDescription, QLabel#botsHelp {{ color: {TEXT_SECONDARY}; }}
QFrame#botsDialogFooter {{ background: {SURFACE_NAV}; border-top: 1px solid {BORDER_DEFAULT}; }}
"""

    # Structural console surfaces: only major work boundaries carry frames.
    # The header is hosted above QMainWindow's docks, and transcript rows flow
    # within their shared work well instead of each becoming another panel.
    stylesheet += f"""
QToolBar#consoleToolbar {{ background: {SURFACE_BASE}; border: 0; padding: 0; spacing: 0; }}
QWidget#consoleHeader {{ background: {SURFACE_BASE}; }}
QFrame#topBar {{ border: none; }}
QFrame#consoleIdentity {{ background: {SURFACE_RAISED}; border: 1px solid {BORDER_STRONG}; }}
QFrame#consoleModelContext {{ background: {SURFACE_NAV}; border: 1px solid {BORDER_DEFAULT}; }}
QFrame#consoleReadinessContext {{ background: {SURFACE_NAV}; border: 1px solid {BORDER_DEFAULT}; }}
QLabel#consoleReadiness {{ color: {TEXT_SECONDARY}; }}
QLabel#brandLabel {{ font-size: {scale_value(22, scale)}px; letter-spacing: {scale_value(2, scale)}px; }}
QLabel#consoleCaption {{ color: {TEXT_SECONDARY}; font-size: {font_size_small}px; letter-spacing: 1px; }}
QWidget#consoleNavigation {{ background: {SURFACE_NAV}; border-top: 1px solid {BORDER_DEFAULT}; }}
QWidget#consoleNavigation QToolButton {{ min-height: {scale_value(24, scale)}px; padding: {scale_value(3, scale)}px {scale_value(18, scale)}px; border: 1px solid {BORDER_DEFAULT}; }}
QWidget#consoleNavigation QToolButton:checked {{ background: {ACCENT_BLUE_DARK}; border-color: {ACCENT_STRUCTURE_DIM}; }}
QFrame#leftRail, QFrame#workspace {{ border: none; }}
QFrame#conversationHeader, QFrame#railHeading, QFrame#workPanelHeading {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {HEADER_HIGH}, stop:1 {HEADER_SURFACE});
    border-bottom: 1px solid {BASE_CANVAS};
}}
QFrame#utilityHeading {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {HEADER_HIGH}, stop:1 {HEADER_SURFACE});
    border: 3px solid {BORDER_SUBTLE}; border-top-color: {BORDER_STRONG}; border-left-color: {BORDER_STRONG};
}}
QFrame#utilityHeading QLabel, QLabel#chatTitle, QLabel#workPanelTitle {{ font-weight: 600; color: {TEXT_PRIMARY}; }}
QMenuBar#consoleMenuBar {{ background: transparent; padding: {scale_value(3, scale)}px 0; }}
QMenuBar#consoleMenuBar::item {{ padding: {scale_value(5, scale)}px {scale_value(9, scale)}px; border: 1px solid transparent; }}
QMenuBar#consoleMenuBar::item:selected {{ border-color: {ACCENT_STRUCTURE_DIM}; background: {ACCENT_BLUE_DARK}; }}
QLabel#chatTitle {{ padding: 0; font-size: {font_size_large}px; }}
QScrollArea#transcriptView, QWidget#transcriptContent {{ background: {SURFACE_SUNKEN}; }}
QFrame#messageBubble, QFrame#messageBubble[role="user"] {{ background: transparent; border: none; border-radius: 0; }}
QFrame#messageBubble[generationActive="true"] {{ background: transparent; border-left: 1px solid {ACCENT_STRUCTURE_DIM}; }}
QFrame#messageBubble[searchFocus="true"] {{ background: {ACCENT_BLUE_DARK}; border-left: 2px solid {ACCENT_STRUCTURE}; }}
QLabel#messageRoleLabel {{ font-weight: 600; color: {TEXT_PRIMARY}; }}
QLabel#messageState {{ color: {TEXT_SECONDARY}; }}
QWidget#messageActions QToolButton {{ font-size: {font_size_small}px; padding: {scale_value(2, scale)}px {scale_value(5, scale)}px; }}
QLabel#messageError {{ color: {STATUS_ERROR}; padding: {scale_value(5, scale)}px; border-left: 2px solid {STATUS_ERROR}; background: #241d22; }}
QFrame#composerFrame {{ background: {SURFACE_PANEL}; border: none; border-top: 1px solid {BORDER_STRONG}; }}
QPlainTextEdit#composer {{ background: {SURFACE_CONTROL}; border: none; border-radius: 0; padding: {scale_value(5, scale)}px; }}
QPlainTextEdit#composer:focus {{ border-bottom: 1px solid {ACCENT_STRUCTURE_DIM}; }}
QLabel#composerReadiness {{ color: {TEXT_SECONDARY}; font-size: {font_size_small}px; }}
QPushButton#sendButton, QPushButton#stopButton {{ padding: {scale_value(4, scale)}px {scale_value(12, scale)}px; }}
QWidget#consoleNavigation QToolButton[iconOnly="true"], QPushButton#sendButton[iconOnly="true"], QPushButton#stopButton[iconOnly="true"] {{ padding: 4px; min-width: 20px; min-height: 20px; }}
QWidget#inspectorPanel {{ background: {SURFACE_PANEL}; border: none; }}
QLabel#inspectorSectionTitle {{ font-weight: 600; color: {TEXT_PRIMARY}; border-bottom: 1px solid {BORDER_DEFAULT}; padding-bottom: {scale_value(4, scale)}px; }}
QLabel#workPanelTitle {{ color: {TEXT_PRIMARY}; font-weight: 600; }}
QLabel#generationGroupHeading {{ color: {TEXT_PRIMARY}; font-weight: 600; border-bottom: 1px solid {BORDER_DEFAULT}; padding-top: {scale_value(12, scale)}px; padding-bottom: {scale_value(5, scale)}px; }}
QListWidget#tuneSectionNav {{ background: {SURFACE_NAV}; }}
QListWidget#tuneSectionNav::item:selected {{ border-left: 2px solid {ACCENT_STRUCTURE}; background: {SURFACE_RAISED}; }}
QDockWidget::title {{ background: {SURFACE_NAV}; padding: {scale_value(8, scale)}px {scale_value(10, scale)}px; border: 1px solid {BORDER_DEFAULT}; }}
"""

    return stylesheet


# =============================================================================
# Theme application
# =============================================================================


def apply_draft1_theme(application: QApplication, *, scale: float = 1.0) -> None:
    """Apply the Draft 1 theme to the application.
    
    Apply the current shared desktop visual system and Fusion palette.
    
    Args:
        application: The QApplication instance
        scale: Scaling factor for metrics (1.0 = original size)
    """
    application.setStyle("Fusion")
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, SURFACE_BASE),
        (QPalette.ColorRole.WindowText, TEXT_PRIMARY),
        (QPalette.ColorRole.Base, SURFACE_CONTROL),
        (QPalette.ColorRole.AlternateBase, SURFACE_PANEL),
        (QPalette.ColorRole.Text, TEXT_PRIMARY),
        (QPalette.ColorRole.Button, SURFACE_RAISED),
        (QPalette.ColorRole.ButtonText, TEXT_PRIMARY),
        (QPalette.ColorRole.Highlight, ACCENT_STRUCTURE_DIM),
        (QPalette.ColorRole.HighlightedText, TEXT_ON_ACCENT),
    ):
        palette.setColor(role, QColor(color))
    application.setPalette(palette)
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
