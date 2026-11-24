"""Python source extractor, called directly on inline source bytes."""

from repo_intel.extractors.python_source import EXTRACTORS, PythonSource
from repo_intel.ontology import EntityKind, Predicate, check_relation

BLOB = "0" * 64
X = PythonSource()


def run(source: str, path: str = "pkg/mod.py"):
    return X.extract(path, source.encode(), BLOB)


def entities(result, kind):
    return [o for o in result.observations if o.kind == "entity" and o.payload["kind"] == kind]


def relations(result, predicate):
    return [o for o in result.observations if o.kind == "relation" and o.payload["predicate"] == predicate]


def of_kind(result, kind):
    return [o for o in result.observations if o.kind == kind]


def text(source: str, o) -> str:
    s = o.evidence.span
    return source.encode()[s.start_byte : s.end_byte].decode()


FASTAPI = '''from fastapi import APIRouter, FastAPI

app = FastAPI()
router = APIRouter(prefix="/v1")
PATH = "/dynamic"


@app.get("/customers")
def list_customers():
    return []


@router.post("/orders/{order_id}")
async def create_order(order_id: int):
    return {}


@app.get(PATH)
def dynamic():
    return {}


@app.get(f"/x/{PATH}")
def formatted():
    return {}
'''


def test_registered_and_matches_python_only():
    assert [e.id for e in EXTRACTORS] == ["python.source"]
    assert X.matches("app/main.py") and not X.matches("app/main.pyi") and not X.matches("pyproject.toml")


def test_fastapi_routes():
    r = run(FASTAPI)
    assert r.parsed
    found = entities(r, "Endpoint")
    # Only the app route is final here; the router route's path depends on where it is included.
    assert [e.payload["natural_key"] for e in found] == [
        {"component_root": "@unit", "protocol": "http", "method": "GET", "route": "/customers"},
    ]
    assert found[0].payload["name"] == "GET /customers"
    assert found[0].payload["attributes"] == {"framework": "fastapi", "handler": "list_customers"}
    assert text(FASTAPI, found[0]) == '@app.get("/customers")'
    assert found[0].evidence.span.start_line == found[0].evidence.span.end_line == 8
    handlers = of_kind(r, "route_handler")
    assert [h.payload for h in handlers] == [
        {"method": "GET", "route": "/customers", "handler": "list_customers", "router": "app"},
        {"method": "POST", "route": "/v1/orders/{order_id}", "handler": "create_order", "router": "router"},
    ]
    assert text(FASTAPI, handlers[1]) == '@router.post("/orders/{order_id}")'
    assert [d.payload for d in of_kind(r, "router_decl")] == [{"name": "app", "factory": "FastAPI", "prefix": ""}, {"name": "router", "factory": "APIRouter", "prefix": "/v1"}]


def test_router_includes_and_mounts():
    src = 'from fastapi import FastAPI\nfrom . import routes\napp = FastAPI()\napp.include_router(routes.router, prefix="/api")\napp.include_router(routes.other)\napp.include_router(routes.x, prefix=PREFIX)\napp.mount("/admin", routes.admin)\napp.mount(path="/s", app=sub)\n'
    got = [o.payload for o in of_kind(run(src), "router_include")]
    assert got == [
        {"owner": "app", "target": "routes.router", "prefix": "/api", "via": "include_router"},
        {"owner": "app", "target": "routes.other", "prefix": "", "via": "include_router"},
        {"owner": "app", "target": "routes.x", "prefix": None, "via": "include_router"},
        {"owner": "app", "target": "routes.admin", "prefix": "/admin", "via": "mount"},
        {"owner": "app", "target": "sub", "prefix": "/s", "via": "mount"},
    ]


def test_method_symbols_and_calls():
    src = """from .db import Session
from . import helpers


def top(x):
    helpers.fmt(x)
    print(x)
    return Order(x).save()


class Order:
    def save(self):
        return self._write()

    @staticmethod
    def _write():
        with Session() as s:
            s.commit()
"""
    r = run(src)
    assert [(o.payload["name"], o.payload["symbol_kind"]) for o in of_kind(r, "symbol")] == [("top", "function"), ("Order", "class"), ("Order.save", "method"), ("Order._write", "method")]
    calls = [(o.payload["caller"], o.payload["callee"]) for o in of_kind(r, "call")]
    assert calls == [("top", "helpers.fmt"), ("top", "Order"), ("Order.save", "self._write"), ("Order._write", "Session"), ("Order._write", "s.commit")]
    assert ("top", "print") not in calls, "builtins are not recorded"


