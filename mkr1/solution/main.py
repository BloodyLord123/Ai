import sys
from dotenv import load_dotenv
from PySide6.QtWidgets import QApplication
from ui_window import MainWindow


def main() -> None:
    load_dotenv()
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()