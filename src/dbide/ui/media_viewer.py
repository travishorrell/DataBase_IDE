"""Preview images / play videos from a cell, and show where they are physically stored."""
from __future__ import annotations

import html
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
)

from dbide.core import media
from dbide.core.config import ConnectionConfig
from dbide.core.roles import ColumnRole


class MediaViewer(QDialog):
    def __init__(self, value, cfg: ConnectionConfig, where: str, parent=None):
        """`value` is bytes (BLOB) or a stored path string. `where` names table.column and row."""
        super().__init__(parent)
        self.setWindowTitle("Media Viewer")
        self.resize(820, 640)
        self.player = None
        lay = QVBoxLayout(self)
        info = QLabel()
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        btns = QHBoxLayout()

        data: bytes | None = None
        file_path: Path | None = None
        if isinstance(value, (bytes, bytearray, memoryview)):
            data = bytes(value)
            mime = media.detect_mime(data)
            kind = media.kind_from_mime(mime)
            info.setText(
                f"<b>Stored inside the database</b> (BLOB) in {html.escape(where)}.<br>"
                f"Type: {mime} · Size: {media.human_size(len(data))}<br>"
                "<i>The bytes are part of the row itself. Copying the database file copies the media too.</i>"
            )
            save = QPushButton("Save a Copy…")
            save.clicked.connect(lambda: self._save_copy(data, mime))
            btns.addWidget(save)
        else:
            stored = str(value)
            file_path = media.resolve(stored, cfg)
            exists = file_path.exists()
            kind = media.kind_from_name(file_path.name)
            if exists:
                with open(file_path, "rb") as fh:
                    kind = media.kind_from_mime(media.detect_mime(fh.read(32))) if kind == "file" else kind
            info.setText(
                f"<b>Stored as a file on disk</b>; {html.escape(where)} holds its path.<br>"
                f"Saved value: <code>{html.escape(stored)}</code><br>"
                f"Actual file: <code>{html.escape(str(file_path))}</code>"
                + (f" · {media.human_size(file_path.stat().st_size)}" if exists else
                   "<br><b style='color:#cb2431'>⚠ The file is missing.</b> The row points to a file that was moved or deleted.")
                + "<br><i>The database only keeps the path. Back up and move this folder together with the database.</i>"
            )
            reveal = QPushButton("Show in Finder")
            reveal.setEnabled(exists)
            reveal.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(file_path.parent))))
            btns.addWidget(reveal)
        lay.addWidget(info)

        if kind == "image":
            pm = QPixmap()
            ok = pm.loadFromData(data) if data is not None else (file_path.exists() and pm.load(str(file_path)))
            label = QLabel()
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if ok:
                label.setPixmap(pm.scaled(780, 520, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                                if pm.width() > 780 or pm.height() > 520 else pm)
            else:
                label.setText("Can't display this image.")
            sa = QScrollArea()
            sa.setWidget(label)
            sa.setWidgetResizable(True)
            lay.addWidget(sa, 1)
        elif kind == "video":
            src = file_path
            if data is not None:
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4" if "mp4" in media.detect_mime(data) else ".bin")
                tmp.write(data)
                tmp.close()
                src = Path(tmp.name)
            if src and src.exists():
                lay.addLayout(self._video(src), 1)
            else:
                lay.addWidget(QLabel("Video file not found."), 1)
        else:
            lay.addWidget(QLabel("This isn't an image or video the viewer can show."), 1)
        btns.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        lay.addLayout(btns)

    def _video(self, path: Path):
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget

        box = QVBoxLayout()
        vw = QVideoWidget()
        vw.setMinimumHeight(360)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(vw)
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        controls = QHBoxLayout()
        play = QPushButton("▶ Play")

        def toggle():
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.player.pause()
                play.setText("▶ Play")
            else:
                self.player.play()
                play.setText("❚❚ Pause")

        play.clicked.connect(toggle)
        slider = QSlider(Qt.Orientation.Horizontal)
        self.player.durationChanged.connect(lambda d: slider.setRange(0, d))
        self.player.positionChanged.connect(lambda p: slider.blockSignals(True) or slider.setValue(p) or slider.blockSignals(False))
        slider.sliderMoved.connect(self.player.setPosition)
        controls.addWidget(play)
        controls.addWidget(slider, 1)
        box.addWidget(vw, 1)
        box.addLayout(controls)
        return box

    def _save_copy(self, data: bytes, mime: str) -> None:
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
               "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"}.get(mime, ".bin")
        path, _ = QFileDialog.getSaveFileName(self, "Save copy", "media" + ext)
        if path:
            Path(path).write_bytes(data)

    def done(self, r):
        if self.player:
            self.player.stop()
        super().done(r)


