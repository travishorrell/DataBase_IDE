import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from dbide.ui.help_panel import install_hover_help
    from dbide.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("DB IDE")
    app.setOrganizationName("dbide")
    install_hover_help(app)
    from dbide.ui.theme import ThemeManager

    ThemeManager.get().apply(app)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
