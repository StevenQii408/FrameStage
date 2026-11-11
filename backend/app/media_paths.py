"""Keep SQLite media references portable while file consumers use checked paths."""
from pathlib import Path, PureWindowsPath
import sqlite3


class MediaPaths:
    path_columns = frozenset(("video_path", "path", "thumbnail_path", "original_path"))
    managed_dirs = frozenset(("jobs", "style", "generation-assets"))

    def __init__(self, root):
        self.root = Path(root).resolve()

    def _checked(self, value, *, legacy):
        path = Path(value)
        windows = PureWindowsPath(value)
        if ".." in path.parts or ".." in windows.parts or "\x00" in str(value):
            raise ValueError("媒体路径无效")
        if path.is_relative_to(self.root):
            relative = path.relative_to(self.root)
        elif path.is_absolute() or windows.drive:
            # Old releases stored absolute paths. Rebase only an app-owned
            # subtree, never dereference the old absolute path on this machine.
            parts = windows.parts if windows.drive else path.parts
            start = max((i for i, part in enumerate(parts[:-1]) if part in self.managed_dirs), default=None)
            if not legacy or start is None:
                raise ValueError("媒体路径超出数据目录")
            relative = Path(*parts[start:])
        else:
            relative = path
        resolved = (self.root / relative).resolve()
        if self.root not in resolved.parents:
            raise ValueError("媒体路径超出数据目录")
        return resolved

    def store(self, value):
        if not value:
            return value
        return self._checked(value, legacy=False).relative_to(self.root).as_posix()

    def open_path(self, value):
        if not value:
            return value
        try:
            return str(self._checked(value, legacy=True))
        except (TypeError, ValueError, OSError, RuntimeError):
            # A legacy row can be inspected even after its artifact disappeared.
            # File consumers treat an empty path as missing.
            return ""

    def row(self, cursor, values):
        return sqlite3.Row(cursor, tuple(
            self.open_path(value) if column[0] in self.path_columns else value
            for column, value in zip(cursor.description, values)
        ))

    def record(self, record, *, reading=False):
        convert = self.open_path if reading else self.store
        return {key: convert(value) if key in self.path_columns else value
                for key, value in record.items()}

    def frame_tree(self, value, *, reading=False):
        """Event results and tool checkpoints can both contain frame records."""
        if isinstance(value, list):
            return [self.frame_tree(item, reading=reading) for item in value]
        if isinstance(value, dict):
            if {"id", "shot_id", "timestamp", "path"} <= value.keys():
                return self.record(value, reading=reading)
            return {key: self.frame_tree(item, reading=reading) for key, item in value.items()}
        return value

    def snapshot(self, snapshot, *, reading=False):
        return {**snapshot, "job": self.record(snapshot["job"], reading=reading),
                "frames": [self.record(frame, reading=reading) for frame in snapshot["frames"]]}

