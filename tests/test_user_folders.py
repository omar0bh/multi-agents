from pathlib import Path
from app.tools.user_folders import (
    get_known_user_folders,
    get_user_folder,
    resolve_user_destination,
)


def test_get_known_user_folders():
    folders = get_known_user_folders()
    assert "desktop" in folders
    assert "videos" in folders
    assert "pictures" in folders
    assert "documents" in folders
    assert "downloads" in folders
    assert "music" in folders
    for k, v in folders.items():
        assert isinstance(v, Path)


def test_resolve_destinations():
    # Video matching
    path_vid, cat_vid = resolve_user_destination("vedio")
    assert cat_vid == "videos"

    # Picture matching
    path_pic, cat_pic = resolve_user_destination("pictures")
    assert cat_pic == "pictures"

    # Documents matching
    path_doc, cat_doc = resolve_user_destination("documents")
    assert cat_doc == "documents"

    # Desktop matching
    path_desk, cat_desk = resolve_user_destination("desktop")
    assert cat_desk == "desktop"

    # Subfolder resolution
    path_sub, cat_sub = resolve_user_destination("dossier smito test_folder f documents")
    assert cat_sub == "documents"
    assert path_sub.name == "test_folder"
