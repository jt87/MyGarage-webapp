"""Editing from the app (1.1): Markdown rewrites, the routes, and git writes."""

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import fixture
from test_app import ROOT, client  # shares the app and its logged-in client

from app import edit, main, repo
from app.sync import RepoSync, WriteError

JSON = {"Accept": "application/json"}


# --------------------------------------------------------------------------- Markdown

def test_log_entry_goes_in_date_order_and_escapes_pipes():
    text = edit.add_log_entry(fixture.TABLE_SAW, "2027-01-02", "Checked | alignment\nand fence")
    rows = edit.get_table(text, "maintenance")
    assert rows == [["2027-01-02", "Checked | alignment and fence"], ["2027-01-03", "Waxed the top."]]
    text = edit.add_log_entry(text, "2027-02-01", "New belt")
    assert edit.get_table(text, "maintenance")[-1] == ["2027-02-01", "New belt"]
    # Still parses the way the app reads it.
    assert "## Settings & notes" in text and "Sibling tool" in text


def test_log_entry_fills_template_placeholder_and_creates_section():
    blank = "# X\n\n## Maintenance log\n\n| Date | What |\n|------|------|\n| | |\n"
    assert edit.get_table(edit.add_log_entry(blank, "2027-01-01", "a"), "maint") == [["2027-01-01", "a"]]
    made = edit.add_log_entry("# X\n", "2027-01-01", "a")
    assert "## Maintenance log" in made and edit.get_table(made, "maint") == [["2027-01-01", "a"]]
    with pytest.raises(edit.Invalid):
        edit.add_log_entry("# X\n", "01/02/2027", "a")


def test_round_trips_leave_files_unchanged():
    t = fixture.TABLE_SAW
    assert edit.set_specs(t, edit.get_specs(t)) == t
    parts = edit.get_table(t, "consumables")
    assert edit.set_table(t, "consumables", "Consumables & parts", main.PART_HEADER, parts) == t
    row = edit.get_inventory_row(fixture.INVENTORY, "acme-ts10")
    assert edit.set_inventory_row(fixture.INVENTORY, "acme-ts10", {"value": row["value"]}) == fixture.INVENTORY


def test_inventory_row_updates_total_and_adds_rows_above_it():
    text = edit.set_inventory_row(fixture.INVENTORY, "acme-ts10", {"value": "~$350"})
    assert "**~$470** (as of Jan 2027)" in text
    text = edit.set_inventory_row(text, "new-saw", {"tool": "New saw", "value": "~$30"})
    rows, _, total = repo.parse_inventory_table(text)
    assert rows["new-saw"]["value"] == "~$30" and total.startswith("~$500")
    assert text.index("new-saw") < text.index("**Total**")


def test_spec_merge_keeps_other_changes_and_catches_real_conflicts():
    base = [("Model #", "TS10"), ("Serial #", "0001")]
    mine = [("Model #", "TS10"), ("Serial #", "0002"), ("Color", "Blue")]
    theirs = [("Model #", "TS10-B"), ("Serial #", "0001")]
    assert edit.merge_specs(base, mine, theirs) == [("Model #", "TS10-B"), ("Serial #", "0002"), ("Color", "Blue")]
    with pytest.raises(edit.Conflict):
        edit.merge_specs(base, mine, [("Model #", "TS10"), ("Serial #", "0003")])
    # Removing a row on the form removes it.
    assert edit.merge_specs(base, [("Model #", "TS10")], base) == [("Model #", "TS10")]


def test_upload_names_file_things_where_they_belong():
    now = datetime(2027, 3, 4, 5, 6, 7)
    assert edit.upload_name("IMG_0001.HEIC", "photo", set(), now) == "photo.heic"
    assert edit.upload_name("IMG_0002.jpeg", "photo", {"photo.heic"}, now) == "photo-20270304-050607.jpg"
    assert edit.upload_name("scan.pdf", "receipt", set(), now) == "receipt.pdf"
    assert repo.PROOF_RE.search(edit.upload_name("scan.pdf", "receipt", {"receipt.pdf"}, now))
    assert repo.MANUAL_RE.search(edit.upload_name("TS10 Owners.pdf", "manual", set(), now))
    # A document whose name looks like a receipt would be filed as one.
    assert not repo.PROOF_RE.search(edit.upload_name("order form.pdf", "document", set(), now)[4:]) or \
        edit.upload_name("order form.pdf", "document", set(), now).startswith("doc-")
    for bad in ("README.md", "x.html", "noext"):
        with pytest.raises(edit.Invalid):
            edit.upload_name(bad, "document", set(), now)


