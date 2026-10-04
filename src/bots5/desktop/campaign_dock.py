"""CampaignDockWidget: dockable operator surface for the campaign engine (Phase 10 M2.0a).

The dock presents a sealed projection via :class:`CampaignViewModel` built from
the immutable :class:`~bots5.core.campaign.CampaignProjection`. Every state read
comes from the bridge projection; every operation goes through the bridge.
No run files, no provider construction, no engine logic duplication.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QDialog,
    QDialogButtonBox,
    QSizePolicy,
    QBoxLayout,
)


from bots5.core.campaign import (
    CampaignBridge,
    CampaignProjection,
    POLL_INTERVAL_MS,
    POLL_MAX_INTERVAL_MS,
)

from .dialog_primitives import (
    ChamferedPanel, PanelPair, WorkPanel, normalize_dialog, scrollable, SectionHeader,
)
from .theme import (
    PANEL_INSET,
    ROW_GAP,
    SURFACE_PANEL,
    SURFACE_RAISED,
    STATUS_SUCCESS,
    STATUS_WARNING,
    STATUS_ERROR,
    ACCENT_STRUCTURE,
    CAMPAIGN_DOCK_PRICING_HEIGHT,
    CAMPAIGN_DOCK_PREFLIGHT_HEIGHT,
    CAMPAIGN_DOCK_RESULT_HEIGHT,
)


class CampaignViewModel:
    """Immutable view model derived from a CampaignProjection.

    Zero business logic: only field projection and derived presentation text.
    All state comes from the projection; nothing is fabricated.
    """

    def __init__(self, projection: CampaignProjection | None = None) -> None:
        self._projection = projection
        self._job_summary: dict[str, object] | None = None
        self._preflight_summary: str | None = None
        self._provider_routes: dict[str, dict[str, object]] | None = None

    @property
    def projection(self) -> CampaignProjection | None:
        return self._projection

    @property
    def job_summary(self) -> dict[str, object] | None:
        return self._job_summary

    @property
    def preflight_summary(self) -> str | None:
        return self._preflight_summary

    @property
    def provider_routes(self) -> dict[str, dict[str, object]] | None:
        return self._provider_routes

    def apply_projection(self, projection: CampaignProjection) -> "CampaignViewModel":
        return CampaignProjectionWrapper(projection, self._job_summary, self._preflight_summary, self._provider_routes)

    def apply_job_summary(self, summary: dict[str, object]) -> "CampaignViewModel":
        return CampaignProjectionWrapper(self._projection, summary, self._preflight_summary, self._provider_routes)

    def apply_preflight_summary(self, summary: str) -> "CampaignViewModel":
        return CampaignProjectionWrapper(self._projection, self._job_summary, summary, self._provider_routes)

    def apply_provider_routes(self, routes: dict[str, dict[str, object]]) -> "CampaignViewModel":
        return CampaignProjectionWrapper(self._projection, self._job_summary, self._preflight_summary, routes)


class CampaignProjectionWrapper(CampaignViewModel):
    """Wrapper that forwards projection access while adding derived fields."""

    def __init__(
        self,
        projection: CampaignProjection | None,
        job_summary: dict[str, object] | None,
        preflight_summary: str | None,
        provider_routes: dict[str, dict[str, object]] | None,
    ) -> None:
        super().__init__(projection)
        self._projection = projection
        self._job_summary = job_summary
        self._preflight_summary = preflight_summary
        self._provider_routes = provider_routes

    @property
    def has_data(self) -> bool:
        return self._projection is not None or self._job_summary is not None

    @property
    def job_identity(self) -> str | None:
        if self._job_summary is None:
            return None
        parts = []
        if "job_name" in self._job_summary:
            parts.append(f"Name: {self._job_summary['job_name']}")
        if "schema_version" in self._job_summary:
            parts.append(f"Version: {self._job_summary['schema_version']}")
        if "runs_dir" in self._job_summary:
            parts.append(f"Runs dir: {self._job_summary['runs_dir']}")
        if "workers" in self._job_summary:
            workers = self._job_summary["workers"]
            if isinstance(workers, list):
                parts.append(f"Workers: {len(workers)}")
        if "synthesis" in self._job_summary and self._job_summary.get("synthesis") is not None:
            parts.append("Synthesis: yes")
        return " | ".join(parts) if parts else None

    @property
    def run_identity(self) -> str | None:
        if self._projection is None:
            return None
        parts = []
        if self._projection.run_id:
            parts.append(f"Run: {self._projection.run_id}")
        if self._projection.run_dir:
            parts.append(f"Dir: {self._projection.run_dir}")
        if self._projection.run_state:
            parts.append(f"State: {self._projection.run_state}")
        return " | ".join(parts) if parts else None

    @property
    def display_state(self) -> str:
        if self._projection is None:
            return "idle"
        return self._projection.display_state or "idle"

    @property
    def stage_rows(self) -> list[dict[str, object]]:
        """Per-stage row data for the stages table."""
        if self._projection is None or not self._projection.stages:
            return []
        rows = []
        for stage in self._projection.stages:
            rows.append({
                "stage_id": stage.stage_id,
                "state": stage.state or "queued",
                "attempt_number": stage.attempt_number,
                "available_attempts": stage.available_attempts,
                "duration_seconds": stage.duration_seconds,
                "total_tokens": stage.total_tokens,
                "cost_usd": stage.cost_usd,
                "cost_known": stage.cost_known,
                "provider_side_outcome_unknown": stage.provider_side_outcome_unknown,
                "output_path": stage.output_path,
                "requested_model": stage.requested_model,
                "error_type": stage.error_type,
                "completion_complete": stage.completion_complete,
            })
        return rows

    @property
    def live_cost_line(self) -> str:
        """Live cost line: known subtotal plus explicit unknown set."""
        if self._projection is None:
            return "cost: unknown (no run active)"
        live = self._projection.live_cost or {}
        known = live.get("known_subtotal_usd")
        unknown_ids = live.get("unknown_stage_ids", [])
        status = live.get("status", "unknown")
        if known is not None:
            known_str = str(known)
        else:
            known_str = "?"

        if unknown_ids:
            unknown_text = f" + {len(unknown_ids)} unknown ({', '.join(str(s) for s in unknown_ids)})"
        else:
            unknown_text = ""

        if status == "complete":
            return f"cost: {known_str}{unknown_text} (complete)"
        if status == "partial":
            return f"cost: {known_str}{unknown_text} (partial)"
        if status == "unknown":
            return f"cost: unknown (no known costs)"
        return f"cost: {known_str}{unknown_text}"

    @property
    def synthesis_freshness(self) -> str | None:
        if self._projection is None:
            return None
        return self._projection.synthesis_freshness

    @property
    def integrity_warnings(self) -> tuple[str, ...]:
        if self._projection is None:
            return ()
        return self._projection.integrity_warnings

    @property
    def is_running(self) -> bool:
        if self._projection is None:
            return False
        return self._projection.is_running

    @property
    def provider_side_outcome_unknown_stage_ids(self) -> tuple[str, ...]:
        if self._projection is None:
            return ()
        return self._projection.provider_side_outcome_unknown_stage_ids


class CampaignDockWidget(QDockWidget):
    """Dockable campaign operator surface.

    Constructor: ``CampaignDockWidget(parent=None, *, bridge_factory)``
    where ``bridge_factory`` is a callable ``(runs_dir) -> CampaignBridge``.

    Presentation rules (§4):
      - Load a campaign job from an explicit path, then Validate.
      - Show job identity (name, schema version, runs dir, worker and synthesis counts).
      - Preflight summary with Approve as explicit action.
      - Live progress: per-stage state, attempt, duration, tokens, known cost.
      - Live cost line: KNOWN subtotal plus explicit unknown set.
      - Result inspection: per-stage output and synthesis, expandable.
      - Attempts: list per stage, mark selected, offer explicit "Make current".
      - Freshness: display synthesis freshness verbatim.
      - Integrity warnings: display every warning verbatim.
      - New Job: clears working context only.
      - Cancel: through the bridge.
      - Regenerate: pick stage, enter replacement model, prepare, then approve.
      - Rerun synthesis: explicit prepare then approve.
      - Polling: QTimer at 250 ms cadence awaiting projection_async.
      - Uncertainty: render provider-side-outcome-unknown distinctly.
    """

    refresh_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        bridge_factory: Callable[[Path], CampaignBridge],
    ) -> None:
        super().__init__("Campaign", parent)
        self.setObjectName("campaignDock")
        self._bridge_factory = bridge_factory
        self._bridge: CampaignBridge | None = None
        self._view_model = CampaignViewModel()
        self._closed = False
        self._projection: CampaignProjection | None = None
        self._prepared_operation = None
        self._runs_dir: Path | None = None

        content = QWidget(self)
        content.setObjectName("campaignContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(PANEL_INSET, PANEL_INSET, PANEL_INSET, PANEL_INSET)
        layout.setSpacing(ROW_GAP)

        # Operational input and consent are separate work panels. Existing
        # controls are reparented without touching the bridge command paths.
        job_section = self._build_job_section(content)
        job_items = [job_section.takeAt(0) for _ in range(job_section.count())]
        job = WorkPanel("Job / pricing evidence", content)
        preflight = WorkPanel("Preflight / approval", content)
        for item in job_items[:4]:
            job.body_layout.addWidget(item.widget())
        preflight.body_layout.addWidget(job_items[4].widget())
        preflight.body_layout.addWidget(job_items[5].widget())
        approval_controls = job_items[6].layout()
        approval_controls.setParent(None)
        preflight.body_layout.addLayout(approval_controls)
        layout.addWidget(PanelPair(job, preflight, content, master_max_width=560,
                                   master_weight=1, detail_weight=1,
                                   narrow_master_height=16_777_215))

        run = WorkPanel("Run / stage outcomes", content)
        run_section = self._build_run_section(content)
        run.body_layout.addLayout(run_section)

        # Stages table
        stages_section = self._build_stages_section(content)
        run.body_layout.addLayout(stages_section)

        # Cost line
        self._cost_label = QLabel("cost: unknown (no run active)", content)
        self._cost_label.setObjectName("campaignCostLabel")
        self._cost_label.setWordWrap(True)
        run.body_layout.addWidget(self._cost_label)

        # Freshness and warnings
        freshness_section = self._build_freshness_section(content)
        run.body_layout.addLayout(freshness_section)
        layout.addWidget(run)

        # Result inspection (expandable)
        result = WorkPanel("Selected stage / result inspection", content)
        result_section = self._build_result_section(content)
        result.body_layout.addLayout(result_section)
        layout.addWidget(result)

        # Status bar
        status_section = self._build_status_section(content)
        layout.addLayout(status_section)

        # Bottom controls
        controls_section = self._build_controls_section(content)
        shell = QWidget(self)
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(8)
        masthead = ChamferedPanel(shell, chamfer=8, header=True)
        head_layout = QVBoxLayout(masthead)
        head_layout.setContentsMargins(12, 8, 12, 8)
        head_layout.addWidget(SectionHeader("Campaign", "Load a job, validate its routes, then approve the prepared operation.", masthead))
        shell_layout.addWidget(masthead)
        shell_layout.addWidget(scrollable(content, shell), 1)
        footer = QFrame(shell)
        footer.setObjectName("botsDialogFooter")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(PANEL_INSET, ROW_GAP, PANEL_INSET, ROW_GAP)
        footer_layout.addLayout(controls_section)
        shell_layout.addWidget(footer)
        for button in shell.findChildren(QPushButton):
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._fit_action_layouts()
        self.setWidget(shell)
        self.resize(1100, 540)

        # Polling timer
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._on_poll_timeout)

        self.visibilityChanged.connect(self._on_visibility_changed)
        self._sync_polling()

    # ------------------------------------------------------------------
    # UI construction helpers
    # ------------------------------------------------------------------

    def _build_job_section(self, parent: QWidget) -> QVBoxLayout:
        section = QVBoxLayout()
        section.setObjectName("campaignJobSection")

        self._job_identity_label = QLabel("No job loaded", parent)
        self._job_identity_label.setObjectName("campaignJobIdentity")
        self._job_identity_label.setWordWrap(True)
        section.addWidget(self._job_identity_label)

        self._pricing_input = QTextEdit(parent)
        self._pricing_input.setObjectName("campaignPricingEvidence")
        self._pricing_input.setPlaceholderText(
            'Paid routes: JSON {"entries":[{"provider":"openrouter",'
            '"input_usd_per_1m":"...","output_usd_per_1m":"...",'
            '"rate_source":"...","observed_at":"..."}]}'
        )
        self._pricing_input.setFixedHeight(CAMPAIGN_DOCK_PRICING_HEIGHT)
        self._pricing_input.setToolTip(
            "Operator-supplied currently advertised rates, cited source, and observation time per paid route"
        )
        self._pricing_input.textChanged.connect(self._on_pricing_changed)
        pricing_label = QLabel("Pricing evidence for paid routes (JSON)", parent)
        pricing_label.setObjectName("botsFieldLabel")
        section.addWidget(pricing_label)
        section.addWidget(self._pricing_input)
        self._pricing_status = QLabel("Pricing evidence required for paid routes", parent)
        self._pricing_status.setObjectName("campaignPricingStatus")
        self._pricing_status.setWordWrap(True)
        section.addWidget(self._pricing_status)

        self._preflight_text = QTextEdit(parent)
        self._preflight_text.setObjectName("campaignPreflightSummary")
        self._preflight_text.setReadOnly(True)
        self._preflight_text.setFixedHeight(100)
        section.addWidget(QLabel("Preflight summary", parent))
        section.addWidget(self._preflight_text)

        job_controls = QHBoxLayout()
        self._job_controls_layout = job_controls
        job_controls.setObjectName("campaignJobControls")

        self._load_job_button = QPushButton("Load Job", parent)
        self._load_job_button.setObjectName("campaignLoadJobButton")
        self._load_job_button.setToolTip("Load a campaign job from an explicit path")
        self._load_job_button.clicked.connect(self._on_load_job)
        job_controls.addWidget(self._load_job_button)

        self._validate_button = QPushButton("Validate", parent)
        self._validate_button.setObjectName("campaignValidateButton")
        self._validate_button.setToolTip("Validate the loaded job (zero spend)")
        self._validate_button.clicked.connect(self._on_validate)
        self._validate_button.setEnabled(False)
        job_controls.addWidget(self._validate_button)

        self._approve_button = QPushButton("Approve", parent)
        self._approve_button.setObjectName("campaignApproveButton")
        self._approve_button.setToolTip("Approve and start the prepared operation")
        self._approve_button.clicked.connect(self._on_approve)
        self._approve_button.setEnabled(False)
        job_controls.addWidget(self._approve_button)

        self._clear_button = QPushButton("New Job", parent)
        self._clear_button.setObjectName("campaignClearButton")
        self._clear_button.setToolTip("Clear the working context (no disk changes)")
        self._clear_button.clicked.connect(self._on_clear_job)
        self._clear_button.setEnabled(False)
        job_controls.addWidget(self._clear_button)

        section.addLayout(job_controls)
        return section

    def _build_run_section(self, parent: QWidget) -> QVBoxLayout:
        section = QVBoxLayout()
        section.setObjectName("campaignRunSection")

        self._run_identity_label = QLabel("No run active", parent)
        self._run_identity_label.setObjectName("campaignRunIdentity")
        self._run_identity_label.setWordWrap(True)
        section.addWidget(self._run_identity_label)

        return section

    def _build_stages_section(self, parent: QWidget) -> QVBoxLayout:
        section = QVBoxLayout()
        section.setObjectName("campaignStagesSection")

        self._stages_table = QTableWidget(parent)
        self._stages_table.setObjectName("campaignStagesTable")
        self._stages_table.setColumnCount(8)
        self._stages_table.setHorizontalHeaderLabels((
            "Stage", "State", "Attempt", "Model", "Duration", "Tokens", "Cost", "Outcome"
        ))
        self._stages_table.verticalHeader().setVisible(False)
        self._stages_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._stages_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._stages_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._stages_table.horizontalHeader()
        self._stages_table.setMinimumHeight(150)
        self._stages_table.setMaximumHeight(240)
        self._stages_table.setAlternatingRowColors(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 140)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        self._stages_table.itemSelectionChanged.connect(self._sync_stage_controls)
        section.addWidget(self._stages_table, 1)

        stage_controls = QHBoxLayout()
        self._stage_controls_layout = stage_controls
        stage_controls.setObjectName("campaignStageControls")

        self._make_current_button = QPushButton("Make current", parent)
        self._make_current_button.setObjectName("campaignMakeCurrentButton")
        self._make_current_button.setToolTip("Select this attempt as the current result")
        self._make_current_button.clicked.connect(self._on_make_current)
        self._make_current_button.setEnabled(False)
        stage_controls.addWidget(self._make_current_button)

        self._regenerate_button = QPushButton("Regenerate", parent)
        self._regenerate_button.setObjectName("campaignRegenerateButton")
        self._regenerate_button.setToolTip("Regenerate this stage with a different model")
        self._regenerate_button.clicked.connect(self._on_regenerate)
        self._regenerate_button.setEnabled(False)
        stage_controls.addWidget(self._regenerate_button)

        self._rerun_synthesis_button = QPushButton("Rerun synthesis", parent)
        self._rerun_synthesis_button.setObjectName("campaignRerunSynthesisButton")
        self._rerun_synthesis_button.setToolTip("Rerun synthesis with the same inputs")
        self._rerun_synthesis_button.clicked.connect(self._on_rerun_synthesis)
        self._rerun_synthesis_button.setEnabled(False)
        stage_controls.addWidget(self._rerun_synthesis_button)

        section.addLayout(stage_controls)
        return section

    def _build_freshness_section(self, parent: QWidget) -> QVBoxLayout:
        section = QVBoxLayout()
        section.setObjectName("campaignFreshnessSection")

        freshness_layout = QHBoxLayout()
        freshness_layout.setObjectName("campaignFreshnessLayout")

        self._freshness_label = QLabel("Synthesis freshness: N/A", parent)
        self._freshness_label.setObjectName("campaignFreshnessLabel")
        self._freshness_label.setWordWrap(True)
        freshness_layout.addWidget(self._freshness_label)

        section.addLayout(freshness_layout)

        self._integrity_warnings_label = QLabel("", parent)
        self._integrity_warnings_label.setObjectName("campaignIntegrityWarningsLabel")
        self._integrity_warnings_label.setWordWrap(True)
        section.addWidget(self._integrity_warnings_label)

        return section

    def _fit_action_layouts(self) -> None:
        direction = QBoxLayout.Direction.TopToBottom if self.width() < 500 else QBoxLayout.Direction.LeftToRight
        for layout in (self._job_controls_layout, self._stage_controls_layout):
            layout.setDirection(direction)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_stage_controls_layout"):
            self._fit_action_layouts()

    def _build_result_section(self, parent: QWidget) -> QVBoxLayout:
        section = QVBoxLayout()
        section.setObjectName("campaignResultSection")

        result_label = QLabel("Result", parent)
        result_label.setObjectName("campaignResultLabel")
        section.addWidget(result_label)

        self._result_text = QTextEdit(parent)
        self._result_text.setObjectName("campaignResultText")
        self._result_text.setReadOnly(True)
        self._result_text.setMaximumHeight(CAMPAIGN_DOCK_RESULT_HEIGHT)
        section.addWidget(self._result_text, 1)

        return section

    def _build_status_section(self, parent: QWidget) -> QVBoxLayout:
        section = QVBoxLayout()
        section.setObjectName("campaignStatusSection")

        self._status_label = QLabel("", parent)
        self._status_label.setObjectName("campaignStatusLabel")
        self._status_label.setWordWrap(True)
        section.addWidget(self._status_label)

        return section

    def _build_controls_section(self, parent: QWidget) -> QHBoxLayout:
        section = QHBoxLayout()
        section.addStretch(1)
        section.setObjectName("campaignControlsSection")

        self._cancel_button = QPushButton("Cancel", parent)
        self._cancel_button.setObjectName("campaignCancelButton")
        self._cancel_button.setToolTip("Cancel the active run (if running)")
        self._cancel_button.clicked.connect(self._on_cancel)
        self._cancel_button.setEnabled(False)
        section.addWidget(self._cancel_button)

        return section

    # ------------------------------------------------------------------
    # Job loading and validation
    # ------------------------------------------------------------------

    def _on_load_job(self) -> None:
        """Load a campaign job from an explicit path."""
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Campaign Job",
            "",
            "Job files (*.yaml *.yml *.json);;All files (*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not path:
            return

        job_path = Path(path)
        
        # Close any existing bridge first
        if self._bridge is not None:
            try:
                asyncio.get_event_loop().run_until_complete(self._bridge.close())
            except Exception:
                pass
            self._bridge = None

        try:
            # Load job first to get runs_dir
            job_path_resolved = job_path.resolve(strict=False)
            summary = self._bridge_factory(job_path_resolved.parent / ".bots5" / "runs")
            self._bridge = summary
            summary = self._bridge.load_job(job_path)
            self._view_model = self._view_model.apply_job_summary(summary)
            self._render_job_summary(summary)
            self._validate_button.setEnabled(True)
            self._clear_button.setEnabled(True)
            self._show_status(f"Job loaded: {job_path}")
        except Exception as exc:
            self._show_status(f"Load failed: {exc}")

    def _on_validate(self) -> None:
        """Validate the loaded job (zero spend)."""
        if self._bridge is None:
            self._show_status("Bridge not available")
            return

        try:
            summary = self._bridge.validate()
            self._view_model = self._view_model.apply_job_summary(summary)
            self._render_job_summary(summary)
            self._show_status("Validation successful (zero spend)")
        except Exception as exc:
            self._show_status(f"Validation failed: {exc}")

    def _pricing_document(self) -> dict | None:
        text = self._pricing_input.toPlainText().strip()
        if not text:
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"pricing JSON is invalid: {exc}") from None
        if not isinstance(value, dict):
            raise ValueError("pricing evidence must be a JSON object")
        return value

    def _on_pricing_changed(self) -> None:
        if self._prepared_operation is not None:
            self._prepared_operation = None
            self._approve_button.setEnabled(False)
            self._pricing_status.setText("Pricing changed — Validate to rebuild the approval summary")

    def _render_job_summary(self, summary: dict[str, object]) -> None:
        """Render the job identity and prepare preflight."""
        parts = []
        if "job_name" in summary:
            parts.append(f"Name: {summary['job_name']}")
        if "schema_version" in summary:
            parts.append(f"Version: {summary['schema_version']}")
        if "runs_dir" in summary:
            runs_dir = summary["runs_dir"]
            if isinstance(runs_dir, str):
                self._runs_dir = Path(runs_dir)
                parts.append(f"Runs dir: {runs_dir}")
        if "workers" in summary:
            workers = summary["workers"]
            if isinstance(workers, list):
                parts.append(f"Workers: {len(workers)}")
        if "synthesis" in summary and summary.get("synthesis") is not None:
            parts.append("Synthesis: yes")
        if "execution" in summary:
            exec_info = summary["execution"]
            if isinstance(exec_info, dict):
                max_p = exec_info.get("max_parallelism")
                if max_p is not None:
                    parts.append(f"Parallel: {max_p}")

        self._job_identity_label.setText(" | ".join(parts) if parts else "No job identity")

        # Prepare preflight snapshot
        if self._bridge is not None and self._runs_dir is not None:
            try:
                prepared = self._bridge.prepare_full_run(
                    "desktop-user", pricing_evidence=self._pricing_document()
                )
                self._prepared_operation = prepared
                self._provider_routes = prepared.provider_routes
                self._view_model = self._view_model.apply_provider_routes(prepared.provider_routes)
                self._preflight_summary = prepared.summary
                self._render_preflight_summary(prepared.summary)
                paid = any(route.get("kind") != "local_openai" for route in prepared.provider_routes.values())
                can_approve = not paid or prepared.approval.pricing_evidence is not None
                self._approve_button.setEnabled(can_approve)
                self._pricing_status.setText(
                    "Pricing evidence complete — accepting the recorded bound approves spend"
                    if paid and can_approve else
                    "Paid route: complete pricing evidence is required before Approve"
                    if paid else "Local-only route: pricing evidence is not required"
                )
                self._show_status("Preflight ready — Approve to start" if can_approve else "Preflight ready — pricing required")
            except Exception as exc:
                self._show_status(f"Preflight failed: {exc}")

    def _render_preflight_summary(self, summary: str) -> None:
        """Render preflight summary (models, provider route, attempt, limits)."""
        self._preflight_text.setPlainText(summary)

    def _on_approve(self) -> None:
        """Approve and dispatch the prepared operation.

        The prepared operation decides which one-shot approval consumer runs:
        ``approve_and_start`` for a full run, ``approve_and_regenerate`` for a
        worker regeneration, ``approve_and_rerun_synthesis`` for a synthesis
        rerun. Dispatch is by operation, not by a single entry point: the
        bridge refuses a full-run target for a regeneration operation, and it
        is right to -- the desktop was calling the wrong consumer, which made
        obligations 9 and 11 unreachable from the surface.
        """
        if self._bridge is None or self._prepared_operation is None:
            self._show_status("No prepared operation")
            return

        operation = getattr(self._prepared_operation, "operation", None)
        dispatch = {
            "full_run": self._bridge.approve_and_start,
            "worker_regeneration": self._bridge.approve_and_regenerate,
            "synthesis_rerun": self._bridge.approve_and_rerun_synthesis,
        }.get(operation)
        if dispatch is None:
            self._show_status(f"Approval failed: unsupported operation {operation!r}")
            return

        started_message = {
            "full_run": "Run started",
            "worker_regeneration": "Regeneration started",
            "synthesis_rerun": "Synthesis rerun started",
        }[operation]
        try:
            dispatch(self._prepared_operation)
            self._show_status(started_message)
            # Update view to show running state
            asyncio.create_task(self._refresh_projection_async())
        except Exception as exc:
            self._show_status(f"Approval failed: {exc}")

    def _on_clear_job(self) -> None:
        """Clear the working context (no disk changes)."""
        # Close existing bridge if any
        if self._bridge is not None:
            try:
                asyncio.get_event_loop().run_until_complete(self._bridge.close())
            except Exception:
                pass
            self._bridge = None
        self._runs_dir = None
        self._prepared_operation = None
        self._provider_routes = None
        self._view_model = CampaignViewModel()
        self._job_identity_label.setText("No job loaded")
        self._pricing_input.clear()
        self._pricing_status.setText("Pricing evidence required for paid routes")
        self._preflight_text.clear()
        self._run_identity_label.setText("No run active")
        self._stages_table.setRowCount(0)
        self._cost_label.setText("cost: unknown (no run active)")
        self._freshness_label.setText("Synthesis freshness: N/A")
        self._integrity_warnings_label.setText("")
        self._result_text.setPlainText("")
        self._status_label.setText("")
        self._validate_button.setEnabled(False)
        self._approve_button.setEnabled(False)
        self._clear_button.setEnabled(False)
        self._cancel_button.setEnabled(False)
        self._make_current_button.setEnabled(False)
        self._regenerate_button.setEnabled(False)
        self._rerun_synthesis_button.setEnabled(False)

    # ------------------------------------------------------------------
    # Run identity and polling
    # ------------------------------------------------------------------

    def _on_validate_finished(self) -> None:
        """Called after validate finishes to show results."""
        pass

    def _build_poll_timer(self) -> QTimer:
        timer = QTimer(self)
        timer.setInterval(POLL_INTERVAL_MS)
        timer.timeout.connect(self._on_poll_timeout)
        return timer

    def _on_poll_timeout(self) -> None:
        """Poll timeout handler: await projection_async."""
        asyncio.create_task(self._refresh_projection_async())

    async def _refresh_projection_async(self) -> None:
        """Refresh the projection from the bridge."""
        if self._closed or self._bridge is None:
            return

        try:
            projection = await self._bridge.projection_async()
            self._projection = projection
            self._view_model = self._view_model.apply_projection(projection)
            self._render_projection(projection)
        except Exception as exc:
            self._show_status(f"Projection refresh failed: {exc}")

    def _render_projection(self, projection: CampaignProjection) -> None:
        """Render the projection into the UI."""
        # Run identity
        parts = []
        if projection.run_id:
            parts.append(f"Run: {projection.run_id}")
        if projection.run_dir:
            parts.append(f"Dir: {projection.run_dir}")
        display_state = projection.display_state or "unknown"
        parts.append(f"State: {display_state}")
        self._run_identity_label.setText(" | ".join(parts) if parts else "No run active")

        # Stages table with attempt switcher support
        rows = self._view_model.stage_rows
        self._stages_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            self._stages_table.setItem(i, 0, QTableWidgetItem(row["stage_id"]))
            state_item = QTableWidgetItem(row["state"])
            # Color-code states
            state_color = self._state_color(row["state"], row.get("provider_side_outcome_unknown", False))
            state_item.setBackground(state_color)
            self._stages_table.setItem(i, 1, state_item)
            
            # Attempt cell with dropdown for attempt switcher
            attempt = row.get("attempt_number", 0)
            available_attempts = row.get("available_attempts", [attempt])
            
            # Create a combo box for attempt selection if multiple attempts available
            if len(available_attempts) > 1:
                from PySide6.QtWidgets import QComboBox
                attempt_combo = QComboBox()
                for att_num in sorted(available_attempts):
                    attempt_combo.addItem(str(att_num), att_num)
                attempt_combo.setCurrentText(str(attempt))
                attempt_combo.currentIndexChanged.connect(
                    lambda idx, row_idx=i: self._on_attempt_changed(row_idx, idx)
                )
                self._stages_table.setCellWidget(i, 2, attempt_combo)
            else:
                self._stages_table.setItem(i, 2, QTableWidgetItem(str(attempt)))
            
            self._stages_table.setItem(i, 3, QTableWidgetItem(row.get("requested_model") or ""))
            duration = row.get("duration_seconds")
            duration_str = f"{duration:.1f}s" if duration is not None else "—"
            self._stages_table.setItem(i, 4, QTableWidgetItem(duration_str))
            tokens = row.get("total_tokens")
            tokens_str = str(tokens) if tokens is not None else "—"
            self._stages_table.setItem(i, 5, QTableWidgetItem(tokens_str))
            cost = row.get("cost_usd")
            cost_str = cost if cost is not None else "—"
            self._stages_table.setItem(i, 6, QTableWidgetItem(cost_str))
            # Outcome column
            outcome_parts = []
            error_type = row.get("error_type")
            if error_type:
                outcome_parts.append(f"Error: {error_type}")
            completion = row.get("completion_complete")
            if completion is True:
                outcome_parts.append("Complete")
            elif completion is False:
                outcome_parts.append("Incomplete")
            if row.get("provider_side_outcome_unknown"):
                outcome_parts.append("Unknown outcome")
            self._stages_table.setItem(i, 7, QTableWidgetItem(", ".join(outcome_parts) if outcome_parts else "—"))

        # Live cost line
        self._cost_label.setText(self._view_model.live_cost_line)

        # Freshness
        freshness = self._view_model.synthesis_freshness
        if freshness:
            self._freshness_label.setText(f"Synthesis freshness: {freshness}")
        else:
            self._freshness_label.setText("Synthesis freshness: N/A")

        # Integrity warnings
        warnings = self._view_model.integrity_warnings
        if warnings:
            self._integrity_warnings_label.setText("Integrity warnings: " + "; ".join(warnings))
        else:
            self._integrity_warnings_label.setText("")

        # Cancel button state
        self._cancel_button.setEnabled(projection.is_running)

        # Sync stage controls
        self._sync_stage_controls()

    def _on_attempt_changed(self, row_idx: int, combo_idx: int) -> None:
        """Handle attempt combo box change in the projection table."""
        # This will be called when user selects a different attempt
        # The actual switching is handled by _on_make_current
        pass

    def _state_color(self, state: str, provider_unknown: bool) -> QColor:
        """Retain state tones with readable light text on graphite."""
        base = QColor(SURFACE_PANEL)
        tone = {
            "succeeded": STATUS_SUCCESS,
            "failed": STATUS_WARNING if provider_unknown else STATUS_ERROR,
            "running": ACCENT_STRUCTURE,
            "queued": SURFACE_RAISED,
            "cancelled": SURFACE_RAISED,
        }.get(state, SURFACE_RAISED)
        accent = QColor(tone)
        return QColor(round(base.red() * .8 + accent.red() * .2),
                      round(base.green() * .8 + accent.green() * .2),
                      round(base.blue() * .8 + accent.blue() * .2))

    # ------------------------------------------------------------------
    # Stage controls
    # ------------------------------------------------------------------

    def _sync_stage_controls(self) -> None:
        """Sync stage controls based on selection.

        Regeneration targets a stage and is offered whenever one is selected and
        is not currently running. The engine's own preconditions for
        ``prepare_regeneration`` are evidence version, stage membership, job
        match, approval scope, route identity, byte match and one-shot -- stage
        state is not among them, and a *succeeded* stage is the normal
        regeneration target ("try a different model for this worker"), so
        disabling it there made the control dead on every row of a successful
        run.

        Rerun synthesis is a run-level control, not a per-stage one: it is
        motivated by the staleness indicator rather than by which worker row is
        selected, so it is offered whenever a run is loaded.
        """
        selected = self._selected_stage()
        if selected is None:
            self._make_current_button.setEnabled(False)
            self._regenerate_button.setEnabled(False)
        else:
            self._make_current_button.setEnabled(True)
            self._regenerate_button.setEnabled(selected["state"] != "running")
            # Expandable persisted output for the selected attempt
            # (DESKTOP_SURFACE_AND_LIFECYCLE.md section 3).
            self._render_selected_stage_output()

        self._rerun_synthesis_button.setEnabled(
            self._bridge is not None and self._stages_table.rowCount() > 0
        )

    def _render_selected_stage_output(self) -> None:
        """Render the selected stage attempt's durable output.

        The read goes through the bridge, which uses the engine's own
        ``storage.load_stage_view`` reader; this widget never parses run files
        itself. A read failure is surfaced, never guessed at.
        """
        if self._bridge is None:
            return
        selected = self._selected_stage()
        if selected is None:
            return
        stage_id = selected["stage_id"]
        attempt = selected.get("attempt_number")
        try:
            text = self._bridge.read_stage_output(stage_id, attempt)
        except Exception as exc:
            self._show_status(f"Output unavailable for {stage_id}: {exc}")
            return
        if text is not None:
            self._result_text.setPlainText(text)

    def _selected_stage(self) -> dict[str, object] | None:
        """Get the currently selected stage row data."""
        indexes = self._stages_table.selectionModel().selectedRows() if self._stages_table.selectionModel() else []
        if not indexes:
            return None
        row = indexes[0].row()
        if row < 0 or row >= self._stages_table.rowCount():
            return None

        stage_id = self._stages_table.item(row, 0).text()
        state = self._stages_table.item(row, 1).text()
        
        # Get attempt from combo box or table item
        attempt_widget = self._stages_table.cellWidget(row, 2)
        if attempt_widget is not None and hasattr(attempt_widget, 'currentData'):
            attempt = attempt_widget.currentData()
            if attempt is None:
                attempt = int(attempt_widget.currentText())
        else:
            attempt = int(self._stages_table.item(row, 2).text()) if self._stages_table.item(row, 2) else 0
        
        model = self._stages_table.item(row, 3).text() if self._stages_table.item(row, 3) else ""
        duration = self._stages_table.item(row, 4).text() if self._stages_table.item(row, 4) else ""
        tokens = self._stages_table.item(row, 5).text() if self._stages_table.item(row, 5) else ""
        cost = self._stages_table.item(row, 6).text() if self._stages_table.item(row, 6) else ""
        outcome = self._stages_table.item(row, 7).text() if self._stages_table.item(row, 7) else ""

        return {
            "stage_id": stage_id,
            "state": state,
            "attempt_number": attempt,
            "requested_model": model,
            "duration": duration,
            "total_tokens": tokens,
            "cost_usd": cost,
            "outcome": outcome,
        }

    def _on_make_current(self) -> None:
        """Make the selected attempt current."""
        selected = self._selected_stage()
        if selected is None or self._bridge is None:
            return

        stage_id = selected["stage_id"]
        attempt = selected["attempt_number"]

        try:
            self._bridge.select_attempt(stage_id, attempt)
            self._show_status(f"Made {stage_id} attempt {attempt} current")
            asyncio.create_task(self._refresh_projection_async())
        except Exception as exc:
            self._show_status(f"Make current failed: {exc}")

    def _on_regenerate(self) -> None:
        """Regenerate the selected stage with a different model."""
        selected = self._selected_stage()
        if selected is None or self._bridge is None:
            return

        stage_id = selected["stage_id"]
        current_model = selected.get("requested_model") or ""

        # Create a proper QDialog for model input
        dialog = QDialog(self)
        dialog.setWindowTitle("Regenerate Stage")
        dialog.setObjectName("campaignRegenerationDialog")
        dialog_layout = QVBoxLayout(dialog)

        model_label = QLabel("New model:", dialog)
        dialog_layout.addWidget(model_label)

        model_input = QLineEdit(dialog)
        model_input.setText(current_model)
        dialog_layout.addWidget(model_input)

        # Use QDialogButtonBox for standard dialog buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            dialog
        )
        # normalize_dialog applies shared Cancel-before-primary action ordering.
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        dialog_actions = QHBoxLayout()
        dialog_actions.addStretch(1)
        dialog_actions.addWidget(button_box)
        dialog_layout.addLayout(dialog_actions)
        button_box.button(QDialogButtonBox.StandardButton.Ok).setProperty("role", "primary")
        normalize_dialog(dialog, size=(620, 340), description=f"Choose a replacement model for stage {stage_id}. This prepares regeneration; approval is still required.")

        if dialog.exec() != 1:  # QDialog.Accepted
            return

        new_model = model_input.text().strip()
        if not new_model:
            self._show_status("Model cannot be empty")
            return

        try:
            prepared = self._bridge.prepare_regeneration(
                stage_id, new_model, "desktop-user", pricing_evidence=self._pricing_document()
            )
            self._prepared_operation = prepared
            self._provider_routes = prepared.provider_routes
            self._view_model = self._view_model.apply_provider_routes(prepared.provider_routes)
            self._show_status(f"Regeneration prepared for {stage_id} with model {new_model}")
            # Show preflight in result text
            self._result_text.setPlainText(prepared.summary)
            self._approve_button.setEnabled(
                not any(route.get("kind") != "local_openai" for route in prepared.provider_routes.values())
                or prepared.approval.pricing_evidence is not None
            )
        except Exception as exc:
            self._approve_button.setEnabled(False)
            self._show_status(f"Regeneration prepare failed: {exc}")

    def _on_rerun_synthesis(self) -> None:
        """Rerun synthesis with the same inputs."""
        if self._bridge is None:
            return

        try:
            prepared = self._bridge.prepare_synthesis_rerun(
                "desktop-user", pricing_evidence=self._pricing_document()
            )
            self._prepared_operation = prepared
            self._provider_routes = prepared.provider_routes
            self._view_model = self._view_model.apply_provider_routes(prepared.provider_routes)
            self._show_status("Synthesis rerun prepared")
            # Render synthesis output if available
            self._render_synthesis_output(prepared)
            self._approve_button.setEnabled(
                not any(route.get("kind") != "local_openai" for route in prepared.provider_routes.values())
                or prepared.approval.pricing_evidence is not None
            )
        except Exception as exc:
            self._approve_button.setEnabled(False)
            self._show_status(f"Synthesis rerun prepare failed: {exc}")

    def _render_synthesis_output(self, prepared: object) -> None:
        """Render the durable synthesis output for a prepared rerun.

        The dock never parses run files itself: the bridge exposes the engine's
        own reader, so desktop inspection and headless ``inspect`` cannot drift
        apart (DESKTOP_SURFACE_AND_LIFECYCLE.md section 3).
        """
        if self._bridge is None:
            return
        stage_id = getattr(prepared, "stage_id", None)
        attempt = getattr(prepared, "attempt_number", None)
        text = None
        if stage_id:
            try:
                text = self._bridge.read_stage_output(stage_id, attempt)
            except Exception:
                text = None
        if text is None:
            text = getattr(prepared, "summary", "") or ""
        self._result_text.setPlainText(text)

    # ------------------------------------------------------------------
    # Cancel and visibility
    # ------------------------------------------------------------------

    def _on_cancel(self) -> None:
        """Cancel the active run."""
        if self._bridge is None:
            return

        async def cancel_async():
            try:
                await self._bridge.cancel()
                self._show_status("Cancel requested")
                await asyncio.sleep(0.1)  # Give time for state transition
                await self._refresh_projection_async()
            except Exception as exc:
                self._show_status(f"Cancel failed: {exc}")

        asyncio.create_task(cancel_async())

    def _on_visibility_changed(self, visible: bool) -> None:
        """Handle visibility changes for polling control."""
        if visible:
            asyncio.create_task(self._refresh_projection_async())
        self._sync_polling()

    def _sync_polling(self) -> None:
        """Sync polling timer based on visibility and run state."""
        should_poll = (
            not self._closed
            and self.isVisible()
            and self._projection is not None
            and self._projection.display_state not in ("succeeded", "failed", "timed_out", "cancelled")
        )
        if should_poll and not self._poll_timer.isActive():
            self._poll_timer.start()
        elif not should_poll and self._poll_timer.isActive():
            self._poll_timer.stop()

    # ------------------------------------------------------------------
    # Status and drain
    # ------------------------------------------------------------------

    def _show_status(self, message: str) -> None:
        """Show a status message in the dock's status area."""
        self._status_label.setText(message)

    def mark_closed(self) -> None:
        """Stop polling and mark closed."""
        if self._closed:
            return
        self._closed = True
        self._poll_timer.stop()

    async def drain(self) -> None:
        """Await bridge.close() if a bridge exists."""
        if self._bridge is not None:
            await self._bridge.close()
