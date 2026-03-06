from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from rich.logging import RichHandler


def configure_logging(log_file: Optional[Path] = None, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("nightfall_mix")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers = []
    logger.propagate = False

    console = RichHandler(rich_tracebacks=True, show_path=False)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_formatter = logging.Formatter("%(message)s")
    console.setFormatter(console_formatter)
    logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(file_handler)

    return logger

