"""1.1.5 fixes folded into 1.1: files, deleting, projects, reference pages,
log entries, history and undo."""

import json
import re
import textwrap
from io import BytesIO

import pytest

import fixture
from test_app import ROOT, client
from test_storage import first_run, run_script, git
from test_edit import remote, other_push  # noqa: F401  (fixture)

from app import edit, main, repo

JSON = {"Accept": "application/json"}


def readme(rel):
    return (ROOT / rel / "README.md").read_text()


def post(url, **data):
    r = client.post(url, data=data, headers=JSON)
    return r.status_code, r.json()


# --------------------------------------------------------------------------- Markdown helpers

def test_change_and_remove_log_entries():
    text = edit.add_log_entry(fixture.TABLE_SAW, "2027-02-01", "New belt")
    assert edit.log_rows(text) == [["2027-01-03", "Waxed the top."], ["2027-02-01", "New belt"]]
    moved = edit.change_log_entry(text, ("2027-01-03", "Waxed the top."), ("2027-03-01", "Waxed it."))
    assert edit.log_rows(moved) == [["2027-02-01", "New belt"], ["2027-03-01", "Waxed it."]]
    gone = edit.change_log_entry(text, ("2027-02-01", "New belt"), None)
    assert edit.log_rows(gone) == [["2027-01-03", "Waxed the top."]]
    with pytest.raises(edit.Conflict):
        edit.change_log_entry(text, ("2027-02-01", "Changed elsewhere"), None)


def test_remove_inventory_row_refreshes_total():
    text = edit.remove_inventory_row(fixture.INVENTORY, "acme-dd20")
    rows, _, total = repo.parse_inventory_table(text)
    assert "acme-dd20" not in rows and total.startswith("~$300")


def test_project_fields_and_tools():
    text = edit.set_project_field(fixture.PROJECT, "Status", "in progress")
    text = edit.set_project_field(text, "Finished", "2027-03")
    assert edit.get_project_field(text, "Status") == "in progress"
    assert edit.get_project_field(text, "Finished") == "2027-03"
    text = edit.set_project_tools(text, [("acme-dd20", "Acme DD20"), ("acme-ts10", "Acme TS10")])
    assert edit.project_tools(text) == ["acme-dd20", "acme-ts10"]
    assert "## Goal" in text and "A simple bookshelf." in text
    new = edit.new_project_readme("Shed | door", "planning", "2027-04", "Fix it", [])
    assert new.startswith("# Shed \\| door\n") and "Started: 2027-04" in new


def test_photos_are_shrunk_unless_small_jpegs():
    from PIL import Image
    big = BytesIO()
    Image.new("RGB", (4032, 3024), "red").save(big, "PNG")
    big.seek(0)
    data, name = edit.shrink_photo(big, "IMG_1.png")
    assert name == "IMG_1.jpg"
    assert max(Image.open(BytesIO(data)).size) == edit.MAX_PHOTO_EDGE
    small = BytesIO(fixture._jpeg())
    assert edit.shrink_photo(small, "x.jpg") is None


def test_crop_box_keeps_the_chosen_square_inside_the_photo():
    assert main.crop_box((400, 300), repo.DEFAULT_CROP) == (50, 0, 350, 300)
    assert main.crop_box((400, 300), (0.0, 0.5, 1.0)) == (0, 0, 300, 300)  # pushed back inside
    assert main.crop_box((400, 300), (0.75, 0.25, 2.0)) == (225, 0, 375, 150)
    assert repo.clean_crop(["9", "-1", "50"]) == (1.0, 0.0, repo.MAX_ZOOM)
    assert repo.clean_crop(["nan", "0.5", "1"]) == repo.DEFAULT_CROP
    assert repo.clean_crop([None, None, None]) == repo.DEFAULT_CROP


# --------------------------------------------------------------------------- routes (plain folder)