def test_dynamic_route_path_yields_diagnostic_not_entity():
    r = run(FASTAPI)
    assert not any(e.payload["attributes"]["handler"] in ("dynamic", "formatted") for e in entities(r, "Endpoint"))
    info = [d for d in r.diagnostics if d.severity == "info"]
    assert len(info) == 2 and "dynamic" in info[0].message and "formatted" in info[1].message


def test_api_route_methods():
    r = run('import fastapi\napp = fastapi.FastAPI()\n@app.api_route("/a", methods=["GET", "put"])\ndef a(): ...\n')
    assert [e.payload["name"] for e in entities(r, "Endpoint")] == ["GET /a", "PUT /a"]
    assert [h.payload["router"] for h in of_kind(r, "route_handler")] == ["app", "app"]


def test_non_fastapi_files_ignored():
    flask = 'from flask import Flask\napp = Flask(__name__)\n\n@app.get("/status")\ndef status():\n    return {}\n'
    r = run(flask)
    assert not entities(r, "Endpoint") and not of_kind(r, "route_handler")
    # A fastapi file does not turn unrelated local objects into routers.
    r = run('from fastapi import FastAPI\ncache = Cache()\n\n@cache.get("/k")\ndef k(): ...\n')
    assert not entities(r, "Endpoint")


MODELS = '''from sqlalchemy import Column, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = ()

    id: Mapped[int] = mapped_column(primary_key=True)
    total_cents: Mapped[int]
    note = Column(String)
    legacy = Column("legacy_name", String)
    items: Mapped[list["Item"]] = relationship(back_populates="order")
    limit = 10
'''


def test_tables_and_fields():
    r = run(MODELS)
    (table,) = entities(r, "Table")
    assert table.payload["natural_key"] == {"datastore": "default", "name": "orders"}
    assert table.payload["name"] == "orders"
    assert table.payload["attributes"] == {"orm": "sqlalchemy", "model": "Order"}
    assert text(MODELS, table) == '__tablename__ = "orders"'
    fields = entities(r, "Field")
    assert [f.payload["natural_key"] for f in fields] == [{"table": "orders", "name": n} for n in ("id", "total_cents", "note", "legacy_name")]
    assert text(MODELS, fields[1]) == "total_cents: Mapped[int]"
    contains = relations(r, "CONTAINS")
    assert [c.payload["object"]["natural_key"]["name"] for c in contains] == ["id", "total_cents", "note", "legacy_name"]
    assert all(c.payload["subject"]["kind"] == "Table" and c.payload["subject"]["natural_key"]["name"] == "orders" for c in contains)
    assert check_relation(Predicate.CONTAINS, EntityKind.TABLE, EntityKind.FIELD) is None


def test_class_without_tablename_is_not_a_table():
    r = run("class Plain:\n    x: int = 1\n")
    assert not entities(r, "Table") and not entities(r, "Field")


MIGRATION = '''"""0002"""
import sqlalchemy as sa
from alembic import op

revision = "0002_orders"
down_revision = '0001_init'


def upgrade() -> None:
    op.create_table("orders", sa.Column("id", sa.Integer, primary_key=True))
    op.add_column("customers", sa.Column("phone", sa.String, nullable=True))
    op.alter_column("orders", "total_cents", new_column_name="amount_cents")
    op.drop_column("customers", "fax")
    op.drop_table("legacy")
    op.create_table(NAME)
'''


