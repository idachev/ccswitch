"""macOS Security.framework access; secrets never appear in subprocess argv."""
from __future__ import annotations

import ctypes as ct
from functools import lru_cache
import sys


class KeychainError(RuntimeError):
    """Keychain error containing status codes, never secret values."""


class SecurityFramework:
    NOT_FOUND = -25300
    DUPLICATE = -25299

    def __init__(self):
        if sys.platform != "darwin":
            raise KeychainError("ccswitch requires macOS Keychain.")
        self.lib = ct.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.cf = ct.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        pointer, uint = ct.c_void_p, ct.c_uint32
        signatures = {
            "SecKeychainFindGenericPassword": [pointer, uint, ct.c_char_p, uint, ct.c_char_p,
                                               ct.POINTER(uint), ct.POINTER(pointer), ct.POINTER(pointer)],
            "SecKeychainAddGenericPassword": [pointer, uint, ct.c_char_p, uint, ct.c_char_p,
                                              uint, ct.c_char_p, ct.POINTER(pointer)],
            "SecKeychainItemModifyAttributesAndData": [pointer, pointer, uint, ct.c_char_p],
            "SecKeychainItemFreeContent": [pointer, pointer],
            "SecKeychainItemDelete": [pointer],
        }
        for name, args in signatures.items():
            fn = getattr(self.lib, name)
            fn.argtypes, fn.restype = args, ct.c_int32
        self.cf.CFRelease.argtypes = [pointer]
        self.cf.CFRelease.restype = None

    @staticmethod
    def check(status, operation):
        if status:
            raise KeychainError(f"Keychain {operation} failed (OSStatus {status}).")

    def find(self, service, account):
        svc, acc = service.encode(), account.encode()
        length, data, item = ct.c_uint32(), ct.c_void_p(), ct.c_void_p()
        status = self.lib.SecKeychainFindGenericPassword(
            None, len(svc), svc, len(acc), acc, ct.byref(length), ct.byref(data), ct.byref(item)
        )
        if status == self.NOT_FOUND:
            return None, item
        self.check(status, "read")
        try:
            value = ct.string_at(data, length.value).decode("utf-8")
        except Exception:
            if item:
                self.cf.CFRelease(item)
            raise KeychainError("Keychain entry is not UTF-8.") from None
        finally:
            self.lib.SecKeychainItemFreeContent(None, data)
        return value, item

    def read(self, service, account):
        value, item = self.find(service, account)
        if item:
            self.cf.CFRelease(item)
        return value

    def write(self, service, account, value):
        data = value.encode()
        for _ in range(2):
            _, item = self.find(service, account)
            if item:
                try:
                    self.check(self.lib.SecKeychainItemModifyAttributesAndData(item, None, len(data), data), "write")
                finally:
                    self.cf.CFRelease(item)
                return
            svc, acc = service.encode(), account.encode()
            status = self.lib.SecKeychainAddGenericPassword(None, len(svc), svc, len(acc), acc, len(data), data, None)
            if status != self.DUPLICATE:
                self.check(status, "create")
                return
        raise KeychainError("Keychain entry changed concurrently; retry.")

    def delete(self, service, account):
        _, item = self.find(service, account)
        if item:
            try:
                self.check(self.lib.SecKeychainItemDelete(item), "delete")
            finally:
                self.cf.CFRelease(item)


@lru_cache(maxsize=1)
def _backend():
    return SecurityFramework()


def read(service: str, account: str) -> str | None:
    return _backend().read(service, account)


def write(service: str, account: str, value: str) -> None:
    _backend().write(service, account, value)


def delete(service: str, account: str) -> None:
    _backend().delete(service, account)
