"""Atomic replacement of local settings that may contain credentials."""

import os
import sys
from pathlib import Path
from uuid import uuid4


def _create_private_file(path: Path) -> int:
    if sys.platform == "win32":
        import msvcrt

        import pywintypes
        import win32api
        import win32con
        import win32file
        import win32security

        # Supply the ACL at creation, even when the client config directory is shared.
        try:
            token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
            user, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
            security = win32security.SECURITY_ATTRIBUTES()
            sid = win32security.ConvertSidToStringSid(user)
            security.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                f"D:P(A;;FA;;;{sid})", win32security.SDDL_REVISION_1
            )
            handle = win32file.CreateFile(
                str(path), win32con.GENERIC_WRITE, 0, security,
                win32con.CREATE_NEW, win32con.FILE_ATTRIBUTE_NORMAL, None,
            )
        except pywintypes.error as exc:
            raise OSError(None, exc.strerror, str(path), exc.winerror) from exc
        try:
            descriptor = msvcrt.open_osfhandle(int(handle), os.O_WRONLY | os.O_BINARY)
        except OSError:
            handle.Close()
            path.unlink(missing_ok=True)
            raise
        handle.Detach()  # The file descriptor now owns the handle.
        return descriptor
    return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)


def write_private_text(path: Path, content: str, *, restrict_parent: bool = False) -> None:
    """Publish a complete private UTF-8 file or leave the existing file intact."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if restrict_parent and os.name != "nt":
        path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = _create_private_file(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
