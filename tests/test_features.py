"""Security, media, ERD, SQL explanation, JOIN analysis and lessons (no GUI)."""
import pytest

from dbide.core import crypto, lessons, media, secrets
from dbide.core.config import ConnectionConfig
from dbide.core.erd_model import layout, load_erd
from dbide.core.knowledge import TOPICS, friendly_error, keyword_topic
from dbide.core.roles import ColumnRole, RoleStore, guess_media, prepare_row, role_choices, role_store
from dbide.core.session import DatabaseSession
from dbide.core.sql_explain import explain, extract_joins, join_stat_queries, rows_for_kind


# ---- hashing & encryption ------------------------------------------------------
@pytest.mark.parametrize("algo", ["argon2id", "bcrypt", "scrypt", "pbkdf2_sha256"])
def test_hash_roundtrip(algo):
    h = crypto.hash_password("correct horse", algo)
    assert crypto.detect_hash(h) == algo
    assert crypto.verify_password("correct horse", h)
    assert not crypto.verify_password("wrong", h)
    assert crypto.hash_password("correct horse", algo) != h  # salted


def test_bcrypt_rejects_long_passwords():
    with pytest.raises(ValueError, match="72 bytes"):
        crypto.hash_password("x" * 80, "bcrypt")


@pytest.mark.parametrize("algo", ["aes256gcm", "chacha20", "fernet"])
def test_encrypt_roundtrip(algo):
    key = crypto.new_key()
    tok = crypto.encrypt_value("4111-1111 ✓", key, algo)
    assert crypto.is_encrypted(tok) and crypto.encrypted_algo(tok) == algo
    assert crypto.decrypt_value(tok, key) == "4111-1111 ✓"
    with pytest.raises(ValueError):
        crypto.decrypt_value(tok, crypto.new_key())
    tampered = tok[:-4] + ("AAAA" if not tok.endswith("AAAA") else "BBBB")
    with pytest.raises(ValueError):
        crypto.decrypt_value(tampered, key)


def test_vault_store(tmp_path):
    v = secrets.VaultStore(tmp_path / "vault.bin")
    v.create("master-pass-123")
    v.set("conn1", "s3cret")
    raw = (tmp_path / "vault.bin").read_text()
    assert "s3cret" not in raw and "conn1" not in raw
    again = secrets.VaultStore(tmp_path / "vault.bin")
    assert again.get("conn1") is None  # locked
    with pytest.raises(ValueError, match="Wrong master password"):
        again.unlock("nope-nope-nope")
    again.unlock("master-pass-123")
    assert again.get("conn1") == "s3cret"


def test_switch_backend_moves_secrets(tmp_path):
    secrets.store().set("a", "1")
    key = secrets.field_key("c1", create=True)
    v = secrets.VaultStore(tmp_path / "v.bin")
    v.create("longenough")
    assert secrets.switch_backend(v) == 2
    assert secrets.store() is v and v.get("a") == "1"
    assert secrets.field_key("c1") == key


# ---- roles ----------------------------------------------------------------------
def test_role_codes_and_prepare(tmp_path):
    for _label, code in role_choices()[1:]:
        assert ColumnRole.from_code(code).code() == code
    roles = {"pw": ColumnRole("password", "pbkdf2_sha256"), "ssn": ColumnRole("encrypted", "aes256gcm")}
    out = prepare_row(roles, {"pw": "hunter2", "ssn": "123-45", "name": "x", }, "c1")
    assert crypto.detect_hash(out["pw"]) == "pbkdf2_sha256" and out["name"] == "x"
    assert crypto.decrypt_value(out["ssn"], secrets.field_key("c1")) == "123-45"
    again = prepare_row(roles, out, "c1")  # already hashed / encrypted: left alone
    assert again == out
    assert prepare_row(roles, {"pw": None, "ssn": ""}, "c1") == {"pw": None, "ssn": ""}

    store = RoleStore(tmp_path / "r.json")
    store.set_table("c1", None, "users", roles)
    assert RoleStore(tmp_path / "r.json").get("c1", None, "users") == roles
    assert guess_media("avatar", "BLOB") == "blob" and guess_media("photo_path", "TEXT") == "path"
    assert guess_media("notes", "TEXT") is None


# ---- media ------------------------------------------------------------------------
def test_media_store_and_report(tmp_path):
    db = tmp_path / "shop.sqlite"
    cfg = ConnectionConfig(name="s", driver="sqlite", database=str(db))
    src = tmp_path / "cat pic.png"
    src.write_bytes(lessons.sample_png())
    stored = media.store_file(str(src), cfg)
    assert stored.startswith("shop_media/images/cat_pic_") and stored.endswith(".png")
    assert media.resolve(stored, cfg).read_bytes() == src.read_bytes()
    assert media.detect_mime(src.read_bytes()) == "image/png"
    (media.default_folder(cfg) / "images" / "orphan.png").write_bytes(b"x")
    reps = media.folder_reports([stored, "shop_media/images/gone.png"], cfg, media.default_folder(cfg))
    rep = reps[0]
    assert rep.referenced == 1 and rep.missing == ["shop_media/images/gone.png"]
    assert [p.endswith("orphan.png") for p in rep.orphans] == [True]


# ---- explanations ------------------------------------------------------------------
def test_explain_statements():
    ex = explain("DELETE FROM users")
    assert ex.destructive and "every row" in ex.warnings[0]
    ex = explain("UPDATE users SET name = 'x' WHERE id = 3")
    assert not ex.destructive and "id = 3" in ex.details[0]
    ex = explain("SELECT b.title FROM books b LEFT JOIN authors a ON a.id = b.author_id WHERE b.year > 1900 ORDER BY b.year DESC LIMIT 5")
    text = " ".join([ex.summary, *ex.details])
    assert "LEFT JOIN" in text and "b.year &gt; 1900" in text and "at most" in text
    ex = explain("CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INT NOT NULL REFERENCES a(id))")
    assert any("a</b>.id" in d for d in ex.details)
    ex = explain("CREATE TABLE nokey (x TEXT)")
    assert "no primary key" in ex.warnings[0]
    drop = explain("DROP TABLE t", "mysql")
    assert drop.destructive and "<code>t</code>" in drop.summary
    assert explain("this is not sql (") is None