def test_edit_and_delete_log_entry():
    t = repo.load(ROOT).tool("acme-ts10")
    n = next(i for i, e in enumerate(t.log) if e.what == "Waxed the top.")
    page = client.get(f"/tools/acme-ts10/log/{n}").text
    assert 'name="old_what" value="Waxed the top."' in page and "Delete Entry" in page
    assert post(f"/tools/acme-ts10/log/{n}", old_date="2027-01-03", old_what="Waxed the top.",
                date="2027-01-03", what="Waxed and buffed the top.")[1]["ok"]
    assert "Waxed and buffed the top." in readme("tools/acme-ts10")
    # A stale form (the row changed) doesn't touch anything.
    code, body = post(f"/tools/acme-ts10/log/{n}", old_date="2027-01-03", old_what="Waxed the top.",
                      action="delete")
    assert code == 409
    assert post(f"/tools/acme-ts10/log/{n}", old_date="2027-01-03", old_what="Waxed and buffed the top.",
                action="delete")[1]["ok"]
    assert "Waxed and buffed" not in readme("tools/acme-ts10")
    assert client.get("/tools/acme-ts10/log/999").status_code == 404


def test_manage_files_rename_retype_cover_delete():
    folder = ROOT / "tools/acme-ts10"
    (folder / "README.md").write_text(readme("tools/acme-ts10") + "\nSee [the manual](manual.pdf).\n")
    (folder / "side.jpg").write_bytes(fixture._jpeg())
    main.inventory.invalidate()
    assert "side.jpg" in client.get("/tools/acme-ts10/files").text
    # Rename keeps the extension and fixes README links.
    assert post("/tools/acme-ts10/files", action="rename", name="manual.pdf", new_name="Owners Manual",
                old_kind="manual", kind="manual")[1]["ok"]
    assert (folder / "owners-manual.pdf").exists() and "](owners-manual.pdf)" in readme("tools/acme-ts10")
    # Changing the type renames the file so the app files it there.
    assert post("/tools/acme-ts10/files", action="rename", name="side.jpg", new_name="side",
                old_kind="photo", kind="receipt")[1]["ok"]
    t = repo.load(ROOT).tool("acme-ts10")
    assert any(f.name.startswith("receipt") and f.name.endswith(".jpg") for f in t.proofs)
    # Make cover: the old cover keeps its place in the gallery.
    (folder / "front.jpg").write_bytes(fixture._jpeg())
    main.inventory.invalidate()
    assert post("/tools/acme-ts10/files", action="cover", name="front.jpg")[1]["ok"]
    t = repo.load(ROOT).tool("acme-ts10")
    assert t.cover.name == "photo.jpg" and len(t.photos) == 2
    # Delete; and nothing outside the folder can be named.
    victim = next(f.name for f in t.photos if f.name != "photo.jpg")
    assert post("/tools/acme-ts10/files", action="delete", name=victim)[1]["ok"]
    assert not (folder / victim).exists()
    for bad in ("../../README.md", "README.md", ".git"):
        assert post("/tools/acme-ts10/files", action="delete", name=bad)[0] == 400


def test_projects_new_edit_files_delete():
    code, body = post("/projects/new", title="Garage shelves", status="planning", started="2027-05",
                      goal="Store bins.", tools="acme-ts10")
    assert body["ok"] and body["url"] == "/projects/2027-05-garage-shelves"
    p = repo.load(ROOT).project("2027-05-garage-shelves")
    assert p.goal == "Store bins." and p.tools == ["acme-ts10"]
    assert "Garage shelves" in client.get("/tools/acme-ts10").text  # shows under Used In

    html = client.get(f"{p.url}/edit").text
    base = re.search(r'name="base" value="([^"]*)"', html).group(1).replace("&#34;", '"').replace("&quot;", '"')
    r = client.post(f"{p.url}/edit", headers=JSON, data={
        "base": base, "title": "Garage shelves", "status": "done", "started": "2027-05",
        "finished": "2027-06", "tools": ["acme-ts10", "acme-dd20"]})
    assert r.json()["ok"], r.text
    p = repo.load(ROOT).project("2027-05-garage-shelves")
    assert p.status == "done" and p.finished == "2027-06" and p.tools == ["acme-dd20", "acme-ts10"]

    r = client.post(f"{p.url}/add", headers=JSON, data={},
                    files=[("file", ("Cut List.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf"))])
    assert r.json()["ok"] and (ROOT / "projects/2027-05-garage-shelves/cut-list.pdf").exists()
    assert client.get(f"/edit/projects/{p.slug}").status_code == 200

    assert post(f"{p.url}/delete", confirm="wrong")[0] == 400
    assert post(f"{p.url}/delete", confirm=p.slug)[1]["ok"]
    assert not (ROOT / "projects" / p.slug).exists()


