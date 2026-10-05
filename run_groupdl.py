"""Entry point used for the packaged (PyInstaller) builds."""

import multiprocessing

from groupdl.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