class InsertMediaDialog(QDialog):
    """Pick a file and decide (and learn) how it will be stored."""

    def __init__(self, cfg: ConnectionConfig, column: str, role: ColumnRole | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Insert media into {column}")
        self.setMinimumWidth(560)
        self.cfg, self.role = cfg, role
        self.value = None
        self.new_role: ColumnRole | None = None

        self.path = QLineEdit()
        self.path.setReadOnly(True)
        browse = QPushButton("Choose File…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.path, 1)
        row.addWidget(browse)

        self.blob = QRadioButton("Store the file INSIDE the database (BLOB)")
        self.pathopt = QRadioButton(f"Copy the file into the media folder and store its PATH\n   ({media.default_folder(cfg)})")
        grp = QButtonGroup(self)
        grp.addButton(self.blob)
        grp.addButton(self.pathopt)
        explain = QLabel(
            "<b>BLOB</b>: one self-contained database, but it grows by the file's size and backups get slower.<br>"
            "<b>Path</b>: the database stays small and big videos stream fast, but the media folder must travel with the database.<br>"
            "<i>Rule of thumb: small images → BLOB is fine. Videos and large files → path.</i>"
        )
        explain.setTextFormat(Qt.TextFormat.RichText)
        explain.setWordWrap(True)
        self.remember = QCheckBox("Remember this choice for the column (sets its role)")
        self.remember.setChecked(True)
        lay = QVBoxLayout(self)
        lay.addLayout(row)
        if role and role.kind == "media":
            fixed = QLabel(f"This column's role is <b>{html.escape(role.label())}</b>, so the file will be stored "
                           + ("inside the database." if role.storage == "blob" else "in the media folder and its path saved."))
            fixed.setWordWrap(True)
            lay.addWidget(fixed)
            (self.blob if role.storage == "blob" else self.pathopt).setChecked(True)
        else:
            self.pathopt.setChecked(True)
            lay.addWidget(self.blob)
            lay.addWidget(self.pathopt)
            lay.addWidget(explain)
            lay.addWidget(self.remember)
        self.size_label = QLabel("")
        lay.addWidget(self.size_label)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "Choose image or video", "",
                                           "Media (*.png *.jpg *.jpeg *.gif *.webp *.bmp *.mp4 *.mov *.m4v *.webm *.mkv *.avi);;All files (*)")
        if p:
            self.set_file(p)

    def set_file(self, p: str) -> None:
        self.path.setText(p)
        size = Path(p).stat().st_size
        self.size_label.setText(f"{media.kind_from_name(p).title()}, {media.human_size(size)}"
                                + ("  ⚠ large: a path is recommended" if size > 5 * 1024 * 1024 else ""))

    def _accept(self):
        src = self.path.text()
        if not src:
            QMessageBox.information(self, "Insert media", "Choose a file first.")
            return
        blob = self.blob.isChecked()
        try:
            self.value = media.read_bytes(src) if blob else media.store_file(src, self.cfg, self.role.folder if self.role else "")
        except OSError as exc:
            QMessageBox.critical(self, "Insert media", str(exc))
            return
        if not (self.role and self.role.kind == "media") and self.remember.isChecked():
            self.new_role = ColumnRole("media", storage="blob" if blob else "path", media_kind=media.kind_from_name(src)
                                       if media.kind_from_name(src) != "file" else "any")
        self.accept()