def test_delete_tool_removes_folder_and_inventory_row():
    assert post("/tools/new", kind="Sander", brand="Acme", model_no="DEL1", value="50")[1]["ok"]
    assert "acme-del1" in (ROOT / "README.md").read_text()
    assert post("/tools/acme-del1/delete", confirm="acme-del1")[1]["ok"]
    assert not (ROOT / "tools/acme-del1").exists()
    assert "acme-del1" not in (ROOT / "README.md").read_text()


def test_reference_pages():
    code, body = post("/reference/new", title="Finishing schedule", text="- Sand 120\n- Sand 180")
    assert body["ok"] and body["url"] == "/reference/finishing-schedule"
    assert "Sand 180" in client.get("/reference/finishing-schedule").text
    html = client.get("/edit/reference/finishing-schedule").text
    base = re.search(r'name="base" value="([^"]*)"', html).group(1)
    assert post("/edit/reference/finishing-schedule", base=base, text="# Finishing\n\nOil.\n")[1]["ok"]
    assert (ROOT / "reference/finishing-schedule.md").read_text() == "# Finishing\n\nOil.\n"
    assert post("/edit/reference/finishing-schedule", action="delete")[1]["ok"]
    assert not (ROOT / "reference/finishing-schedule.md").exists()
    assert client.get("/edit/reference/index").status_code == 200
    assert "Delete Page" not in client.get("/edit/reference/index").text
    assert client.get("/edit/reference/nope").status_code == 404


def test_history_explains_plain_folders():
    assert "Keep History on This Server" in client.get("/history").text
    assert client.post("/undo", data={"sha": "0" * 40}, headers=JSON).status_code == 400


