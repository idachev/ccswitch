"""FFI adapter tests use synthetic bytes; never call the real Keychain."""
import ctypes as ct
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ccswitch import keychain as mod


@pytest.fixture
def backend():
    obj = mod.SecurityFramework.__new__(mod.SecurityFramework)
    obj.cf = SimpleNamespace(CFRelease=Mock())
    obj.lib = SimpleNamespace(SecKeychainItemFreeContent=Mock(),
                              SecKeychainItemModifyAttributesAndData=Mock(return_value=0),
                              SecKeychainAddGenericPassword=Mock(return_value=0),
                              SecKeychainItemDelete=Mock(return_value=0))
    return obj


def test_read_copies_exact_utf8_and_frees_native_buffers(backend):
    value = 'synthetic\nsecret-✓\x00tail'
    data = value.encode()
    buffer = ct.create_string_buffer(data)
    def find(_, slen, service, alen, account, length_ptr, data_ptr, item_ptr):
        assert service == b"service" and account == b"account"
        ct.cast(length_ptr, ct.POINTER(ct.c_uint32))[0] = len(data)
        ct.cast(data_ptr, ct.POINTER(ct.c_void_p))[0] = ct.cast(buffer, ct.c_void_p)
        ct.cast(item_ptr, ct.POINTER(ct.c_void_p))[0] = ct.c_void_p(123)
        return 0
    backend.lib.SecKeychainFindGenericPassword = find
    assert backend.read("service", "account") == value
    backend.lib.SecKeychainItemFreeContent.assert_called_once()
    backend.cf.CFRelease.assert_called_once()


def test_read_not_found_returns_none(backend):
    backend.lib.SecKeychainFindGenericPassword = Mock(return_value=-25300)
    assert backend.read("service", "account") is None


def test_read_error_contains_only_operation_and_status(backend):
    backend.lib.SecKeychainFindGenericPassword = Mock(return_value=-25293)
    with pytest.raises(mod.KeychainError, match="OSStatus -25293"):
        backend.read("service", "account")


def test_write_updates_existing_item_without_delete(backend):
    backend.find = Mock(return_value=("old-synthetic", ct.c_void_p(123)))
    backend.write("service", "account", "new-synthetic")
    assert backend.lib.SecKeychainItemModifyAttributesAndData.call_args.args[-1] == b"new-synthetic"
    backend.lib.SecKeychainItemDelete.assert_not_called()
    backend.lib.SecKeychainAddGenericPassword.assert_not_called()
    backend.cf.CFRelease.assert_called_once()


def test_write_creation_race_retries_as_update(backend):
    backend.find = Mock(side_effect=[(None, ct.c_void_p()), ("created", ct.c_void_p(123))])
    backend.lib.SecKeychainAddGenericPassword.return_value = -25299
    backend.write("service", "account", "synthetic")
    backend.lib.SecKeychainItemModifyAttributesAndData.assert_called_once()


def test_failed_update_releases_item_and_does_not_leak_secret(backend):
    backend.find = Mock(return_value=("old", ct.c_void_p(123)))
    backend.lib.SecKeychainItemModifyAttributesAndData.return_value = -25293
    with pytest.raises(mod.KeychainError) as error:
        backend.write("service", "account", "synthetic-secret")
    assert "synthetic-secret" not in str(error.value)
    backend.cf.CFRelease.assert_called_once()


def test_delete_missing_is_noop(backend):
    backend.find = Mock(return_value=(None, ct.c_void_p()))
    backend.delete("service", "account")
    backend.lib.SecKeychainItemDelete.assert_not_called()


def test_public_api_delegates_in_process(backend, monkeypatch):
    backend.read = Mock(return_value="synthetic")
    backend.write = Mock()
    backend.delete = Mock()
    monkeypatch.setattr(mod, "_backend", lambda: backend)
    assert mod.read("s", "a") == "synthetic"
    mod.write("s", "a", "value")
    mod.delete("s", "a")
    backend.write.assert_called_once_with("s", "a", "value")
    backend.delete.assert_called_once_with("s", "a")