# --------------------------------------------------------------------------- routes (mounted folder)

def readme(slug):
    return (ROOT / "tools" / slug / "README.md").read_text()


def test_log_route_saves_and_is_idempotent():
    for page in ("/tools/acme-dd20/log", "/tools/acme-dd20/edit", "/tools/acme-dd20/add",
                 "/edit/tools/acme-dd20", "/tools/new"):
        assert client.get(page).status_code == 200, page
    form = {"date": "2027-02-02", "what": "Greased the chuck", "edit_id": "log-1"}
    r = client.post("/tools/acme-dd20/log", data=form, headers=JSON)
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert client.post("/tools/acme-dd20/log", data=form, headers=JSON).json()["ok"]
    assert readme("acme-dd20").count("Greased the chuck") == 1
    page = client.get("/tools/acme-dd20").text
    assert "Greased the chuck" in page
    # Without JavaScript: a plain form post redirects back with a message.
    r = client.post("/tools/acme-dd20/log", data={"date": "bad", "what": "x"}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]


def tool_form(slug):
    html = client.get(f"/tools/{slug}/edit").text
    base = json.loads(re.search(r'name="base" value="([^"]*)"', html).group(1)
                      .replace("&#34;", '"').replace("&quot;", '"').replace("&amp;", "&")
                      .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
    return base


def form_data(state, **changes):
    s = {**state, **changes}
    data = {k: s[k] for k in ("kind", "detail", "brand", "model", "category", "paid", "value")}
    data["base"] = json.dumps(state)
    data["spec_k"] = [k for k, _ in s["specs"]]
    data["spec_v"] = [v for _, v in s["specs"]]
    for i, name in enumerate(("part_item", "part_spec", "part_where", "part_price")):
        data[name] = [p[i] for p in s["parts"]]
    return data


def test_edit_details_merges_and_conflicts():
    state = tool_form("acme-ts10")
    assert state["paid"] == "$150" and state["category"] == "Table saw"
    specs = [s if s[0] != "Serial #" else ["Serial #", "0009"] for s in state["specs"]]
    parts = state["parts"] + [["Fence clamp", "Acme **FC-2**", "Amazon", "~$9"]]
    r = client.post("/tools/acme-ts10/edit", data=form_data(state, specs=specs, parts=parts, value="325"),
                    headers=JSON)
    assert r.json()["ok"], r.text
    text = readme("acme-ts10")
    assert "| Serial # | 0009 |" in text and "**FC-2**" in text
    assert "| Category | Table saw |" in text  # category row kept in place
    rows, _, _ = repo.parse_inventory_table((ROOT / "README.md").read_text())
    assert rows["acme-ts10"]["value"] == "~$325"

    # Someone else changes the serial; this stale form changes it too.
    stale = state
    r = client.post("/tools/acme-ts10/edit",
                    data=form_data(stale, specs=[s if s[0] != "Serial #" else ["Serial #", "0010"]
                                                 for s in stale["specs"]]), headers=JSON)
    assert r.status_code == 409 and not r.json()["retry"]
    assert "| Serial # | 0009 |" in readme("acme-ts10")


def test_markdown_edit_detects_conflict_and_can_force():
    html = client.get("/edit/tools/acme-dd20").text
    base = re.search(r'name="base" value="([^"]*)"', html).group(1)
    current = readme("acme-dd20")
    r = client.post("/edit/tools/acme-dd20", data={"text": current + "\nMine.\n", "base": base}, headers=JSON)
    assert r.json()["ok"]
    r = client.post("/edit/tools/acme-dd20", data={"text": current + "\nStale.\n", "base": base}, headers=JSON)
    assert r.status_code == 409
    r = client.post("/edit/tools/acme-dd20",
                    data={"text": current + "\nStale.\n", "base": base, "force": "1"}, headers=JSON)
    assert r.json()["ok"] and "Stale." in readme("acme-dd20")


def test_upload_files():
    r = client.post("/tools/acme-dd20/add", data={"kind": "photo", "edit_id": "up-1"}, headers=JSON,
                    files=[("file", ("IMG_1.jpg", fixture._jpeg(), "image/jpeg")),
                           ("file", ("IMG_2.jpg", fixture._jpeg(), "image/jpeg"))])
    assert r.json()["ok"], r.text
    t = repo.load(ROOT).tool("acme-dd20")
    assert [p.name for p in t.photos][0] == "photo.jpg" and len(t.photos) == 2
    r = client.post("/tools/acme-dd20/add", data={"kind": "receipt"}, headers=JSON,
                    files=[("file", ("scan.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf"))])
    assert r.json()["ok"] and repo.load(ROOT).tool("acme-dd20").proofs
    r = client.post("/tools/acme-dd20/add", data={"kind": "document"}, headers=JSON,
                    files=[("file", ("README.md", b"# pwned", "text/markdown"))])
    assert r.status_code == 400
    assert "pwned" not in readme("acme-dd20")


def test_new_tool():
    r = client.post("/tools/new", data={"kind": "Sander", "detail": "Random Orbit", "brand": "Acme", "model_no": "RS5",
                                        "category": "Sander", "paid": "60", "value": "90",
                                        "edit_id": "new-1"}, headers=JSON)
    assert r.json()["ok"] and r.json()["url"] == "/tools/acme-rs5"
    again = client.post("/tools/new", data={"kind": "Sander", "brand": "Acme", "model_no": "RS5", "edit_id": "new-1"}, headers=JSON)
    assert again.json()["ok"] and not (ROOT / "tools" / "acme-rs5-2").exists()
    t = repo.load(ROOT).tool("acme-rs5")
    assert t.subtitle == "Random Orbit Sander" and t.category == "Sander" and t.value == 90 and t.paid == 60
    assert client.get("/tools/acme-rs5").status_code == 200


def test_edit_needs_login_and_same_site():
    from starlette.testclient import TestClient
    anon = TestClient(main.app)
    assert anon.post("/tools/acme-dd20/log", data={"date": "2027-01-01", "what": "x"},
                     headers=JSON).status_code == 401
    r = client.post("/tools/acme-dd20/log", data={"date": "2027-01-01", "what": "x"},
                    headers={**JSON, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_offline_manifest_and_service_worker():
    m = client.get("/offline/manifest.json").json()
    assert "/tools/acme-ts10" in m["pages"] and "/tools/acme-ts10/log" in m["pages"]
    assert "/settings" not in m["pages"]  # it shows the API key
    assert any(u.startswith("/thumb/tools/acme-ts10/photo.jpg") for u in m["thumbs"])
    assert any(f["url"] == "/files/tools/acme-ts10/manual.pdf" for f in m["files"])
    from starlette.testclient import TestClient
    sw = TestClient(main.app).get("/sw.js")  # no login needed: it's static code
    assert sw.status_code == 200 and main.STATIC_VERSION in sw.text and "__VERSION__" not in sw.text
    assert client.get("/offline").status_code == 200
    page = client.get("/tools/acme-ts10").text
    assert f"/static/app.js?v={main.STATIC_VERSION}" in page
    assert "cdnjs" not in client.get("/view/tools/acme-ts10/manual.pdf").text


# --------------------------------------------------------------------------- git writes

def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    """A bare repo standing in for GitHub, seeded with the fixture."""
    seed = fixture.build(tmp_path / "seed")
    git("init", "-q", "-b", "main", cwd=seed)
    git("add", "-A", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed", cwd=seed)
    bare = tmp_path / "remote.git"
    git("clone", "-q", "--bare", str(seed), str(bare), cwd=tmp_path)
    return bare


def other_push(remote: Path, tmp: Path, rel: str, transform) -> None:
    work = tmp / "other"
    git("clone", "-q", str(remote), str(work), cwd=tmp)
    p = work / rel
    p.write_text(transform(p.read_text()))
    git("-c", "user.name=o", "-c", "user.email=o@o", "commit", "-qam", "other", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)


def log_mutate(date, what):
    def mutate(root):
        p = root / "tools/acme-ts10/README.md"
        p.write_text(edit.add_log_entry(p.read_text(), date, what))
        return ["tools/acme-ts10/README.md"]
    return mutate


def test_git_write_commits_and_pushes_on_top_of_other_changes(remote, tmp_path):
    s = RepoSync(tmp_path / "clone", url=f"file://{remote}", branch="main")
    assert s.sync_once()
    # Someone pushes after the clone; the write must land on top, not reject.
    other_push(remote, tmp_path, "tools/acme-dd20/README.md", lambda t: t + "\nFrom elsewhere.\n")
    assert s.write(log_mutate("2027-05-05", "Trued the fence"), "Log", author="tester via MyGarage")
    head = git("log", "-1", "--format=%an|%s", cwd=remote)
    assert head == "tester via MyGarage|Log"
    shown = git("show", "main:tools/acme-ts10/README.md", cwd=remote)
    assert "Trued the fence" in shown
    assert "From elsewhere." in git("show", "main:tools/acme-dd20/README.md", cwd=remote)
    assert s.head == git("rev-parse", "main", cwd=remote)
    # Nothing to change: no empty commit.
    assert not s.write(lambda root: [], "noop")


def test_git_write_failure_resets_the_clone(remote, tmp_path):
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'remote: Permission to x denied' >&2\nexit 1\n")
    hook.chmod(0o755)
    s = RepoSync(tmp_path / "clone", url=f"file://{remote}", branch="main")
    s.sync_once()
    before = s.head
    with pytest.raises(WriteError) as e:
        s.write(log_mutate("2027-05-05", "Nope"), "Log")
    assert "Read and write" in str(e.value) and not e.value.retry
    assert "Nope" not in (tmp_path / "clone/tools/acme-ts10/README.md").read_text()
    assert git("rev-parse", "HEAD", cwd=tmp_path / "clone") == before


def test_git_write_conflict_from_mutate_resets(remote, tmp_path):
    s = RepoSync(tmp_path / "clone", url=f"file://{remote}", branch="main")
    s.sync_once()

    def mutate(root):
        (root / "tools/acme-ts10/README.md").write_text("half-written")
        raise edit.Conflict("Something")

    with pytest.raises(edit.Conflict):
        s.write(mutate, "x")
    assert (tmp_path / "clone/tools/acme-ts10/README.md").read_text() == fixture.TABLE_SAW


def test_git_write_redoes_the_edit_when_github_moved_mid_save(remote, tmp_path):
    s = RepoSync(tmp_path / "clone", url=f"file://{remote}", branch="main")
    s.sync_once()
    calls = []

    def mutate(root):
        if not calls:  # someone pushes between our fetch and our push
            other_push(remote, tmp_path, "tools/acme-ts10/README.md",
                       lambda t: t.replace("Waxed the top.", "Waxed the top twice."))
        calls.append(1)
        return log_mutate("2027-05-05", "Trued the fence")(root)

    assert s.write(mutate, "Log")
    assert len(calls) == 2
    shown = git("show", "main:tools/acme-ts10/README.md", cwd=remote)
    assert "Waxed the top twice." in shown and "Trued the fence" in shown


def test_edit_name_as_type_size_brand_model():
    state = tool_form("acme-ts10")
    assert (state["kind"], state["detail"], state["brand"], state["model"]) == ("Table Saw", '10"', "Acme", "TS10")
    # Saving other fields leaves the name as written.
    assert client.post("/tools/acme-ts10/edit", data=form_data(state, value="310"), headers=JSON).json()["ok"]
    assert readme("acme-ts10").startswith('# Acme TS10 — 10" Table Saw')
    # A type that isn't the category is kept in a Type row so the split survives.
    state = tool_form("acme-ts10")
    r = client.post("/tools/acme-ts10/edit", data=form_data(state, kind="Saw", detail='10" Table'), headers=JSON)
    assert r.json()["ok"], r.text
    text = readme("acme-ts10")
    assert text.startswith('# Acme TS10 — 10" Table Saw') and "| Type | Saw |" in text
    t = repo.load(ROOT).tool("acme-ts10")
    assert (t.kind, t.kind_detail, t.maker, t.model) == ("Saw", '10" Table', "Acme", "TS10")
    state = tool_form("acme-ts10")
    client.post("/tools/acme-ts10/edit", data=form_data(state, kind="Table Saw", detail='10"'), headers=JSON)
    assert "| Type |" not in readme("acme-ts10")