def test_friendly_errors_and_topics():
    assert "authors" in friendly_error("(sqlite3.OperationalError) no such table: authors")
    assert "foreign key" in friendly_error("FOREIGN KEY constraint failed").lower()
    assert "users.email" in friendly_error("UNIQUE constraint failed: users.email")
    assert "required" in friendly_error("(1048, \"Column 'name' cannot be null\")")
    assert friendly_error("something unrelated") is None
    assert keyword_topic("select") == "kw:SELECT" and "cmd:apply" in TOPICS


# ---- JOIN analysis on real data ------------------------------------------------------
@pytest.fixture
def library(tmp_path):
    s = DatabaseSession(ConnectionConfig(name="lib", driver="sqlite", database=str(tmp_path / "lib.db"), id="lib"))
    q = s.query_connection()
    res = q.run(
        "CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
        "CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER REFERENCES authors(id));"
        "CREATE TABLE photos (id INTEGER PRIMARY KEY, author_id INTEGER UNIQUE REFERENCES authors(id), image BLOB, image_path TEXT);"
        "INSERT INTO authors VALUES (1,'A'),(2,'B'),(3,'C');"
        "INSERT INTO books VALUES (1,'x',1),(2,'y',1),(3,'z',2),(4,'orphan',NULL);"
    )
    assert all(r.error is None for r in res)
    q.close()
    yield s
    s.close()


def test_join_extraction_and_counts(library):
    a = extract_joins("SELECT * FROM authors a LEFT JOIN books b ON b.author_id = a.id", "sqlite")
    (step,) = a.steps
    assert step.kind == "LEFT" and step.left.alias == "a" and step.right.alias == "b"
    assert step.pairs == [("a", "id", "b", "author_id")]
    qs = join_stat_queries(step, library.driver.quote, library.driver.qualify)
    with library.engine.connect() as c:
        stats = {k: int(c.exec_driver_sql(v).scalar()) for k, v in qs.items() if not k.endswith("sample")}
    assert stats == {"left_total": 3, "right_total": 4, "left_unmatched": 1, "right_unmatched": 1, "inner_rows": 3}
    assert rows_for_kind("LEFT", stats) == 4 and rows_for_kind("INNER", stats) == 3 and rows_for_kind("FULL", stats) == 5
    using = extract_joins("SELECT * FROM books JOIN authors USING (id)", "sqlite")
    assert using.steps[0].pairs == [("books", "id", "authors", "id")]
    assert extract_joins("SELECT 1", "sqlite") is None


def test_erd_model(library, tmp_path):
    png = lessons.sample_png()
    with library.engine.begin() as c:
        c.exec_driver_sql("INSERT INTO photos (author_id, image, image_path) VALUES (1, ?, 'lib_media/images/missing.png')", (png,))
    role_store().set_table("lib", None, "photos", {"image": ColumnRole("media", storage="blob"),
                                                    "image_path": ColumnRole("media", storage="path")})
    m = load_erd(library, None, role_store().all_tables("lib", None))
    assert set(m.tables) == {"authors", "books", "photos"}
    rels = {(r.child, r.parent): r for r in m.relations}
    assert not rels[("books", "authors")].one_to_one and rels[("photos", "authors")].one_to_one
    st = {(x.column): x for x in m.media}
    assert st["image"].storage == "blob" and st["image"].total_bytes == len(png) and not st["image"].guessed
    assert st["image_path"].folders[0].missing == ["lib_media/images/missing.png"]
    pos = layout(m)
    assert pos["authors"][0] < pos["books"][0]  # parents to the left of children


# ---- lessons: "do it for me" SQL satisfies every check ----------------------------------
def test_lessons_walkthrough(tmp_path):
    cfg = ConnectionConfig(name=lessons.PRACTICE_NAME, driver="sqlite", database=str(tmp_path / "p.sqlite"), id=lessons.PRACTICE_ID)
    s = DatabaseSession(cfg)
    q = s.query_connection()
    recent = []
    for lsn in lessons.LESSONS:
        for step in lsn.steps:
            if step.sql:
                for r in q.run(step.sql):
                    recent.insert(0, (r.sql, r.error is None))
            for key, code in step.roles.items():
                t, c = key.split(".")
                rs = role_store().get(cfg.id, None, t)
                rs[c] = ColumnRole.from_code(code)
                role_store().set_table(cfg.id, None, t, rs)
            if step.sample_media:
                stored = media.store_file(str(_png(tmp_path)), cfg)
                with s.engine.begin() as c:
                    c.exec_driver_sql("INSERT INTO photos (caption, image, image_path) VALUES ('s', ?, ?)", (lessons.sample_png(), stored))
            if step.title == "Add a user":  # done in the grid in the real app: simulate it
                with s.engine.begin() as c:
                    c.exec_driver_sql("INSERT INTO users (username, password_hash) VALUES ('ada', ?)",
                                      (crypto.hash_password("pw", "argon2id"),))
            if step.check:
                ok, msg = step.check(s, lessons.Context(recent))
                assert ok, f"{lsn.title} / {step.title}: {msg}"
    q.close()
    s.close()


def _png(tmp_path):
    p = tmp_path / "pic.png"
    p.write_bytes(lessons.sample_png())
    return p