def test_alembic_migration_and_ops():
    r = run(MIGRATION, "migrations/versions/0002_orders.py")
    (migration,) = entities(r, "Migration")
    assert migration.payload["natural_key"] == {"tool": "alembic", "revision": "0002_orders"}
    assert migration.payload["attributes"] == {"down_revision": "0001_init"}
    assert text(MIGRATION, migration) == 'revision = "0002_orders"'
    migrates = relations(r, "MIGRATES")
    assert all(m.payload["subject"] == {"kind": "Migration", "natural_key": {"tool": "alembic", "revision": "0002_orders"}, "name": "0002_orders"} for m in migrates)
    got = [(m.payload["object"]["kind"], m.payload["object"]["natural_key"].get("table"), m.payload["object"]["natural_key"]["name"], m.payload["qualifiers"]["op"]) for m in migrates]
    assert got == [
        ("Table", None, "orders", "create_table"),
        ("Table", None, "customers", "add_column"),
        ("Field", "customers", "phone", "add_column"),
        ("Table", None, "orders", "alter_column"),
        ("Field", "orders", "amount_cents", "alter_column"),
        ("Table", None, "customers", "drop_column"),
        ("Table", None, "legacy", "drop_table"),
    ]
    # Only fields that exist after the migration are declared: not the renamed-away or dropped ones,
    # and not the columns listed inside create_table.
    assert [f.payload["natural_key"] for f in entities(r, "Field")] == [{"table": "customers", "name": "phone"}, {"table": "orders", "name": "amount_cents"}]
    assert text(MIGRATION, migrates[0]).startswith('op.create_table("orders"')
    assert any("create_table" in d.message for d in r.diagnostics)
    for kind in (EntityKind.TABLE, EntityKind.FIELD):
        assert check_relation(Predicate.MIGRATES, EntityKind.MIGRATION, kind) is None


def test_down_revision_none_and_non_alembic_file():
    r = run('from alembic import op\nrevision = "0001"\ndown_revision = None\n')
    assert entities(r, "Migration")[0].payload["attributes"] == {"down_revision": None}
    assert not entities(run('revision = "0001"\n'), "Migration")


CONFIG = '''import os
from os import getenv

A = os.environ["DATABASE_URL"]
B = os.environ.get("API_TOKEN", "sk-live-default-secret")
C = os.getenv("REGION", "eu-west-1")
D = getenv("DEBUG")
E = os.environ[A]
F = os.environ["DATABASE_URL"]
'''


def test_config_keys_names_only():
    r = run(CONFIG)
    keys = entities(r, "ConfigKey")
    assert [k.payload["natural_key"] for k in keys] == [{"name": n} for n in ("DATABASE_URL", "API_TOKEN", "REGION", "DEBUG")]
    refs = relations(r, "REFERENCES_CONFIG")
    assert [(x.payload["subject"], x.payload["object"]["natural_key"]["name"]) for x in refs] == [("@package", n) for n in ("DATABASE_URL", "API_TOKEN", "REGION", "DEBUG")]
    assert text(CONFIG, keys[0]) == 'os.environ["DATABASE_URL"]'
    assert text(CONFIG, keys[1]) == 'os.environ.get("API_TOKEN"'
    dumped = repr([(o.kind, o.payload, text(CONFIG, o)) for o in r.observations if o.kind in ("entity", "relation")])
    assert "sk-live-default-secret" not in dumped and "eu-west-1" not in dumped


def test_symbols_and_imports():
    source = "import os.path as p\nfrom . import sibling\nfrom ..pkg.mod import a as b, c\n\n\ndef top():\n    def inner(): ...\n\n\n@decorated\nclass Thing:\n    def method(self): ...\n"
    r = run(source)
    assert [s.payload for s in of_kind(r, "symbol")] == [{"name": "top", "symbol_kind": "function"}, {"name": "Thing", "symbol_kind": "class"}, {"name": "Thing.method", "symbol_kind": "method"}]
    assert [i.payload for i in of_kind(r, "import")] == [
        {"module": "os.path", "names": [], "level": 0, "alias": "p"},
        {"module": "", "names": ["sibling"], "level": 1},
        {"module": "pkg.mod", "names": ["a", "c"], "level": 2, "aliases": {"a": "b"}},
    ]
    assert text(source, of_kind(r, "import")[1]) == "from . import sibling"


def test_syntax_error_does_not_raise():
    broken = 'import os\n\nKEY = os.environ["GOOD"]\n\ndef broken(:\n    return os.environ["BAD"\n\nclass Also(\n'
    r = run(broken)
    assert r.parsed is False
    assert any(d.severity == "warning" for d in r.diagnostics)
    assert [k.payload["natural_key"]["name"] for k in entities(r, "ConfigKey")] == ["GOOD"]
    for junk in (b"\xff\xfe\x00garbage(((", b"", b"def (\n" * 50):
        X.extract("x.py", junk, BLOB)  # must not raise


def test_deterministic():
    assert [o.row() for o in run(FASTAPI).observations] == [o.row() for o in run(FASTAPI).observations]


