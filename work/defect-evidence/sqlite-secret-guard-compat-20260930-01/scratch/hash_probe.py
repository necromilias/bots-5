import hashlib, sqlite3, sys
sys.path.insert(0,'src'); sys.path.insert(0,'.')
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from bots5.infrastructure.persistence.sqlite import _normalise_sql_fragment, _PHASE5_SCHEMA_SHA256, _PHASE5_SCHEMA_OBJECTS
from tests._authority_test_support import upgrade_to

def build(path, script_location):
    import os
    if os.path.exists(path): os.unlink(path)
    engine=create_engine(f"sqlite:///{path}", future=True)
    config=Config(); config.set_main_option("script_location", script_location)
    with engine.connect() as connection:
        config.attributes["connection"]=connection
        command.upgrade(config,"head")
    engine.dispose()

def hashes(path):
    con=sqlite3.connect(path)
    out={}
    for name, sql in con.execute("SELECT name, sql FROM sqlite_master WHERE name IN ({})".format(",".join("?"*len(_PHASE5_SCHEMA_OBJECTS))), _PHASE5_SCHEMA_OBJECTS):
        out[str(name)]=hashlib.sha256(_normalise_sql_fragment(str(sql or "")).encode()).hexdigest()
    con.close(); return out

mode=sys.argv[1]
if mode=="old":
    build(sys.argv[2], "work/baseline-t0/src/bots5/infrastructure/persistence/migrations")
else:
    build(sys.argv[2], "src/bots5/infrastructure/persistence/migrations")
h=hashes(sys.argv[2])
diff=[(n, _PHASE5_SCHEMA_SHA256.get(n), h.get(n)) for n in _PHASE5_SCHEMA_OBJECTS if _PHASE5_SCHEMA_SHA256.get(n)!=h.get(n)]
print(mode, "objects", len(_PHASE5_SCHEMA_OBJECTS), "hash mismatches vs table:", len(diff))
for n,exp,got in diff: print("  ",n,"\n     table:",exp,"\n     actual:",got)
