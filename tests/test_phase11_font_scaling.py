"""Tests for Phase 11 M0 font scaling and theme tokenization.

These tests verify:
- Theme token values are correctly defined
- Scalable metrics system works as expected
- DPI rounding policy is consistent
- Theme stylesheet produces expected output
- Campaign dock migrated from inline styles to tokens

Colour assertions follow Mick's explicit colour-refinement authority; metric assertions remain unchanged.
"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from bots5.desktop.theme import (
    FontConfig,
    apply_draft1_theme,
    build_theme_stylesheet,
    round_for_dpi,
    scale_value,
    set_high_dpi_rounding_policy,
)


def test_theme_tokens_follow_colour_authority():
    """Verify the shared palette uses the accepted colour targets."""
    # These follow the explicit colour-refinement targets
    assert build_theme_stylesheet(scale=1.0).startswith("QWidget {")
    # The stylesheet should contain the shared surface, frame and accent colours
    stylesheet = build_theme_stylesheet(scale=1.0)
    assert "#040B12" in stylesheet  # SURFACE_BASE
    assert "#031120" in stylesheet  # SURFACE_PANEL
    assert "#05101B" in stylesheet  # SURFACE_BUBBLE
    assert "#21364C" in stylesheet  # BORDER_DEFAULT
    assert "#0E67A5" in stylesheet  # ACCENT_BLUE


def test_theme_scaling_produces_different_output_at_different_scales():
    """Verify theme scales properly."""
    stylesheet_1_0 = build_theme_stylesheet(scale=1.0)
    stylesheet_1_25 = build_theme_stylesheet(scale=1.25)
    stylesheet_0_75 = build_theme_stylesheet(scale=0.75)
    
    # At scale 1.0, we get original sizes
    assert "font-size: 13px;" in stylesheet_1_0
    assert "font-size: 15px;" in stylesheet_1_0
    assert "padding: 4px" in stylesheet_1_0
    assert "padding: 7px" in stylesheet_1_0
    
    # At scale > 1.0, sizes increase
    assert "font-size: 16px;" in stylesheet_1_25  # 13 * 1.25 = 16.25 -> 16
    assert "padding: 5px" in stylesheet_1_25  # 4 * 1.25 = 5.0
    
    # At scale < 1.0, sizes decrease
    assert "font-size: 10px;" in stylesheet_0_75  # 13 * 0.75 = 9.75 -> 10
    assert "padding: 3px" in stylesheet_0_75  # 4 * 0.75 = 3.0


def test_dpi_rounding_policy():
    """Verify DPI rounding helper produces consistent results."""
    # round_for_dpi uses Python's round() (banker's rounding)
    assert round_for_dpi(1.5) == 2  # Round half to even
    assert round_for_dpi(2.5) == 2  # Round half to even
    assert round_for_dpi(1.4) == 1
    assert round_for_dpi(1.6) == 2
    assert round_for_dpi(9.75) == 10  # 13 * 0.75
    assert round_for_dpi(16.25) == 16  # 13 * 1.25


def test_scale_value_function():
    """Verify scale_value combines scaling with DPI rounding."""
    # 13 * 1.0 = 13.0 -> 13
    assert scale_value(13, 1.0) == 13
    # 13 * 1.25 = 16.25 -> 16
    assert scale_value(13, 1.25) == 16
    # 13 * 0.75 = 9.75 -> 10
    assert scale_value(13, 0.75) == 10
    # 4 * 1.25 = 5.0 -> 5
    assert scale_value(4, 1.25) == 5
    # 4 * 0.75 = 3.0 -> 3
    assert scale_value(4, 0.75) == 3


def test_font_config_defaults():
    """Verify default font configuration matches Draft 1."""
    config = FontConfig.default()
    assert config.ui_family == "System UI"
    assert config.transcript_family == "System UI"
    assert config.code_family == "Monospace"
    assert config.ui_size_pt == 9.0
    assert config.transcript_size_pt == 9.0
    assert config.code_size_pt == 8.5


def test_font_config_customization():
    """Verify font config can be customized."""
    config = FontConfig(
        ui_family="Arial",
        transcript_family="Helvetica",
        code_family="Courier New",
        ui_size_pt=10.0,
        transcript_size_pt=10.0,
        code_size_pt=9.0,
    )
    assert config.ui_family == "Arial"
    assert config.transcript_family == "Helvetica"
    assert config.code_family == "Courier New"
    assert config.ui_size_pt == 10.0
    assert config.transcript_size_pt == 10.0
    assert config.code_size_pt == 9.0


def test_stylesheet_with_custom_fonts():
    """Verify stylesheet includes custom font families.
    
    The stylesheet sets font-family for QWidget (transcript), and specific
    font-size values for different elements. At Draft 1 compatibility,
    font-family is not explicitly set for most elements (they inherit).
    """
    config = FontConfig(
        ui_family="Custom UI",
        transcript_family="Custom Transcript",
        code_family="Custom Code",
    )
    stylesheet = build_theme_stylesheet(font_config=config)
    # The base QWidget gets transcript_family
    assert 'font-family: "Custom Transcript"' in stylesheet


def test_stylesheet_contains_campaign_dock_rules():
    """Verify campaign dock styles are in the stylesheet."""
    stylesheet = build_theme_stylesheet()
    
    # Campaign dock objectName rules
    assert "QDockWidget#campaignDock" in stylesheet
    assert "QWidget#campaignContent" in stylesheet
    
    # Campaign section rules
    assert "QFrame#campaignJobSection" in stylesheet
    assert "QFrame#campaignStagesSection" in stylesheet
    
    # Campaign label rules with objectName
    assert "QLabel#campaignIntegrityWarningsLabel" in stylesheet
    assert "QLabel#campaignStatusLabel" in stylesheet
    assert "QLabel#campaignPricingStatus" in stylesheet
    
    # Campaign widget rules
    assert "QTextEdit#campaignPricingEvidence" in stylesheet
    assert "QTextEdit#campaignPreflightSummary" in stylesheet
    assert "QTextEdit#campaignResultText" in stylesheet
    assert "QTableWidget#campaignStagesTable" in stylesheet
    
    # Campaign button rules
    assert "QPushButton#campaignLoadJobButton" in stylesheet
    assert "QPushButton#campaignValidateButton" in stylesheet
    assert "QPushButton#campaignApproveButton" in stylesheet
    assert "QPushButton#campaignClearButton" in stylesheet


def test_campaign_dock_inline_styles_removed():
    """Verify no inline styles remain in campaign_dock.py."""
    # This is verified by code inspection - campaign_dock.py should
    # no longer have setStyleSheet calls for colors
    import inspect
    from bots5.desktop.campaign_dock import CampaignDockWidget
    
    source = inspect.getsource(CampaignDockWidget)
    # Should not contain inline color styles
    assert 'color: #b00020' not in source
    assert 'color: #3366cc' not in source


def test_high_dpi_rounding_policy_available():
    """Verify high DPI policy setting function exists."""
    # Just verify the function exists and can be called
    # (actual policy setting requires QApplication)
    assert callable(set_high_dpi_rounding_policy)


def test_apply_draft1_theme_function():
    """Verify theme application function works."""
    app = QApplication.instance() or QApplication([])
    
    # Should not raise
    apply_draft1_theme(app, scale=1.0)
    apply_draft1_theme(app, scale=1.25)
    apply_draft1_theme(app, scale=0.75)


def test_stylesheet_structure_preserved_at_scale_1():
    """Verify the established selectors remain present at scale 1.0."""
    stylesheet = build_theme_stylesheet(scale=1.0)
    
    # Check key properties from Draft 1 are present
    assert "QWidget {" in stylesheet
    assert "QMainWindow, QWidget#draft1Root {" in stylesheet
    assert "QFrame#topBar, QFrame#composerFrame, QWidget#leftRail, QDockWidget > QWidget {" in stylesheet
    assert "QLabel#modelPill {" in stylesheet
    assert "QLabel#chatTitle {" in stylesheet
    assert "QFrame#messageBubble {" in stylesheet
    assert "QPlainTextEdit#composer {" in stylesheet
    assert "QPushButton#sendButton {" in stylesheet
    
    # Check the current colour authority is applied
    assert "#040B12" in stylesheet  # base
    assert "#031120" in stylesheet  # panel
    assert "#05101B" in stylesheet  # bubble
    assert "#21364C" in stylesheet  # border
    assert "#0E67A5" in stylesheet  # accent
