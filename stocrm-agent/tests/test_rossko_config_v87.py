"""Настройка работает локально, без запросов в CRM и Росско."""
import errno

import pytest

from stocrm_agent import rossko_config
from stocrm_agent.envfile import _parse


def test_quoted_and_non_alphanumeric_keys_roundtrip(tmp_path):
    path = tmp_path / '.env'
    path.write_text('OTHER=keep\n', encoding='utf-8')
    key1, key2 = 'abc+/# token', 'key.with"quotes'
    rossko_config.configure_file(path, key1, key2)
    values = _parse(path.read_text(encoding='utf-8'))
    assert values['ROSSKO_KEY1'] == key1 and values['ROSSKO_KEY2'] == key2
    assert values['OTHER'] == 'keep' and values['ROSSKO_ENABLED'] == '1'
    assert values['ROSSKO_DELIVERY_ID'] == '' and values['ROSSKO_ADDRESS_ID'] == ''


def test_unsupported_key_is_rejected_without_exposing_it(tmp_path, capsys):
    path = tmp_path / '.env'
    initial = 'STOCRM_SID=keep\n'
    path.write_text(initial, encoding='utf-8')
    inputs = iter(['bad\nkey', 'validkey'])
    assert rossko_config.run(path=path, ask=lambda _: next(inputs)) == 1
    assert path.read_text(encoding='utf-8') == initial
    output = capsys.readouterr().out
    assert 'непечатаемые символы' in output and 'bad\nkey' not in output


def test_permissions_error_is_useful_without_exposing_data(tmp_path, monkeypatch, capsys):
    path = tmp_path / '.env'
    initial = 'STOCRM_SID=keep\n'
    path.write_text(initial, encoding='utf-8')
    inputs = iter(['private-key1', 'private-key2'])
    def denied(*args):
        raise PermissionError(errno.EACCES, 'private-key1')
    monkeypatch.setattr(rossko_config.os, 'replace', denied)
    assert rossko_config.run(path=path, ask=lambda _: next(inputs)) == 1
    assert path.read_text(encoding='utf-8') == initial
    assert not list(tmp_path.glob('.rossko-env-*'))
    output = capsys.readouterr().out
    assert 'Нет доступа' in output and 'private-key1' not in output and 'private-key2' not in output


def test_interrupted_input_does_not_change_env(tmp_path, capsys):
    path = tmp_path / '.env'
    initial = 'STOCRM_SID=keep\n'
    path.write_text(initial, encoding='utf-8')
    inputs = iter(['private-key1'])
    def ask(_):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError from None
    assert rossko_config.run(path=path, ask=ask) == 1
    assert path.read_text(encoding='utf-8') == initial
    assert 'Ввод прерван' in capsys.readouterr().out


def test_missing_env_is_explained(tmp_path, capsys):
    assert rossko_config.run(path=tmp_path / '.env', ask=lambda _: pytest.fail('unexpected input')) == 1
    assert 'Файл .env не найден' in capsys.readouterr().out


def test_system_error_code_does_not_expose_path_or_key(tmp_path, monkeypatch, capsys):
    path = tmp_path / '.env'
    path.write_text('OTHER=keep\n', encoding='utf-8')
    inputs = iter(['private-key1', 'private-key2'])
    def fail(*args):
        raise OSError(errno.ENOSPC, 'private-key1')
    monkeypatch.setattr(rossko_config.os, 'replace', fail)
    assert rossko_config.run(path=path, ask=lambda _: next(inputs)) == 1
    output = capsys.readouterr().out
    assert f'Код ошибки системы: {errno.ENOSPC}' in output
    assert 'private-key1' not in output and 'private-key2' not in output
    assert path.read_text(encoding='utf-8') == 'OTHER=keep\n'


def test_local_setup_never_uses_network(tmp_path, monkeypatch):
    import socket
    path = tmp_path / '.env'
    path.write_text('STOCRM_SID=keep\n', encoding='utf-8')
    inputs = iter(['key-one', 'key-two'])
    def forbidden(*args, **kwargs):
        raise AssertionError('Настройка не должна использовать сеть')
    monkeypatch.setattr(socket, 'socket', forbidden)
    assert rossko_config.run(path=path, ask=lambda _: next(inputs)) == 0
    assert _parse(path.read_text(encoding='utf-8'))['ROSSKO_KEY2'] == 'key-two'


def test_concurrent_env_change_is_not_overwritten(tmp_path, monkeypatch, capsys):
    path = tmp_path / '.env'
    path.write_text('STOCRM_SID=keep\n', encoding='utf-8')
    original_read = type(path).read_bytes
    reads = 0
    def modified(self):
        nonlocal reads
        reads += 1
        if reads == 2:
            return b'STOCRM_SID=changed\n'
        return original_read(self)
    monkeypatch.setattr(type(path), 'read_bytes', modified)
    inputs = iter(['key-one', 'key-two'])
    assert rossko_config.run(path=path, ask=lambda _: next(inputs)) == 1
    assert 'изменился во время настройки' in capsys.readouterr().out
    assert original_read(path) == b'STOCRM_SID=keep\n'
    assert not list(tmp_path.glob('.rossko-env-*'))


def test_unrelated_bytes_bom_and_windows_line_endings_preserved(tmp_path):
    path = tmp_path / '.env'
    path.write_bytes(b'\xef\xbb\xbfSTOCRM_SID=keep\r\n# user comment\r\nROSSKO_KEY1=old\r\nOTHER=last')
    rossko_config.configure_file(path, 'new-key1', 'new-key2')
    data = path.read_bytes()
    assert data.startswith(b'\xef\xbb\xbfSTOCRM_SID=keep\r\n# user comment\r\nROSSKO_KEY1=new-key1\r\nOTHER=last\r\n')
    assert b'\n' not in data.replace(b'\r\n', b'')
    assert _parse(data.decode('utf-8-sig'))['ROSSKO_KEY2'] == 'new-key2'
