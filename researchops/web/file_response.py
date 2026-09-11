"""Descriptor-backed downloads of immutable archived logs."""

from researchops.workspace.security import open_safe_file
from researchops.errors import WorkspaceError


class StreamingFileBody:
    def __init__(self, path, root):
        # A stored archive's limit may predate today's runner configuration.
        # Freeze the safe opened inode's size instead of reusing an import cap.
        self._context = open_safe_file(path, root, path.stat().st_size)
        self.stream, self.size = self._context.__enter__()
        self.closed = False

    def __len__(self):
        return self.size

    def chunks(self):
        remaining = self.size
        while remaining:
            chunk = self.stream.read(min(65536, remaining))
            if not chunk:
                raise WorkspaceError("Archive download ended before its recorded size")
            remaining -= len(chunk)
            yield chunk

    def close(self):
        if not self.closed:
            self.closed = True
            self._context.__exit__(None, None, None)