ORM = """from sqlalchemy import delete, func, insert, select, text, update

from . import models
from .models import Customer, Order


def orders_for(session, customer_id):
    session.scalars(select(Order).where(Order.customer_id == customer_id))
    return list(session.scalars(select(Order)))


def stats(session):
    session.execute(select(func.count(Order.id), models.Customer.email))
    session.query(Customer).all()
    return session.get(models.Invoice, 1)


def add_customer(session, email):
    with Session() as other:
        pass
    customer = Customer(email=email)
    ignored = Order()
    session.add(customer)
    session.add_all([models.Invoice(total=1)])
    return customer


def mutate(session, obj):
    session.execute(insert(Order).values(id=1))
    session.execute(update(models.Customer).where(Customer.id == 1))
    session.execute(delete(Order))
    session.delete(obj)
    settings.get(KEY, None)
    select(TABLE_CONSTANT)


class Repo:
    def find(self, session):
        return session.query(Order.id).first()


def count_rows(session, table_name):
    return session.execute(text(f"SELECT count(*) FROM {table_name}")).scalar_one()


def more_sql(conn, name):
    conn.execute("SELECT * FROM " + name)
    conn.exec_driver_sql("SELECT * FROM %s" % name)
    conn.execute(text("SELECT * FROM {}".format(name)))
    conn.execute(text("SELECT 1"))
    conn.execute(text(f"SELECT 2"))
"""


def test_orm_use():
    r = run(ORM)
    uses = of_kind(r, "orm_use")
    assert [(u.payload["function"], u.payload["model"], u.payload["access"]) for u in uses] == [
        ("orders_for", "Order", "read"),
        ("stats", "Order", "read"),
        ("stats", "models.Customer", "read"),
        ("stats", "Customer", "read"),
        ("stats", "models.Invoice", "read"),
        ("add_customer", "Customer", "write"),
        ("add_customer", "models.Invoice", "write"),
        ("mutate", "Order", "write"),
        ("mutate", "models.Customer", "write"),
        ("Repo.find", "Order", "read"),
    ]
    assert all(set(u.payload) == {"function", "model", "access"} for u in uses)
    assert text(ORM, uses[0]) == "select(Order)"
    assert text(ORM, uses[3]) == "session.query(Customer)"
    assert text(ORM, uses[5]) == "Customer(email=email)"
    assert text(ORM, uses[7]) == "insert(Order)"


def test_dynamic_sql():
    r = run(ORM)
    found = of_kind(r, "dynamic_sql")
    assert [d.payload for d in found] == [{"function": "count_rows"}, {"function": "more_sql"}, {"function": "more_sql"}, {"function": "more_sql"}]
    assert [text(ORM, d) for d in found] == [
        'text(f"SELECT count(*) FROM {table_name}")',
        'conn.execute("SELECT * FROM " + name)',
        'conn.exec_driver_sql("SELECT * FROM %s" % name)',
        'text("SELECT * FROM {}".format(name))',
    ]


TESTS = """import pytest


def test_top(client, db):
    assert True


def helper():
    pass


@pytest.mark.slow
async def test_decorated():
    pass


class TestThing:
    def test_method(self):
        pass

    def helper(self):
        pass


class Other:
    def test_not_collected(self):
        pass
"""


def test_test_observations():
    r = run(TESTS, "tests/test_things.py")
    found = of_kind(r, "test")
    assert [t.payload for t in found] == [{"name": "test_top"}, {"name": "test_decorated"}, {"name": "TestThing.test_method"}]
    assert [text(TESTS, t) for t in found] == ["test_top(client, db)", "test_decorated()", "test_method(self)"]
    assert len(of_kind(run(TESTS, "pkg/things_test.py"), "test")) == 3
    assert not of_kind(run(TESTS, "pkg/things.py"), "test")
    assert not of_kind(run(TESTS, "pkg/contest_helpers.py"), "test")


def test_linker_observations_on_fixtures():
    from repo_intel.fixtures import FIXTURES_DIR

    def uses(rel):
        r = X.extract(rel, (FIXTURES_DIR / rel).read_bytes(), BLOB)
        return r, [(u.payload["function"], u.payload["model"], u.payload["access"]) for u in of_kind(r, "orm_use")]

    r, got = uses("F09-data-heavy/base/app/repo.py")
    assert got == [("orders_for", "Order", "read"), ("add_customer", "Customer", "write")]
    assert [d.payload for d in of_kind(r, "dynamic_sql")] == [{"function": "count_rows"}]
    r, got = uses("F01-python-api/base/app/main.py")
    assert got == [("list_customers", "Customer", "read"), ("create_customer", "Customer", "write")]
    r, _ = uses("F01-python-api/base/tests/test_customers.py")
    assert [t.payload for t in of_kind(r, "test")] == [{"name": "test_handlers_exist"}]


