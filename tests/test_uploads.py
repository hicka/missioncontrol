import uploads


def test_safe_name_strips_hostile_paths():
    assert "/" not in uploads.safe_name("../../etc/passwd")
    assert uploads.safe_name("") == "file"
    assert uploads.safe_name("report final (2).pdf").endswith(".pdf")


def test_save_and_kind_and_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path)
    p = uploads.save(b"name,qty\napples,4\n", "stock.csv")
    assert p.exists() and p.name.endswith("stock.csv")
    assert uploads.kind_of(p) == "text"
    assert "apples" in uploads.preview(p)

    img = uploads.save(b"\x89PNG fake", "photo.PNG")
    assert uploads.kind_of(img) == "image"
    binary = uploads.save(b"\x00\x01", "blob.zip")
    assert uploads.kind_of(binary) == "binary"
    assert uploads.preview(binary) == ""
