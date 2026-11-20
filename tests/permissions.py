"""Assertions for private state on POSIX filesystems and Windows NTFS."""

import stat
import sys
from pathlib import Path


def assert_private_permissions(path: Path, mode: int) -> None:
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == mode
        return

    import win32api
    import win32con
    import win32security as security

    token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    user, _ = security.GetTokenInformation(token, security.TokenUser)
    user_sid = security.ConvertSidToStringSid(user)
    descriptor = security.GetNamedSecurityInfo(
        str(path), security.SE_FILE_OBJECT,
        security.DACL_SECURITY_INFORMATION | security.OWNER_SECURITY_INFORMATION,
    )
    # Python's Windows mode 0700 grants the owner, SYSTEM and administrators access.
    owner = security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
    assert owner in {user_sid, "S-1-5-32-544"}
    allowed = {user_sid, "S-1-3-4", "S-1-5-18", "S-1-5-32-544"}
    acl = descriptor.GetSecurityDescriptorDacl()
    assert acl is not None and acl.GetAceCount() > 0
    for index in range(acl.GetAceCount()):
        (kind, _flags), _access, sid = acl.GetAce(index)
        assert kind == security.ACCESS_ALLOWED_ACE_TYPE
        assert security.ConvertSidToStringSid(sid) in allowed
