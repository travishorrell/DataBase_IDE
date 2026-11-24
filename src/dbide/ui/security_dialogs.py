"""Password hashing / encryption tool, security settings, vault unlock."""
from __future__ import annotations

import base64
import html

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dbide.core import crypto, secrets
from dbide.ui.worker import run_async


def _algo_combo(algos) -> QComboBox:
    cb = QComboBox()
    for a in algos:
        cb.addItem(a.label, a.key)
        cb.setItemData(cb.count() - 1, a.summary, Qt.ItemDataRole.ToolTipRole)
    return cb


class PasswordToolDialog(QDialog):
    """Learn and use hashing and encryption by hand."""

    def __init__(self, manager, conn_id: str | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Password & Encryption Tool")
        self.resize(680, 520)
        self.manager, self.conn_id = manager, conn_id
        tabs = QTabWidget()
        tabs.addTab(self._hash_tab(), "Hash a password")
        tabs.addTab(self._verify_tab(), "Check a password")
        tabs.addTab(self._encrypt_tab(), "Encrypt / decrypt")
        lay = QVBoxLayout(self)
        intro = QLabel(
            "<b>Hashing</b> is one-way (for passwords): you can check a guess but never get the password back. "
            "<b>Encryption</b> is two-way (for data you must read again) and needs a key."
        )
        intro.setWordWrap(True)
        lay.addWidget(intro)
        lay.addWidget(tabs)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        lay.addWidget(close)

    def _hash_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.h_pw = QLineEdit()
        self.h_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.h_algo = _algo_combo(crypto.HASH_ALGORITHMS)
        self.h_about = QLabel()
        self.h_about.setWordWrap(True)
        self.h_algo.currentIndexChanged.connect(lambda _: self.h_about.setText(crypto.ALGORITHMS[self.h_algo.currentData()].summary))
        self.h_about.setText(crypto.HASH_ALGORITHMS[0].summary)
        self.h_out = QPlainTextEdit()
        self.h_out.setReadOnly(True)
        self.h_out.setMaximumHeight(80)
        go = QPushButton("Hash")
        go.clicked.connect(self._do_hash)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.h_out.toPlainText()))
        row = QHBoxLayout()
        row.addWidget(go)
        row.addWidget(copy)
        row.addStretch()
        f.addRow("Password", self.h_pw)
        f.addRow("Algorithm", self.h_algo)
        f.addRow("", self.h_about)
        f.addRow("", row)
        f.addRow("Hash", self.h_out)
        self.h_note = QLabel("Hash the same password twice and you get different results: each hash has its own random <i>salt</i>.")
        self.h_note.setWordWrap(True)
        f.addRow("", self.h_note)
        return w

    def _do_hash(self):
        pw, algo = self.h_pw.text(), self.h_algo.currentData()
        if not pw:
            return
        self.h_out.setPlainText("Hashing… (slow on purpose)")
        run_async(lambda: crypto.hash_password(pw, algo), self.h_out.setPlainText,
                  lambda m: self.h_out.setPlainText(f"Error: {m}"))

    def _verify_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.v_pw = QLineEdit()
        self.v_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.v_hash = QLineEdit()
        self.v_hash.setPlaceholderText("paste a stored hash, e.g. $argon2id$…")
        self.v_res = QLabel("")
        go = QPushButton("Check")
        go.clicked.connect(self._do_verify)
        f.addRow("Password guess", self.v_pw)
        f.addRow("Stored hash", self.v_hash)
        f.addRow("", go)
        f.addRow("", self.v_res)
        explain = QLabel("Your app does exactly this at login: hash the typed password with the stored salt and compare.")
        explain.setWordWrap(True)
        f.addRow("", explain)
        return w

    def _do_verify(self):
        pw, h = self.v_pw.text(), self.v_hash.text().strip()
        algo = crypto.detect_hash(h)
        if not algo:
            self.v_res.setText("That doesn't look like a bcrypt / Argon2 / PBKDF2 / scrypt hash.")
            return

        def show(ok):
            name = crypto.ALGORITHMS[algo].label
            self.v_res.setText(f"✅ Correct password ({name})" if ok else f"❌ Wrong password ({name})")

        run_async(lambda: crypto.verify_password(pw, h), show, self.v_res.setText)

    def _encrypt_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.e_conn = QComboBox()
        for cid, s in self.manager.open_sessions():
            self.e_conn.addItem(s.cfg.name, cid)
        if self.conn_id:
            self.e_conn.setCurrentIndex(max(0, self.e_conn.findData(self.conn_id)))
        self.e_algo = _algo_combo(crypto.ENCRYPT_ALGORITHMS)
        self.e_in = QPlainTextEdit()
        self.e_in.setMaximumHeight(70)
        self.e_out = QPlainTextEdit()
        self.e_out.setMaximumHeight(70)
        self.e_out.setReadOnly(True)
        enc = QPushButton("Encrypt")
        enc.clicked.connect(self._do_encrypt)
        dec = QPushButton("Decrypt")
        dec.clicked.connect(self._do_decrypt)
        row = QHBoxLayout()
        row.addWidget(enc)
        row.addWidget(dec)
        row.addStretch()
        f.addRow("Key of connection", self.e_conn)
        f.addRow("Algorithm", self.e_algo)
        f.addRow("Input", self.e_in)
        f.addRow("", row)
        f.addRow("Output", self.e_out)
        keys = QHBoxLayout()
        exp = QPushButton("Export Key…")
        exp.clicked.connect(self._export_key)
        imp = QPushButton("Import Key…")
        imp.clicked.connect(self._import_key)
        keys.addWidget(exp)
        keys.addWidget(imp)
        keys.addStretch()
        f.addRow("Field key", keys)
        warn = QLabel("⚠️ Each connection has its own 256-bit key, kept in your secret store. "
                      "<b>If the key is lost, encrypted values can never be read again.</b> Export it and keep a backup.")
        warn.setWordWrap(True)
        f.addRow("", warn)
        return w

    def _key(self, create: bool):
        cid = self.e_conn.currentData()
        if not cid:
            QMessageBox.information(self, "Encryption", "Connect to a database first; each connection has its own key.")
            return None
        return secrets.field_key(cid, create=create)

    def _do_encrypt(self):
        key = self._key(True)
        if key:
            self.e_out.setPlainText(crypto.encrypt_value(self.e_in.toPlainText(), key, self.e_algo.currentData()))

    def _do_decrypt(self):
        key = self._key(False)
        if not key:
            self.e_out.setPlainText("No key exists for this connection yet.")
            return
        try:
            self.e_out.setPlainText(crypto.decrypt_value(self.e_in.toPlainText().strip(), key))
        except ValueError as exc:
            self.e_out.setPlainText(str(exc))

    def _export_key(self):
        key = self._key(True)
        if key:
            QInputDialog.getText(self, "Field key", "Copy this key and store it somewhere safe (a password manager):",
                                 QLineEdit.EchoMode.Normal, base64.b64encode(key).decode())

    def _import_key(self):
        cid = self.e_conn.currentData()
        if not cid:
            return
        text, ok = QInputDialog.getText(self, "Import field key", "Paste the base64 key:")
        if ok and text.strip():
            if secrets.field_key(cid) and QMessageBox.warning(
                self, "Replace key?", "This connection already has a key. Values encrypted with it won't decrypt after replacing it. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
                return
            try:
                secrets.import_field_key(cid, text)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Import key", str(exc))


class SecuritySettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Security Settings")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        intro = QLabel("Where should DB IDE keep <b>saved connection passwords</b> and <b>field-encryption keys</b>?")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        current = secrets.store().name
        self.radios = {}
        descriptions = {
            "keychain": "Protected by your macOS login. Convenient and secure for a personal machine.",
            "vault": "An AES-256-GCM encrypted file; its key is derived from a master password with scrypt. "
                     "You'll unlock it once per launch. Good for shared machines or syncing the vault file.",
            "session": "Nothing is written to disk; you type passwords every launch. "
                       "⚠️ Field-encryption keys are lost when you quit, so encrypted columns become unreadable.",
        }
        for key, label in secrets.BACKENDS.items():
            rb = QRadioButton(label)
            rb.setChecked(key == current)
            d = QLabel(descriptions[key])
            d.setWordWrap(True)
            d.setContentsMargins(24, 0, 0, 8)
            lay.addWidget(rb)
            lay.addWidget(d)
            self.radios[key] = rb
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._apply)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _apply(self):
        choice = next(k for k, rb in self.radios.items() if rb.isChecked())
        if choice == secrets.store().name:
            self.accept()
            return
        new = secrets.make_store(choice)
        if choice == "vault":
            v: secrets.VaultStore = new  # type: ignore[assignment]
            if v.exists:
                if not unlock_vault(self, v):
                    return
            else:
                pw, ok = QInputDialog.getText(self, "Create vault", "Choose a master password (8+ characters):", QLineEdit.EchoMode.Password)
                if not ok:
                    return
                pw2, ok = QInputDialog.getText(self, "Create vault", "Type it again:", QLineEdit.EchoMode.Password)
                if not ok or pw != pw2:
                    QMessageBox.warning(self, "Vault", "The passwords didn't match.")
                    return
                try:
                    v.create(pw)
                except ValueError as exc:
                    QMessageBox.warning(self, "Vault", str(exc))
                    return
        if choice == "session" and QMessageBox.warning(
            self, "Don't save secrets",
            "Saved passwords and encryption keys will be moved to memory only and lost when you quit.\n"
            "Export your field keys first if you use encrypted columns. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        try:
            n = secrets.switch_backend(new)
        except secrets.VaultLocked:
            QMessageBox.warning(self, "Vault locked", "Unlock the current vault first (restart the app and enter the master password).")
            return
        QMessageBox.information(self, "Security", f"Done. {n} secret(s) moved to: {html.escape(secrets.BACKENDS[choice])}.")
        self.accept()


def unlock_vault(parent, vault: secrets.VaultStore) -> bool:
    for _ in range(3):
        pw, ok = QInputDialog.getText(parent, "Unlock vault", "Master password for your DB IDE vault:", QLineEdit.EchoMode.Password)
        if not ok:
            return False
        try:
            vault.unlock(pw)
            return True
        except ValueError as exc:
            QMessageBox.warning(parent, "Vault", str(exc))
    return False
