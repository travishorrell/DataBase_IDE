"""Create / edit a saved connection."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from dbide.core.config import ConnectionConfig, get_password
from dbide.core.session import DatabaseSession
from dbide.drivers import all_drivers
from dbide.ui.worker import run_async


class ConnectionDialog(QDialog):
    def __init__(self, parent=None, cfg: ConnectionConfig | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit Connection" if cfg else "New Connection")
        self.setMinimumWidth(480)
        self.cfg = cfg
        self.password: str | None = None

        self.name = QLineEdit()
        self.driver = QComboBox()
        for d in all_drivers():
            self.driver.addItem(d.label, d.key)

        # SQLite page
        self.file_path = QLineEdit()
        self.file_path.setPlaceholderText("/path/to/database.sqlite")
        browse = QPushButton("Open…")
        browse.clicked.connect(self._browse)
        new = QPushButton("New…")
        new.setToolTip("Choose where to create a new, empty SQLite database")
        new.clicked.connect(self._new_file)
        row = QHBoxLayout()
        row.addWidget(self.file_path, 1)
        row.addWidget(browse)
        row.addWidget(new)
        sqlite_page = QWidget()
        sf = QFormLayout(sqlite_page)
        sf.setContentsMargins(0, 0, 0, 0)
        sf.addRow("Database file", row)
        sf.addRow("", QLabel("A file that doesn't exist yet will be created on connect."))

        # server page
        self.host = QLineEdit("localhost")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(3306)
        self.user = QLineEdit("root")
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.save_pw = QCheckBox("Save password in the system keychain")
        self.database = QLineEdit()
        self.database.setPlaceholderText("optional default database")
        self.ssl = QCheckBox("Use SSL/TLS")
        server_page = QWidget()
        f = QFormLayout(server_page)
        f.setContentsMargins(0, 0, 0, 0)
        f.addRow("Host", self.host)
        f.addRow("Port", self.port)
        f.addRow("User", self.user)
        f.addRow("Password", self.pw)
        f.addRow("", self.save_pw)
        f.addRow("Database", self.database)
        f.addRow("", self.ssl)

        self.pages = QStackedWidget()
        self.pages.addWidget(sqlite_page)
        self.pages.addWidget(server_page)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        test = QPushButton("Test Connection")
        test.clicked.connect(self._test)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.addButton(test, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        top = QFormLayout()
        top.addRow("Name", self.name)
        top.addRow("Type", self.driver)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addSpacing(6)
        lay.addWidget(self.pages)
        lay.addWidget(self.status)
        lay.addWidget(buttons)

        self.driver.currentIndexChanged.connect(self._driver_changed)
        if cfg:
            self._load(cfg)
        self._driver_changed()

    def _driver_changed(self) -> None:
        key = self.driver.currentData()
        drv = next(d for d in all_drivers() if d.key == key)
        self.pages.setCurrentIndex(0 if drv.file_based else 1)
        if not drv.file_based and drv.default_port and not self.cfg:
            self.port.setValue(drv.default_port)
            defaults = {"mysql": ("root", ""), "postgresql": ("postgres", "postgres"), "mssql": ("sa", "master")}
            user, db = defaults.get(drv.key, ("", ""))
            if self.user.text() in ("", "root", "postgres", "sa"):
                self.user.setText(user)
            if self.database.text() in ("", "postgres", "master"):
                self.database.setText(db)
            self.database.setPlaceholderText({"mysql": "optional default database", "postgresql": "database (default: postgres)",
                                              "mssql": "database (default: master)"}.get(drv.key, ""))
            self.ssl.setVisible(drv.key in ("mysql", "postgresql"))

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open SQLite database", "", "SQLite (*.db *.sqlite *.sqlite3 *.db3);;All files (*)")
        if path:
            self.file_path.setText(path)
            if not self.name.text():
                self.name.setText(Path(path).stem)

    def _new_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Create SQLite database", "new_database.sqlite", "SQLite (*.sqlite *.db)")
        if path:
            self.file_path.setText(path)
            if not self.name.text():
                self.name.setText(Path(path).stem)

    def _load(self, cfg: ConnectionConfig) -> None:
        self.name.setText(cfg.name)
        self.driver.setCurrentIndex(self.driver.findData(cfg.driver))
        if cfg.driver == "sqlite":
            self.file_path.setText(cfg.database)
        else:
            self.host.setText(cfg.host)
            self.port.setValue(cfg.port or 3306)
            self.user.setText(cfg.user)
            self.database.setText(cfg.database)
            self.save_pw.setChecked(cfg.save_password)
            self.ssl.setChecked(bool(cfg.options.get("ssl")))
            self.pw.setText(get_password(cfg) or "")

    def build(self) -> ConnectionConfig:
        key = self.driver.currentData()
        base = dict(id=self.cfg.id) if self.cfg else {}
        if key == "sqlite":
            path = self.file_path.text().strip()
            return ConnectionConfig(name=self.name.text().strip() or Path(path).stem or "SQLite", driver=key, database=path, **base)
        return ConnectionConfig(
            name=self.name.text().strip() or f"{self.host.text()}",
            driver=key,
            host=self.host.text().strip(),
            port=self.port.value(),
            user=self.user.text().strip(),
            database=self.database.text().strip(),
            save_password=self.save_pw.isChecked(),
            options={"ssl": self.ssl.isChecked()},
            **base,
        )

    def _valid(self, cfg: ConnectionConfig) -> bool:
        if cfg.driver == "sqlite" and not cfg.database:
            self.status.setText("Choose a database file.")
            return False
        if cfg.driver != "sqlite" and not cfg.host:
            self.status.setText("Host is required.")
            return False
        return True

    def _test(self) -> None:
        cfg = self.build()
        if not self._valid(cfg):
            return
        pw = self.pw.text() or None
        self.status.setText("Connecting…")

        def work():
            s = DatabaseSession(cfg, pw)
            try:
                return s.connect()
            finally:
                s.close()

        run_async(work, lambda v: self.status.setText(f"✓ Connected: {v}"), lambda m: self.status.setText(f"✗ {m}"))

    def _accept(self) -> None:
        cfg = self.build()
        if not self._valid(cfg):
            return
        self.result_cfg = cfg
        self.password = self.pw.text() or None if cfg.driver != "sqlite" else None
        self.accept()
