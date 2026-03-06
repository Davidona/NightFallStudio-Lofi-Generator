from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal

from nightfall_desktop.models.session_models import EngineSessionModel, GuiSettings, RenderArtifactsModel
from nightfall_desktop.services.engine_service import GuiEngineService
from nightfall_desktop.services.media_tools_service import MediaToolsService


class AnalysisWorker(QThread):
    log = Signal(str)
    progress = Signal(int, int)
    finished_session = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        service: GuiEngineService,
        settings: GuiSettings,
        ordered_paths: Optional[list[Path]] = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._settings = settings
        self._ordered_paths = ordered_paths
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            session = self._service.analyze_folder(
                settings=self._settings,
                ordered_paths=self._ordered_paths,
                on_log=self.log.emit,
                on_progress=self.progress.emit,
                should_cancel=lambda: self._cancelled,
            )
            if self._cancelled:
                self.cancelled.emit()
            else:
                self.finished_session.emit(session)
        except Exception as exc:
            if self._cancelled:
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))


class RenderWorker(QThread):
    log = Signal(str)
    progress = Signal(int, int)
    finished_render = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        service: GuiEngineService,
        session: EngineSessionModel,
        settings: GuiSettings,
    ) -> None:
        super().__init__()
        self._service = service
        self._session = session
        self._settings = settings
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            artifacts: RenderArtifactsModel = self._service.render(
                session=self._session,
                settings=self._settings,
                on_log=self.log.emit,
                on_progress=self.progress.emit,
                should_cancel=lambda: self._cancelled,
            )
            if self._cancelled:
                self.cancelled.emit()
            else:
                self.finished_render.emit(artifacts)
        except Exception as exc:
            if self._cancelled:
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))


class Mp3SplitWorker(QThread):
    log = Signal(str)
    progress = Signal(int, int)
    finished_paths = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        service: MediaToolsService,
        input_path: Path,
        output_dir: Path,
        chunk_minutes: int,
        bitrate: str,
    ) -> None:
        super().__init__()
        self._service = service
        self._input_path = input_path
        self._output_dir = output_dir
        self._chunk_minutes = chunk_minutes
        self._bitrate = bitrate
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            paths = self._service.split_mp3(
                input_path=self._input_path,
                output_dir=self._output_dir,
                chunk_minutes=self._chunk_minutes,
                bitrate=self._bitrate,
                on_log=self.log.emit,
                on_progress=self.progress.emit,
                should_cancel=lambda: self._cancelled,
            )
            if self._cancelled:
                self.cancelled.emit()
            else:
                self.finished_paths.emit(paths)
        except Exception as exc:
            if self._cancelled:
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))


class Mp4StitchWorker(QThread):
    log = Signal(str)
    progress = Signal(int, int)
    finished_path = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        service: MediaToolsService,
        folder: Path,
        output_path: Path,
        smart_ordering: bool,
        smart_fade: bool,
        crossfade_sec: float,
        input_files: Optional[list[Path]] = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._folder = folder
        self._output_path = output_path
        self._smart_ordering = smart_ordering
        self._smart_fade = smart_fade
        self._crossfade_sec = crossfade_sec
        self._input_files = input_files
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            path = self._service.stitch_mp4(
                folder=self._folder,
                output_path=self._output_path,
                smart_ordering=self._smart_ordering,
                smart_fade=self._smart_fade,
                base_crossfade_sec=self._crossfade_sec,
                input_files=self._input_files,
                on_log=self.log.emit,
                on_progress=self.progress.emit,
                should_cancel=lambda: self._cancelled,
            )
            if self._cancelled:
                self.cancelled.emit()
            else:
                self.finished_path.emit(path)
        except Exception as exc:
            if self._cancelled:
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))