def test_linker_payload_keys():
    r = run(FASTAPI)
    assert all(set(s.payload) == {"name", "symbol_kind"} for s in of_kind(r, "symbol"))
    assert all(set(i.payload) == {"module", "names", "level"} for i in of_kind(r, "import"))
    assert all(set(h.payload) == {"method", "route", "handler", "router"} for h in of_kind(r, "route_handler"))
    assert all(set(c.payload) == {"caller", "callee"} for c in of_kind(r, "call"))


SQLMODEL = """import uuid
from typing import ClassVar

from sqlmodel import Field, Relationship, SQLModel


class Timestamped(SQLModel):
    created_at: str | None = None


class UserBase(Timestamped):
    email: str = Field(unique=True, index=True)
    is_active: bool = True


class UserCreate(UserBase):
    password: str


class User(UserBase, Remote, table=True):
    __hidden__: int = 1
    registry: ClassVar[dict] = {}
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    hashed_password: str
    email: str = Field(max_length=10)
    items: list["Item"] = Relationship(back_populates="owner")
    untyped = 3


class HeroTeamLink(SQLModel, table=True):
    __tablename__ = "hero_team"
    hero_id: int = Field(foreign_key="hero.id", primary_key=True)


class Item(SQLModel, table=True):
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False
    )


class NotATable(SQLModel, table=False):
    x: int
"""


def test_sqlmodel_tables_and_fields():
    r = run(SQLMODEL)
    tables = entities(r, "Table")
    assert [(t.payload["natural_key"]["name"], t.payload["attributes"]) for t in tables] == [
        ("user", {"orm": "sqlmodel", "model": "User"}),
        ("hero_team", {"orm": "sqlmodel", "model": "HeroTeamLink"}),
        ("item", {"orm": "sqlmodel", "model": "Item"}),
    ]
    assert all(t.payload["natural_key"] == {"datastore": "default", "name": t.payload["name"]} for t in tables)
    assert text(SQLMODEL, tables[0]) == "User(UserBase, Remote, table=True)"  # default name: evidence is the class header
    assert text(SQLMODEL, tables[1]) == '__tablename__ = "hero_team"'
    fields = [(f.payload["natural_key"]["table"], f.payload["natural_key"]["name"], f.payload["attributes"].get("inherited_from")) for f in entities(r, "Field")]
    assert fields == [
        ("user", "id", None),
        ("user", "hashed_password", None),
        ("user", "email", None),  # the table class's own declaration wins over the inherited one
        ("user", "is_active", "UserBase"),
        ("user", "created_at", "Timestamped"),  # resolved recursively; Remote is not in this file
        ("hero_team", "hero_id", None),
        ("item", "owner_id", None),
    ]
    by_name = {(f.payload["natural_key"]["table"], f.payload["natural_key"]["name"]): f for f in entities(r, "Field")}
    assert by_name["user", "id"].payload["attributes"] == {"orm": "sqlmodel", "attribute": "id"}
    assert by_name["user", "is_active"].payload["attributes"] == {"orm": "sqlmodel", "attribute": "is_active", "inherited_from": "UserBase"}
    assert text(SQLMODEL, by_name["user", "email"]) == "email: str = Field(max_length=10)"
    assert text(SQLMODEL, by_name["user", "is_active"]) == "is_active: bool = True"
    assert text(SQLMODEL, by_name["item", "owner_id"]).startswith("owner_id: uuid.UUID = Field(") and text(SQLMODEL, by_name["item", "owner_id"]).endswith(")")
    contains = [(c.payload["subject"]["natural_key"]["name"], c.payload["object"]["natural_key"]["name"]) for c in relations(r, "CONTAINS")]
    assert contains == [(t, n) for t, n, _ in fields]
    # No secrets-by-accident: foreign_key targets and defaults stay out of payloads.
    assert "user.id" not in repr([o.payload for o in r.observations])


