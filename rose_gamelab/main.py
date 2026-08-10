"""Main entry point for Rose GameLab."""

import sys

import click
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont

from rose_gamelab.config import Config
from rose_gamelab.ui.app import MainWindow


@click.command()
@click.option("--no-ui", is_flag=True, help="Run in headless mode (future)")
@click.option("--version", is_flag=True, help="Show version")
def main(version: bool, no_ui: bool) -> None:
    """GameLab — Your games, one launcher."""
    if version:
        from rose_gamelab import __version__
        print(f"GameLab v{__version__}")
        return

    # Headless mode stub
    if no_ui:
        print("Headless mode not yet implemented. Use the UI version.")
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setApplicationName("GameLab")
    app.setOrganizationName("Rose")
    app.setApplicationVersion("0.1.0")

    # Set font (use system default for compatibility)
    font = QFont("Sans", 12)
    QApplication.setFont(font)

    # Show window
    config = Config()
    window = MainWindow(config)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
