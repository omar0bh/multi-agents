"""Comprehensive Test Suite for all PC local operations and natural language intent parsing."""

import asyncio
import os
import shutil
from pathlib import Path
import pytest

from app.tools.user_folders import get_known_user_folders
from scripts.local_bridge import execute_local_task
from app.tools.intent_parser import parse_file_operation_intent


@pytest.fixture(scope="module")
def desktop_dir():
    known = get_known_user_folders()
    return known["desktop"]


def test_01_create_folder(desktop_dir):
    """Test creating a folder on Desktop and a subfolder inside it."""
    res = execute_local_task("create_folder", {"folder": "_test_suite_box", "parent": "desktop"})
    assert res["ok"] is True
    target = desktop_dir / "_test_suite_box"
    assert target.is_dir()

    # Create subfolder inside it
    res_sub = execute_local_task("create_folder", {"folder": "sub_box", "parent": "_test_suite_box"})
    assert res_sub["ok"] is True
    assert (target / "sub_box").is_dir()


def test_02_create_and_read_file(desktop_dir):
    """Test creating a file with content and reading it back."""
    res_create = execute_local_task(
        "create_file",
        {
            "filename": "hello.txt",
            "folder": "_test_suite_box",
            "content": "Salam Omar! Test automated file content.",
        }
    )
    assert res_create["ok"] is True
    file_path = desktop_dir / "_test_suite_box" / "hello.txt"
    assert file_path.is_file()

    # Read file
    res_read = execute_local_task("read_file", {"query": "_test_suite_box/hello.txt"})
    assert res_read["ok"] is True
    assert "Salam Omar!" in res_read["preview"]


def test_03_copy_file(desktop_dir):
    """Test copying a file into another folder."""
    res = execute_local_task(
        "copy_file",
        {
            "src": "hello.txt",
            "src_parent": "_test_suite_box",
            "dest_folder": "_test_suite_box/sub_box",
        }
    )
    assert res["ok"] is True
    assert (desktop_dir / "_test_suite_box" / "sub_box" / "hello.txt").is_file()


def test_04_copy_folder(desktop_dir):
    """Test copying an entire directory with nested files."""
    res = execute_local_task(
        "copy_file",
        {
            "src": "sub_box",
            "src_parent": "_test_suite_box",
            "dest_folder": "_test_suite_box/copied_box",
        }
    )
    assert res["ok"] is True
    assert (desktop_dir / "_test_suite_box" / "copied_box" / "sub_box").is_dir()
    assert (desktop_dir / "_test_suite_box" / "copied_box" / "sub_box" / "hello.txt").is_file()


def test_05_move_and_rename(desktop_dir):
    """Test moving/renaming files and folders."""
    res = execute_local_task(
        "move_file",
        {
            "src": "_test_suite_box/hello.txt",
            "dest": "renamed_hello.txt",
        }
    )
    assert res["ok"] is True
    assert (desktop_dir / "_test_suite_box" / "renamed_hello.txt").is_file()
    assert not (desktop_dir / "_test_suite_box" / "hello.txt").exists()


def test_06_list_directory(desktop_dir):
    """Test listing directory contents."""
    res = execute_local_task("list_dir", {"folder": "_test_suite_box"})
    assert res["ok"] is True
    assert any("renamed_hello.txt" in item for item in res["items"])
    assert any("sub_box" in item for item in res["items"])


def test_07_delete_path(desktop_dir):
    """Test deleting files and folders cleanly."""
    # Delete file
    res_del_f = execute_local_task("delete_path", {"path": "_test_suite_box/renamed_hello.txt"})
    assert res_del_f["ok"] is True
    assert not (desktop_dir / "_test_suite_box" / "renamed_hello.txt").exists()

    # Delete main folder
    res_del_d = execute_local_task("delete_path", {"path": "_test_suite_box"})
    assert res_del_d["ok"] is True
    assert not (desktop_dir / "_test_suite_box").exists()


def test_08_natural_language_intent_parsing():
    """Test intent parsing across Moroccan Darija, English, Arabic, and French."""
    test_cases = [
        ("o bghitk tmchi l desktop o copy folder smito new l chess", "copy_file"),
        ("sawb li dossier smito projects f desktop", "create_folder"),
        ("dir file smito notes.txt o kteb fih hello", "create_file"),
        ("ok mse7 db dak intelgant", "delete_path"),
        ("9ra li chno kyn f notes.txt", "read_file"),
        ("move file notes.txt to documents", "move_file"),
        ("وريني شنو كاين ف سطح المكتب", "list_dir"),
    ]

    for prompt, expected_action in test_cases:
        intent = asyncio.run(parse_file_operation_intent(prompt))
        assert intent["is_file_op"] is True, f"Failed detection on: {prompt}"
        assert intent["action"] == expected_action, f"Expected {expected_action} but got {intent['action']} on '{prompt}'"