def test_old_readme_editor_url_redirects():
    r = client.get("/tools/acme-ts10/markdown", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/edit/tools/acme-ts10"


# --------------------------------------------------------------------------- undo with history

UNDO = textwrap.dedent("""
    import os, sys, json, re, html
    sys.path.insert(0, os.environ["APP_ROOT"])
    from starlette.testclient import TestClient
    from app import main
    J = {"Accept": "application/json"}
    out = {}
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "admin", "password": "correct horse battery"})
        form = json.loads(os.environ["FORM"])
        out["setup"] = c.post("/setup/storage", data=form, follow_redirects=False).status_code
        import time
        for _ in range(100):
            if main.sync.head:
                break
            time.sleep(0.1)
        main.inventory.invalidate()
        slug = [t.slug for t in main.inventory.get().tools][0]
        c.post(f"/tools/{slug}/log", data={"date": "2027-09-09", "what": "Oiled it"}, headers=J)
        c.post(f"/tools/{slug}/log", data={"date": "2027-09-10", "what": "Sharpened it"}, headers=J)
        page = c.get(f"/tools/{slug}/history").text
        out["listed"] = "Oiled it" in page and "Sharpened it" in page
        shas = re.findall(r'name="sha" value="([0-9a-f]{40})"', page)
        subjects = [html.unescape(s) for s in re.findall(r'name="subject" value="([^"]*)"', page)]
        i = next(i for i, s in enumerate(subjects) if "Oiled it" in s)
        r = c.post("/undo", data={"sha": shas[i], "subject": subjects[i], "back": f"/tools/{slug}/history"}, headers=J)
        out["undo"] = r.json()
        text = open(os.path.join(os.environ["REPO_PATH"], "tools", slug, "README.md")).read()
        out["oiled"], out["sharpened"] = "Oiled it" in text, "Sharpened it" in text
        out["all"] = "Undo" in c.get("/history").text
    print("RESULT" + json.dumps(out))
""")


def test_undo_on_this_server(tmp_path):
    out = run_script(tmp_path, UNDO, {"where": "server", "starter": "sample"})
    assert out["setup"] == 303 and out["listed"] and out["undo"]["ok"], out
    assert not out["oiled"] and out["sharpened"] and out["all"]
    log = git("log", "--format=%s", cwd=tmp_path / "data/repo").splitlines()
    assert log[0].startswith('Undo "') and len(log) == 4  # nothing erased


def test_undo_with_github_primary(tmp_path, remote):
    out = run_script(tmp_path, UNDO, {"where": "github", "repo": f"file://{remote}"})
    assert out["setup"] == 303 and out["listed"] and out["undo"]["ok"], out
    assert not out["oiled"] and out["sharpened"]
    assert git("log", "-1", "--format=%s", "main", cwd=remote).startswith('Undo "')


def test_reverse_hunks():
    from app import manage
    old = "a\nb\nc\n"
    new = "a\nb\nX\nc\n"           # the commit added X
    later = "a\nb\nX\nY\nc\n"      # then someone added Y right after it
    diff = "@@ -2,0 +3 @@\n+X\n"
    assert manage.reverse_hunks(later, diff, old) == "a\nb\nY\nc\n"
    # A removal is put back after the line that came before it.
    diff = "@@ -3 +2,0 @@\n-c\n"
    assert manage.reverse_hunks("a\nb\nZ\n", diff, "a\nb\nc\n") == "a\nb\nc\nZ\n"
    # A changed line comes back as it was; ambiguous or missing lines refuse.
    assert manage.reverse_hunks("a\nB2\nc\n", "@@ -2 +2 @@\n-b\n+B2\n", old) == "a\nb\nc\n"
    assert manage.reverse_hunks("a\nB3\nc\n", "@@ -2 +2 @@\n-b\n+B2\n", old) is None
    assert manage.reverse_hunks("X\nX\n", "@@ -1,0 +1 @@\n+X\n", "") is None


def _photo(w, h, color="red"):
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()


def test_photos_are_shown_square_and_their_square_can_be_chosen():
    from PIL import Image
    assert post("/tools/new", kind="Sander", brand="Acme", model_no="SQ1", edit_id="sq-new")[1]["ok"]
    folder = ROOT / "tools/acme-sq1"
    # A wide photo: the app goes straight to choosing its square.
    r = client.post("/tools/acme-sq1/add", data={"kind": "photo"}, headers=JSON,
                    files=[("file", ("wide.jpg", _photo(400, 300), "image/jpeg"))])
    assert r.json()["ok"] and r.json()["url"] == "/tools/acme-sq1/crop/photo.jpg", r.text
    # A square one doesn't need it.
    r = client.post("/tools/acme-sq1/add", data={"kind": "photo"}, headers=JSON,
                    files=[("file", ("square.jpg", _photo(300, 300), "image/jpeg"))])
    assert r.json()["url"] == "/tools/acme-sq1"
    second = next(f.name for f in repo.load(ROOT).tool("acme-sq1").photos if f.name != "photo.jpg")

    page = client.get("/tools/acme-sq1/crop/photo.jpg").text
    assert 'name="x" value="0.5"' in page and 'name="zoom"' in page
    assert client.get("/tools/acme-sq1/crop/nope.jpg").status_code == 404
    assert not (folder / repo.CROPS_FILE).exists()  # nothing saved until a square is chosen

    assert post("/tools/acme-sq1/crop/photo.jpg", x="0.75", y="0.25", zoom="2")[1]["ok"]
    t = repo.load(ROOT).tool("acme-sq1")
    assert t.cover.crop == (0.75, 0.25, 2.0)
    assert json.loads((folder / repo.CROPS_FILE).read_text()) == {"photo.jpg": [0.75, 0.25, 2.0]}
    main.inventory.invalidate()
    tool_page = client.get("/tools/acme-sq1").text
    assert "photo.jpg?v=" in tool_page and "&amp;sq=0.75,0.25,2&w=1280" in tool_page

    # The thumbnail is that square of the photo.
    im = Image.open(BytesIO(client.get(t.cover.square + "&w=320").content))
    assert im.size == (150, 150)  # never bigger than the square itself
    assert Image.open(BytesIO(client.get(t.cover.thumb + "&w=320").content)).size == (320, 240)

    # Making another photo the cover: each photo keeps its own square.
    assert post("/tools/acme-sq1/files", action="cover", name=second)[1]["ok"]
    crops = repo.read_crops(folder)
    assert "photo.jpg" not in crops and list(crops.values()) == [(0.75, 0.25, 2.0)]
    moved = next(iter(crops))
    # Back to the middle drops it; deleting the last cropped photo removes the file.
    assert post(f"/tools/acme-sq1/crop/{moved}", x="0.5", y="0.5", zoom="1")[1]["ok"]
    assert not (folder / repo.CROPS_FILE).exists()
    assert post(f"/tools/acme-sq1/crop/{moved}", x="0.2", y="0.5", zoom="1.5")[1]["ok"]
    assert post("/tools/acme-sq1/files", action="delete", name=moved)[1]["ok"]
    assert not (folder / repo.CROPS_FILE).exists()
    assert repo.CROPS_FILE not in [f.name for f in repo.load(ROOT).tool("acme-sq1").other_files]