def test_table_true_ignored_without_sqlmodel_import():
    source = 'class User(Base, table=True):\n    id: int\n'
    r = run(source)
    assert not entities(r, "Table") and not entities(r, "Field")
    assert [t.payload["name"] for t in entities(run("import sqlmodel\n" + source), "Table")] == ["user"]


def test_sqlalchemy_models_unaffected_in_sqlmodel_file():
    r = run("from sqlmodel import SQLModel\n" + MODELS)
    assert [t.payload["attributes"] for t in entities(r, "Table")] == [{"orm": "sqlalchemy", "model": "Order"}]


SQLMODEL_USE = """from sqlmodel import Session, col, delete, func, select, update

from app.models import Item, ItemCreate, ItemPublic, User


def read_exec(session):
    return session.exec(select(User).where(User.email == "x")).first()


def read_get(session, id):
    return session.get(Item, id)


def read_count(session):
    statement = select(func.count()).select_from(Item)
    return session.exec(statement).one()


def write_ctor(session):
    user = User(email="x")
    session.add(user)


def write_validate(session, item_in: ItemCreate):
    item = Item.model_validate(item_in, update={"owner_id": 1})
    public = ItemPublic.model_validate(item)
    session.add(item)
    return public


def write_from_orm(session, data):
    session.add(Item.from_orm(data))


def write_statements(session):
    session.exec(delete(Item).where(col(Item.owner_id) == 1))
    session.exec(update(User).values(is_active=False))


def delete_loaded(session, id):
    item = session.get(Item, id)
    session.delete(item)


def delete_unknown(session, obj, other: int):
    session.delete(obj)
    session.add(other)


def update_param(*, session: Session, db_user: User, maybe: Item | None = None):
    db_user.sqlmodel_update({})
    session.add(db_user)
    session.delete(maybe)
"""


def test_sqlmodel_orm_use_idioms():
    r = run(SQLMODEL_USE)
    uses = of_kind(r, "orm_use")
    assert [(u.payload["function"], u.payload["model"], u.payload["access"]) for u in uses] == [
        ("read_exec", "User", "read"),
        ("read_get", "Item", "read"),
        ("read_count", "Item", "read"),
        ("write_ctor", "User", "write"),
        ("write_validate", "Item", "write"),
        ("write_from_orm", "Item", "write"),
        ("write_statements", "Item", "write"),
        ("write_statements", "User", "write"),
        ("delete_loaded", "Item", "read"),
        ("delete_loaded", "Item", "write"),
        ("update_param", "User", "write"),
        ("update_param", "Item", "write"),
    ]
    spans = [text(SQLMODEL_USE, u) for u in uses]
    assert spans[0] == "select(User)"
    assert spans[2] == "select(func.count()).select_from(Item)"
    assert spans[4] == 'Item.model_validate(item_in, update={"owner_id": 1})'
    assert spans[5] == "Item.from_orm(data)"
    assert spans[9] == "session.delete(item)"
    assert spans[10] == "session.add(db_user)"


def test_dropping_a_table_does_not_declare_it(tmp_path):
    """A migration that drops a table refers to it without bringing it into the graph."""
    import subprocess

    from repo_intel.pipeline import compile_commit, load_shards
    from repo_intel.store import ObjectStore

    repo = tmp_path / "repo"
    (repo / "migrations").mkdir(parents=True)
    (repo / "pyproject.toml").write_text('[project]\nname = "dropper"\nversion = "1"\n')
    (repo / "models.py").write_text('class Kept:\n    __tablename__ = "kept"\n    id: Mapped[int]\n')
    (repo / "migrations" / "0002.py").write_text('from alembic import op\n\nrevision = "0002"\ndown_revision = "0001"\n\n\ndef upgrade():\n    op.drop_table("legacy")\n    op.drop_table("kept")\n')
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], check=True)
    sha = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    store = ObjectStore(tmp_path / "store", "t")
    shards = load_shards(store, compile_commit(store, repo, "github.com/9", sha).manifest)
    tables = {e["name"] for s in shards.values() for e in s["entities"] if e["kind"] == "Table"}
    assert tables == {"kept"}, "a dropped table that nothing declares must not appear"
    migrates = [a for s in shards.values() for a in s["assertions"] if a["predicate"] == "MIGRATES"]
    assert len(migrates) == 1 and migrates[0]["qualifiers"]["op"] == "drop_table"
