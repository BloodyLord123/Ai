import json
import os
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,)
from schemas import RequirementCheck
from worker import AuditWorker, DEFAULT_EVIDENCE_DIR, sanitize_folder_name

class CheckDetailDialog(QDialog):
    def __init__(self, check: RequirementCheck, parent: QWidget | None = None):
        super().__init__(parent)
        self.check = check
        self.setWindowTitle(f"Деталі вимоги [{check.requirement_id}] — {check.status}")
        self.resize(680, 520)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        status_colors = {
            "PASS": "#2e7d32",
            "WARNING": "#ef6c00",
            "FAIL": "#c62828",
        }
        badge_color = status_colors.get(check.status, "#424242")

        header_lbl = QLabel(
            f"<b>ID вимоги:</b> {check.requirement_id} &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"<b>Статус:</b> <span style='background-color:{badge_color}; padding:2px 8px; border-radius:4px;'>"
            f"{check.status}</span>"
        )
        header_lbl.setFont(QFont("Segoe UI", 11))
        layout.addWidget(header_lbl)

        layout.addWidget(QLabel("<b>Вимога з методички:</b>"))
        req_edit = QPlainTextEdit(check.requirement_text)
        req_edit.setReadOnly(True)
        req_edit.setMaximumHeight(110)
        layout.addWidget(req_edit)

        layout.addWidget(QLabel("<b>Доказ у роботі:</b>"))
        ev_edit = QPlainTextEdit(check.evidence_from_work)
        ev_edit.setReadOnly(True)
        ev_edit.setMaximumHeight(120)
        layout.addWidget(ev_edit)

        layout.addWidget(QLabel("<b>Рекомендація щодо виправлення:</b>"))
        rec_edit = QPlainTextEdit(check.recommendation)
        rec_edit.setReadOnly(True)
        layout.addWidget(rec_edit, stretch=1)

        btn_row = QHBoxLayout()
        copy_btn = QPushButton("Скопіювати рекомендацію")
        copy_btn.setStyleSheet("background-color: #1976d2; font-weight: bold;")
        copy_btn.clicked.connect(self._copy_recommendation)

        copy_all_btn = QPushButton("Скопіювати все")
        copy_all_btn.clicked.connect(self._copy_all)

        close_btn = QPushButton("Закрити")
        close_btn.clicked.connect(self.accept)

        btn_row.addWidget(copy_btn)
        btn_row.addWidget(copy_all_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _copy_recommendation(self) -> None:
        QApplication.clipboard().setText(self.check.recommendation)
        QMessageBox.information(self, "Скопійовано", "Текст рекомендації скопійовано в буфер обміну.")

    def _copy_all(self) -> None:
        full_text = (
            f"[{self.check.requirement_id}] ({self.check.status})\n"
            f"Вимога: {self.check.requirement_text}\n"
            f"Доказ: {self.check.evidence_from_work}\n"
            f"Рекомендація: {self.check.recommendation}"
        )
        QApplication.clipboard().setText(full_text)
        QMessageBox.information(self, "Скопійовано", "Повний опис пункту скопійовано в буфер обміну.")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI-асистент перевірки лабораторних робіт")
        self.resize(1180, 800)
        self.worker: AuditWorker | None = None
        self.evidence_dir = DEFAULT_EVIDENCE_DIR
        self._all_checks: list[RequirementCheck] = []

        self._setup_ui()
        self._apply_dark_theme()
        self._refresh_subjects_combo()
        self._refresh_history_combo()

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(10)

        config_group = QGroupBox("Параметри перевірки")
        form = QFormLayout(config_group)

        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText("Вставте GEMINI_API_KEY або задайте у .env / змінній середовища...")
        self.api_key_input.setText(os.environ.get("GEMINI_API_KEY", ""))
        form.addRow("Gemini API Key:", self.api_key_input)

        self.subject_combo = QComboBox()
        self.subject_combo.setEditable(True)
        self.subject_combo.setMinimumWidth(360)
        self.subject_combo.lineEdit().setPlaceholderText("Оберіть існуючий предмет або впишіть назву нового...")

        btn_new_subject = QPushButton("+ Нова папка")
        btn_new_subject.clicked.connect(self._create_new_subject_folder)

        row_subj = QHBoxLayout()
        row_subj.addWidget(self.subject_combo)
        row_subj.addWidget(btn_new_subject)
        row_subj.addStretch()
        form.addRow("Папка предмета:", row_subj)

        self.guidelines_input = QLineEdit()
        self.guidelines_input.setPlaceholderText("Шлях до методички (.pdf, .docx, .html, .md, .txt)")
        btn_guidelines = QPushButton("Обрати файл...")
        btn_guidelines.clicked.connect(self._browse_guidelines)
        row_g = QHBoxLayout()
        row_g.addWidget(self.guidelines_input)
        row_g.addWidget(btn_guidelines)
        form.addRow("Методичка:", row_g)

        self.report_input = QLineEdit()
        self.report_input.setPlaceholderText("Шлях до файлу звіту, зображення АБО до папки з лабораторною роботою...")
        btn_report_file = QPushButton("Обрати файл...")
        btn_report_file.clicked.connect(self._browse_report)
        btn_report_dir = QPushButton("Обрати папку...")
        btn_report_dir.clicked.connect(self._browse_report_dir)

        row_r = QHBoxLayout()
        row_r.addWidget(self.report_input)
        row_r.addWidget(btn_report_file)
        row_r.addWidget(btn_report_dir)
        form.addRow("Робота студента:", row_r)

        options_row = QHBoxLayout()
        self.variant_input = QLineEdit("Варіант 1")
        self.variant_input.setMaximumWidth(180)

        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 20)
        self.batch_spin.setValue(5)
        self.batch_spin.setToolTip("Кількість вимог методички в одному запиті до API")

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItems([
            "gemini-3.5-flash-lite",
            "gemini-3.6-flash",
            "gemini-4-hydrogen",
        ])

        options_row.addWidget(QLabel("Варіант:"))
        options_row.addWidget(self.variant_input)
        options_row.addSpacing(15)
        options_row.addWidget(QLabel("Вимог у батчі:"))
        options_row.addWidget(self.batch_spin)
        options_row.addSpacing(15)
        options_row.addWidget(QLabel("Модель:"))
        options_row.addWidget(self.model_combo)
        options_row.addStretch()
        form.addRow("Налаштування:", options_row)

        history_row = QHBoxLayout()
        self.history_combo = QComboBox()
        self.history_combo.setMinimumWidth(540)
        btn_load_history = QPushButton("Завантажити звіт")
        btn_load_history.clicked.connect(self._load_selected_history)

        history_row.addWidget(self.history_combo)
        history_row.addWidget(btn_load_history)
        history_row.addStretch()
        form.addRow("Минулі перевірки:", history_row)

        main_layout.addWidget(config_group)

        action_row = QHBoxLayout()
        self.start_btn = QPushButton("Запустити AI-перевірку")
        self.start_btn.setMinimumHeight(36)
        self.start_btn.setStyleSheet("background-color: #2e7d32; font-weight: bold; font-size: 13px;")
        self.start_btn.clicked.connect(self._start_audit)

        self.stop_btn = QPushButton("Зупинити")
        self.stop_btn.setMinimumHeight(36)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet("background-color: #c62828; font-weight: bold;")
        self.stop_btn.clicked.connect(self._stop_audit)

        self.score_label = QLabel("Готовність: —")
        self.score_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))

        action_row.addWidget(self.start_btn)
        action_row.addWidget(self.stop_btn)
        action_row.addSpacing(20)
        action_row.addWidget(self.score_label)
        action_row.addStretch()
        main_layout.addLayout(action_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label = QLabel("Очікування запуску...")
        main_layout.addWidget(self.progress_bar)
        main_layout.addWidget(self.status_label)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("<b>Фільтр результатів:</b>"))

        self.filter_group = QButtonGroup(self)
        self.radio_all = QRadioButton("Усі (0)")
        self.radio_fail = QRadioButton("Лише FAIL (0)")
        self.radio_fail_warn = QRadioButton("FAIL + WARNING (0)")
        self.radio_pass = QRadioButton("Лише PASS (0)")
        self.radio_all.setChecked(True)

        for idx, rb in enumerate([self.radio_all, self.radio_fail, self.radio_fail_warn, self.radio_pass]):
            self.filter_group.addButton(rb, idx)
            filter_row.addWidget(rb)

        self.filter_group.idClicked.connect(lambda _: self._apply_table_filter())

        filter_row.addStretch()
        main_layout.addLayout(filter_row)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([
            "ID", "Статус", "Вимога з методички", "Доказ у роботі", "Рекомендація"
        ])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setWordWrap(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._on_row_double_clicked)
        splitter.addWidget(self.table)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(160)
        self.log_box.setPlaceholderText("Журнал виконання батчів, токенів та повторних спроб 503/429...")
        splitter.addWidget(self.log_box)

        main_layout.addWidget(splitter, stretch=1)

    def _apply_dark_theme(self) -> None:
        self.setStyleSheet("""
            QMainWindow, QWidget, QDialog { background-color: #1e1e24; color: #e8e8ed; font-family: 'Segoe UI'; font-size: 12px; }
            QGroupBox { border: 1px solid #3a3a45; border-radius: 6px; margin-top: 8px; padding-top: 10px; font-weight: bold; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
            QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTableWidget {
                background-color: #282830; border: 1px solid #3f3f4e; border-radius: 4px; padding: 5px; color: #f0f0f5;
            }
            QRadioButton { spacing: 5px; padding: 2px 6px; }
            QHeaderView::section { background-color: #32323e; padding: 6px; border: 1px solid #3f3f4e; font-weight: bold; }
            QPushButton { background-color: #3b3b4a; border: none; border-radius: 4px; padding: 6px 14px; color: white; }
            QPushButton:hover { background-color: #4c4c5e; }
            QPushButton:disabled { background-color: #2a2a32; color: #6c6c7a; }
            QProgressBar { border: 1px solid #3f3f4e; border-radius: 4px; text-align: center; height: 18px; }
            QProgressBar::chunk { background-color: #1976d2; }
        """)

    def _update_filter_counts(self) -> None:
        total = len(self._all_checks)
        fails = sum(1 for c in self._all_checks if c.status == "FAIL")
        warns = sum(1 for c in self._all_checks if c.status == "WARNING")
        passes = sum(1 for c in self._all_checks if c.status == "PASS")

        self.radio_all.setText(f"Усі ({total})")
        self.radio_fail.setText(f"Лише FAIL ({fails})")
        self.radio_fail_warn.setText(f"FAIL + WARNING ({fails + warns})")
        self.radio_pass.setText(f"Лише PASS ({passes})")

    def _apply_table_filter(self) -> None:
        filter_id = self.filter_group.checkedId()
        allowed: set[str] | None = None
        if filter_id == 1:
            allowed = {"FAIL"}
        elif filter_id == 2:
            allowed = {"FAIL", "WARNING"}
        elif filter_id == 3:
            allowed = {"PASS"}

        for row_idx, chk in enumerate(self._all_checks):
            visible = allowed is None or chk.status in allowed
            self.table.setRowHidden(row_idx, not visible)

    def _on_row_double_clicked(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._all_checks):
            dlg = CheckDetailDialog(self._all_checks[row], self)
            dlg.exec()

    def _get_last_used_subject(self) -> str | None:
        history_file = self.evidence_dir / "history.jsonl"
        if not history_file.is_file():
            return None
        try:
            lines = [ln.strip() for ln in history_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if lines:
                last_entry = json.loads(lines[-1])
                return last_entry.get("subject")
        except Exception:
            return None
        return None

    def _refresh_subjects_combo(self, select_name: str | None = None) -> None:
        target_subject = select_name or self.subject_combo.currentText().strip() or self._get_last_used_subject()
        self.subject_combo.clear()

        if self.evidence_dir.exists():
            folders = sorted([d.name for d in self.evidence_dir.iterdir() if d.is_dir()])
            self.subject_combo.addItems(folders)

        if target_subject:
            self.subject_combo.setCurrentText(target_subject)

    def _create_new_subject_folder(self) -> None:
        name, ok = QInputDialog.getText(
            self,
            "Нова папка предмета",
            "Введіть назву предмета (папка створиться в evidence/):",
        )
        if ok and name.strip():
            clean_name = sanitize_folder_name(name)
            (self.evidence_dir / clean_name).mkdir(parents=True, exist_ok=True)
            self._refresh_subjects_combo(select_name=clean_name)
            self._append_log(f"Створено папку предмета: evidence/{clean_name}/")

    def _refresh_history_combo(self) -> None:
        self.history_combo.clear()
        if not self.evidence_dir.exists():
            self.history_combo.addItem("Історія порожня (папка evidence ще не створена)", None)
            return

        entries: list[tuple[str, str, str]] = []
        history_file = self.evidence_dir / "history.jsonl"
        seen_paths: set[Path] = set()

        if history_file.is_file():
            for line in history_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    rel_path = item.get("relative_path", "")
                    full_path = (self.evidence_dir / rel_path).resolve()
                    if full_path.is_file():
                        seen_paths.add(full_path)
                        subject = item.get("subject", full_path.parent.name)
                        date_str = item.get("date_human", item.get("timestamp", full_path.stem))
                        target = item.get("report_target", "—")
                        variant = item.get("variant", "—")
                        score = item.get("readiness_score", 0)
                        label = f"[{subject}] {date_str} | {target} ({variant}) — {score}%"
                        entries.append((date_str, label, str(full_path)))
                except Exception:
                    continue

        for f_path in self.evidence_dir.rglob("audit_*.json"):
            resolved = f_path.resolve()
            if resolved in seen_paths:
                continue
            try:
                data = json.loads(f_path.read_text(encoding="utf-8"))
                subject = data.get("subject", f_path.parent.name)
                date_str = data.get("date_human", data.get("timestamp", f_path.stem))
                target = data.get("report_target", "—")
                variant = data.get("variant", "—")
                score = data.get("readiness_score", 0)
                label = f"[{subject}] {date_str} | {target} ({variant}) — {score}%"
                entries.append((date_str, label, str(resolved)))
            except Exception:
                entries.append((f_path.name, f"{f_path.parent.name}/{f_path.name}", str(resolved)))

        if not entries:
            self.history_combo.addItem("Минулих перевірок ще немає", None)
            return

        entries.sort(key=lambda x: x[0], reverse=True)
        for _, label, path_str in entries:
            self.history_combo.addItem(label, path_str)

    def _load_selected_history(self) -> None:
        file_str = self.history_combo.currentData()
        if not file_str:
            return
        file_path = Path(file_str)
        if not file_path.is_file():
            QMessageBox.warning(self, "Помилка", f"Файл не знайдено: {file_path}")
            return

        data = json.loads(file_path.read_text(encoding="utf-8"))
        self.table.setRowCount(0)
        self._all_checks.clear()
        self._update_filter_counts()

        checks = [RequirementCheck.model_validate(item) for item in data.get("checks", [])]
        self._on_batch_ready(checks)

        score = data.get("readiness_score", 0)
        metrics = data.get("metrics", {})
        subject = data.get("subject", file_path.parent.name)
        imgs_cnt = len(data.get("images_checked", []))
        self.score_label.setText(f"Готовність: {score}%")
        self.status_label.setText(f"Завантажено: evidence/{subject}/{file_path.name}")
        self.log_box.clear()
        self._append_log(
            f"[ІСТОРІЯ] Предмет: {subject} | Файл: {file_path.name}\n"
            f"Модель: {metrics.get('model', '—')} | Час: {metrics.get('elapsed_seconds', 0)} с | "
            f"Токени: {metrics.get('total_tokens', 0)} | Зображень: {imgs_cnt}"
        )

    def _browse_guidelines(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Оберіть файл методички", "", "Документи (*.pdf *.docx *.html *.htm *.md *.txt);;Усі файли (*)"
        )
        if path:
            self.guidelines_input.setText(path)

    def _browse_report(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Оберіть файл звіту або зображення",
            "",
            "Документи та зображення (*.md *.docx *.pdf *.txt *.html *.png *.jpg *.jpeg *.webp);;Усі файли (*)",
        )
        if path:
            self.report_input.setText(path)

    def _browse_report_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Оберіть папку з лабораторною роботою")
        if path:
            self.report_input.setText(path)

    def _append_log(self, text: str) -> None:
        self.log_box.appendPlainText(text)

    def _start_audit(self) -> None:
        api_key = self.api_key_input.text().strip()
        subject_name = self.subject_combo.currentText().strip()
        g_raw = self.guidelines_input.text().strip()
        r_raw = self.report_input.text().strip()

        if not api_key:
            QMessageBox.warning(self, "Помилка", "Вкажіть Gemini API Key.")
            return
        if not subject_name:
            QMessageBox.warning(self, "Помилка", "Оберіть існуючий предмет або впишіть назву нової папки.")
            return
        if not g_raw or not r_raw:
            QMessageBox.warning(self, "Помилка", "Вкажіть шляхи до методички та файлу/папки роботи.")
            return

        g_path = Path(g_raw)
        r_path = Path(r_raw)
        if not g_path.is_file() or not r_path.exists():
            QMessageBox.warning(self, "Помилка", "Перевірте коректність шляхів до методички та файлу/папки роботи.")
            return

        self.table.setRowCount(0)
        self._all_checks.clear()
        self._update_filter_counts()
        self.log_box.clear()
        self.score_label.setText("Готовність: перевірка...")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        self.worker = AuditWorker(
            api_key=api_key,
            subject_name=subject_name,
            guidelines_path=g_path,
            report_path=r_path,
            variant=self.variant_input.text().strip() or "Варіант 1",
            batch_size=self.batch_spin.value(),
            model_name=self.model_combo.currentText().strip(),
            evidence_dir=self.evidence_dir,
        )
        self.worker.log_signal.connect(self._append_log)
        self.worker.progress_signal.connect(self._on_progress)
        self.worker.batch_ready_signal.connect(self._on_batch_ready)
        self.worker.ask_retry_signal.connect(self._on_ask_retry)
        self.worker.finished_signal.connect(self._on_finished)
        self.worker.cancelled_signal.connect(self._on_cancelled)
        self.worker.error_signal.connect(self._on_error)
        self.worker.start()

    def _stop_audit(self) -> None:
        if self.worker and self.worker.isRunning():
            self.stop_btn.setEnabled(False)
            self.status_label.setText("Зупинка запиту...")
            self._append_log("Зупинка активного запиту до API...")
            self.worker.request_abort()

    def _on_progress(self, current: int, total: int, status: str) -> None:
        self.status_label.setText(status)
        if current >= 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(current)

    def _on_batch_ready(self, checks: list[RequirementCheck]) -> None:
        status_colors = {
            "PASS": QColor("#2e7d32"),
            "WARNING": QColor("#ef6c00"),
            "FAIL": QColor("#c62828"),
        }
        for chk in checks:
            self._all_checks.append(chk)
            row = self.table.rowCount()
            self.table.insertRow(row)

            id_item = QTableWidgetItem(chk.requirement_id)
            status_item = QTableWidgetItem(chk.status)
            status_item.setBackground(status_colors.get(chk.status, QColor("#424242")))
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            self.table.setItem(row, 0, id_item)
            self.table.setItem(row, 1, status_item)
            self.table.setItem(row, 2, QTableWidgetItem(chk.requirement_text))
            self.table.setItem(row, 3, QTableWidgetItem(chk.evidence_from_work))
            self.table.setItem(row, 4, QTableWidgetItem(chk.recommendation))

        self._update_filter_counts()
        self._apply_table_filter()

    def _on_ask_retry(self, title: str, message: str) -> None:
        msg_box = QMessageBox(self)
        msg_box.setIcon(QMessageBox.Icon.Warning)
        msg_box.setWindowTitle(title)
        msg_box.setText(message)

        btn_continue = msg_box.addButton("Продовжити (ще 5 спроб)", QMessageBox.ButtonRole.AcceptRole)
        msg_box.addButton("Закрити перевірку", QMessageBox.ButtonRole.RejectRole)
        msg_box.setDefaultButton(btn_continue)
        msg_box.exec()

        if self.worker:
            self.worker.resolve_retry_dialog(continue_retries=(msg_box.clickedButton() == btn_continue))

    def _on_finished(self, payload: dict) -> None:
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        score = payload["readiness_score"]
        metrics = payload["metrics"]
        self.score_label.setText(f"Готовність: {score}%")
        self._append_log(
            f"Готово! Предмет: {payload['subject']} | Час: {metrics['elapsed_seconds']} с | "
            f"Токени: {metrics['total_tokens']} | Збережено: {payload['saved_to']}"
        )
        self._refresh_subjects_combo(select_name=payload["subject"])
        self._refresh_history_combo()

    def _on_cancelled(self, msg: str, partial_data: list) -> None:
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.score_label.setText("Готовність: зупинено")

        if partial_data and self.worker:
            state = partial_data[0]
            raw_checks = state.get("checks", [])
            if raw_checks:
                reply = QMessageBox.question(
                    self,
                    "Зберегти частковий результат?",
                    f"Перевірку зупинено, але вже перевірено {len(raw_checks)} вимог(и).\n"
                    f"Зберегти частковий звіт в історію?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    checks_objs = [RequirementCheck.model_validate(c) for c in raw_checks]
                    payload = self.worker.save_report(
                        all_checks=checks_objs,
                        loaded_files=state.get("loaded_files", []),
                        image_names=state.get("image_names", []),
                        elapsed=state.get("elapsed", 0.0),
                        prompt_tokens=state.get("prompt_tokens", 0),
                        output_tokens=state.get("output_tokens", 0),
                    )
                    self.score_label.setText(f"Готовність: {payload['readiness_score']}% (частково)")
                    self._append_log(f"Частковий звіт збережено: {payload['saved_to']}")
                    self._refresh_subjects_combo(select_name=payload["subject"])
                    self._refresh_history_combo()

    def _on_error(self, err_msg: str) -> None:
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.score_label.setText("Готовність: помилка")
        self._append_log(f"[ПОМИЛКА] {err_msg}")
        QMessageBox.critical(self, "Помилка виконання", err_msg)