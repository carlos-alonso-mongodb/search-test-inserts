#!/usr/bin/env python3
"""
openEHR → CatSalut CDR ingestor  (v12.1  2025-05-01)

• atomic global _codes (with safety re-insert of _id:"ar_code")
• full key/value shortcut pass (deep)
• replication factor, safe inserts
• detailed per-EHR logging
"""

import os, json, logging, threading, certifi, traceback, time
from datetime import datetime
from typing import Any, Dict, List, Tuple, Set
from bson     import ObjectId
from pymongo  import MongoClient, ReturnDocument, errors
from pymongo.database import Database
from pymongo.errors import BulkWriteError, DuplicateKeyError
from pymongo.collection import Collection
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import freeze_support
import re

# ───────── logging ─────────
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL","INFO").upper()),
    format="[%(processName)s %(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("pymongo").setLevel(logging.WARNING)

# ───────── globals shared by workers ─────────
CACHE_LOCK = threading.Lock()

LOCATABLE = {                   # quick membership test
    "COMPOSITION","SECTION","ADMIN_ENTRY","OBSERVATION","EVALUATION",
    "INSTRUCTION","ACTION","CLUSTER","ITEM_TREE","ITEM_LIST","ITEM_SINGLE",
    "ITEM_TABLE","ELEMENT","HISTORY","EVENT","POINT_EVENT","INTERVAL_EVENT",
    "ACTIVITY","ISM_TRANSITION","INSTRUCTION_DETAILS","CARE_ENTRY",
    "PARTY_PROXY","EVENT_CONTEXT"
}

NON_ARCHETYPED_RM = {                     
    "HISTORY", "EVENT", "POINT_EVENT", "INTERVAL_EVENT",
    "ISM_TRANSITION",
}

LOC_HINT = {"archetype_node_id", "archetype_details"}
SKIP_ATTRS = {"archetype_details", "uid"}

SHORTCUT_KEYS : Dict[str,str] = {}
SHORTCUT_VALS : Dict[str,str] = {}
alloc_code = None

cfg_global: dict = {}         
_local = threading.local()

CODE_BOOK   : dict[str, dict[str, int]] = {}   # e.g. CODE_BOOK["ar_code"][sid] = n
SEQ         : dict[str, int]            = {"ar_code": 0}

# ═════════ config ═════════
def cfg(path="config.json"):
    with open(path,encoding="utf-8") as f:
        return json.load(f)

# ═════════ _codes allocator ═════════
def load_codes_from_db(codes_col: Collection):
    """
    Fetch the “ar_code” document and reload CODE_BOOK["ar_code"] and SEQ["ar_code"].
    """
    doc = codes_col.find_one({"_id": "ar_code"}) or {}
    book: dict[str, int] = {}

    # Reconstruct only the nested rm → name → ver → code entries
    for rm, subtree in doc.items():
        if rm in ("_id", "_max", "unknown"):
            continue
        if not isinstance(subtree, dict):
            continue
        for name, vers in subtree.items():
            if not isinstance(vers, dict):
                continue
            for ver, code in vers.items():
                # e.g. key "ehr-ehr-composition.report.v1" → code
                book[f"{rm}.{name}.{ver}"] = code

    # preserve the special “unknown” code if present
    if "unknown" in doc and isinstance(doc["unknown"], int):
        book["unknown"] = doc["unknown"]

    with CACHE_LOCK:
        CODE_BOOK["ar_code"] = book
        SEQ["ar_code"] = doc.get("_max", SEQ.get("ar_code", 0))

def _primary_flush_loop(tgt_coll: Collection, cfg: dict):
    interval = cfg["codes_refresh_interval"]
    while True:
        time.sleep(interval)
        try:
            # pass in the Database, not the Collection or MongoClient
            flush_globals_into_db(tgt_coll.database, cfg)
            logging.info("✅ Primary: flushed _codes to %r", cfg["target"]["codes_collection"])
        except Exception:
            logging.exception("❌ Primary: failed to flush _codes")

def _secondary_reload_loop(tgt_coll: Collection, cfg: dict):
    interval = cfg["codes_refresh_interval"]
    while True:
        time.sleep(interval)
        try:
            load_codes_from_db(tgt_coll.database[cfg["target"]["codes_collection"]])
            logging.info("🔄 Secondary: reloaded _codes from server")
        except Exception:
            logging.exception("❌ Secondary: failed to reload _codes")

def make_allocator(_: str):
    """
    In-memory allocator.
    • primary: allocates any new non-at… codes into CODE_BOOK/SEQ
    • secondary: if sees a non-at… sid not in CODE_BOOK, marks the doc as bad
                 and returns None, so that composition is skipped.
    """
    def alloc(key: str, sid: str) -> int|None:
            sid_lower = sid.lower()
            if key == "ar_code" and sid_lower.startswith("at"):
                n = at_code_to_int(sid)
                if n is not None:
                    return n

            book = CODE_BOOK.setdefault(key,{})
            if sid in book:
                return book[sid]

            if cfg_global.get("role") != "primary":
                _local.bad_code = True
                return None

            SEQ.setdefault(key,0)
            SEQ[key] += 1
            code = SEQ[key]
            book[sid] = code
            return code

    return alloc

def bootstrap_codes(db, coll_name):
    col = db[coll_name]
    col.update_one({"_id":"ar_code"},
                   {"$setOnInsert": {"_id":"ar_code"}},
                   upsert=True)
    col.update_one({"_id":"sequence"},
                   {"$setOnInsert": {"_id":"sequence","seq": {"ar_code":0}}},
                   upsert=True)

# ═════════ shortcut expansion ═══════════
def apply_sc(o):
    if isinstance(o,dict):
        return {SHORTCUT_KEYS.get(k,k):apply_sc(v) for k,v in o.items()}
    if isinstance(o,list):
        return [apply_sc(x) for x in o]
    if isinstance(o,str):
        if o in SHORTCUT_VALS:
            return SHORTCUT_VALS[o]
    return o
     
# ───────── claim & lock a patient ─────────
def claim_patient_id(coll) -> str | None:
    """
    Atomically pick exactly one still-unused document (used=False → True)
       and return its ehr_id.
    Then mark *all* documents with that ehr_id used=True so no one
       else can pick the same patient.
    """
    doc = coll.find_one_and_update(
        {"used": {"$ne": True}},    
        {"$set": {"used": True}},
        sort=[("_id", 1)],
        projection={"ehr_id": 1},
    )
    if not doc:
        return None

    eid = doc["ehr_id"]
    coll.update_many(
        {"ehr_id": eid, "used": False},
        {"$set": {"used": True}}
    )
    return eid

def next_patient_id(src_coll, *, stop_after:int):
    """Generator that hands out up to `stop_after` patient IDs."""
    issued = 0
    while issued < stop_after:
        eid = claim_patient_id(src_coll)
        if eid is None:
            break
        issued += 1
        yield eid

# ═════════ at‑code → int ════════════════════════════════════════

# major=any digits, optional .fraction=any digits
_AT_RE = re.compile(r"""^
    at0*([0-9]+)        # group(1) = major
    (?:\.([0-9]+))?     # optional .fraction in group(2)
$""", re.IGNORECASE | re.VERBOSE)

def at_code_to_int(at: str) -> int | None:
    at_norm = at.strip().lower()
    m = _AT_RE.match(at_norm)
    if not m:
        if at_norm.startswith("at"):
            logging.warning("at_code_to_int failed to parse archetype_node_id %r.", at)
        return None     # truly un-parsable → fallback
    major = int(m.group(1), 10)
    frac  = (m.group(2) or "").ljust(6, "0")[:6]
    return major * 1_000_000 + int(frac, 10)

def archetype_id(obj: dict) -> str | None:
    # 1️⃣ look in archetype_details.archetype_id.value
    ad = obj.get("archetype_details") or {}
    ai = ad.get("archetype_id")
    if isinstance(ai, dict) and ai.get("value"):
        return ai["value"].strip()
    if isinstance(ai, str) and ai.strip():
        return ai.strip()

    # 2️⃣ fallback to the direct archetype_node_id property if present
    ani = obj.get("archetype_node_id")
    if isinstance(ani, str) and ani.strip():
        return ani.strip()

    return None


def is_locatable(obj: dict) -> bool:
    if not isinstance(obj, dict):
        return False

    # only test membership if _type is actually a str
    t = obj.get("_type")
    if isinstance(t, str) and t in LOCATABLE:
        return True

    # fallback: any of our “hints” in the dict keys
    return any(k in obj for k in LOC_HINT)

# ────────────────────────────────────────────────────────────────────
#  Flatten a composition
# ────────────────────────────────────────────────────────────────────
def walk(node: dict,
         ancestors: Tuple[int, ...],
         cn: List[dict],
         *,
         inside_list: bool,
         attr_key: str | None = None) -> None:
    """
    • A new `cn` entry is created **only** when the node lives inside a list
      (i.e. it was an array element) _or_ it is the root COMPOSITION.
    • `ap` stores *ancestors only*; there is no `pp`.
    • While copying scalars into the parent pocket, *any* attribute is
      skipped when it
         – is `archetype_details`, or
         – is a **dict** that is a Locatable, or
         – is a **list** that contains (at least one) Locatable.
    """
    # ── decide whether this node gets its own record ───────────────
    aid   = archetype_id(node)
    code  = None
    if aid and aid.startswith("at"):
        code = at_code_to_int(aid)
        # **don’t** ever do `alloc_code("ar_code", aid)` if this was an “at…” string
    elif aid:
        # everything else (the proper URIs, openEHR-EHR-CLUSTER… etc)
        code = alloc_code("ar_code", aid)

    is_root  = not ancestors
    emit     = (inside_list or is_root) and code is not None

    # ── pocket the scalar attributes (only if we are *emitting*) ───
    if emit:
        scalars: Dict[str, Any] = {}
        for k, v in node.items():
            if k in SKIP_ATTRS:
                continue
            if isinstance(v, dict) and is_locatable(v):
                continue                              # drop entire sub‑object
            if isinstance(v, list) and any(is_locatable(x) for x in v):
                continue                              # drop locatable array

            scalars[k] = v

        d = apply_sc(scalars)
        d["ani"] = code
        if attr_key:
            d["ak"] = SHORTCUT_KEYS.get(attr_key, attr_key)
        rt = node.get("_type")
        if rt and rt in SHORTCUT_VALS:
            d["T"] = SHORTCUT_VALS[rt]

        cn.append({"ap": list(ancestors), "d": d})

    # ── recurse ────────────────────────────────────────────────────
    new_anc = ancestors + ((code,) if emit else ())

    # ① first: dive into **dict** attributes that are themselves Locatables
    for k, v in node.items():
        if isinstance(v, dict) and is_locatable(v):
            walk(v, new_anc, cn,
                 inside_list=False, attr_key=k)

    # ② second: dive into **list** attributes – each locatable element
    #            becomes `inside_list=True`
    for k, v in node.items():
        if isinstance(v, list):
            for it in v:
                if is_locatable(it):
                    walk(it, new_anc, cn,
                         inside_list=True, attr_key=k)

# ──────────────────────────────────────────────────────────────────
#  transform() – thin wrapper around walk()
# ──────────────────────────────────────────────────────────────────
def transform(raw: dict) -> tuple[dict,str] | None:
    comp     = raw["comp"]
    root_aid = archetype_id(comp) or "unknown"
    template = alloc_code("ar_code", root_aid)

    cn: list[dict] = []
    walk(comp, (), cn, inside_list=False)

    if _local.bad_code and cfg_global.get("role") != "primary":
        return None

    return {
      "_id":         raw["_id"],
      "ehr_id":      raw["ehr_id"],
      "comp_id":     raw.get("comp_id"),
      "version":     raw.get("version"),
      "template_id": template,
      "cn":          cn,
    }, root_aid

# ═════════ safe insert ═══════════════════
def safe_insert(col,docs):
    if not docs: return 0
    try: return len(col.insert_many(docs,ordered=False).inserted_ids)
    except BulkWriteError as bwe:
        dup=sum(1 for e in bwe.details.get("writeErrors",[]) if e["code"]==11000)
        other=[e for e in bwe.details.get("writeErrors",[]) if e["code"]!=11000]
        if other: raise
        return len(docs)-dup

# ═════════ batch flag reset ═══════════════
def reset_used(coll,batch):
    while True:
        ids=[d["_id"] for d in coll.find({"used":True},{"_id":1}).limit(batch)]
        if not ids: break
        coll.update_many({"_id":{"$in":ids}},{"$unset":{"used":""}})

# ═════════ worker init ════════════════════
def init_worker(cfg: dict, ca: str) -> None:
    """
    Connects to Mongo, primes the shortcut tables and installs the
    in‑memory *alloc_code* function.  
    """
    global SRC, TGT, alloc_code

    src_cli = MongoClient(cfg["source"]["connection_string"], tlsCAFile=ca)
    tgt_cli = MongoClient(cfg["target"]["connection_string"], tlsCAFile=ca)

    SRC = src_cli[cfg["source"]["database_name"]][
        cfg["source"]["collection_name"]
    ]
    TGT = tgt_cli[cfg["target"]["database_name"]][
        cfg["target"]["compositions_collection"]
    ]

    alloc_code = make_allocator("")      # now purely in‑memory

    # ── populate the shortcut tables (keys / values) ───────────────────
    sc = tgt_cli[cfg["target"]["database_name"]][
        cfg["target"]["shortcuts_collection"]
    ].find_one({"_id": "shortcuts"}) or {}

    SHORTCUT_KEYS.update(sc.get("keys",   {}))
    SHORTCUT_VALS.update(sc.get("values", {}))

# ═════════ per-patient job ════════════════
def proc(eid, cfg):
    """
    Fetch all raw parts for this patient, transform each.
    If transform() returned None (secondary + unknown code), skip that composition.
    """
    try:
        raws = list(SRC.find({"ehr_id": eid},
                             {"_id":1, "ehr_id":1, "comp":1, "comp_id":1, "version":1}))
        if not raws:
            return eid, 0, 0, []

        rf = cfg.get("replication_factor", 1)
        docs, tpl_ids = [], set()

        for r in raws:
            _local.bad_code = False
            result = transform(r)

            base, tuid = result
            tpl_ids.add(tuid)

            for i in range(rf):
                d = base.copy()
                d["_id"] = ObjectId()
                d["ehr_id"] = f"{eid}~r{i+1}"
                docs.append(d)

        ins = safe_insert(TGT, docs)
        return eid, len(raws), ins, []

    except Exception:
        return eid, 0, 0, [traceback.format_exc()]

# ────────────────────────────────────────────────────────────────────────
#  Flush the in‑memory dictionaries into MongoDB
# ────────────────────────────────────────────────────────────────────────
def flush_globals_into_db(db: Database, cfg: dict) -> None:
    """
    Persists CODE_BOOK and SEQ into the target codes_collection.
    Writes two documents:
      - _id="ar_code": nested {rm: {name: {ver: code}}} plus "_max"
      - _id="sequence": {"seq": SEQ}
    """
    codes_col = db[cfg["target"]["codes_collection"]]

    # Build a nested dict {rm: {name: {ver: code}}} from CODE_BOOK["ar_code"]
    book     = CODE_BOOK.get("ar_code", {})
    nested   : dict[str,Any] = {}
    max_code = 0

    for sid, code in book.items():
        if code > max_code:
            max_code = code

        if sid == "unknown":
            nested["unknown"] = code
            continue

        parts = sid.split(".", 2)
        if len(parts) != 3:
            nested[sid] = code
            continue

        rm, name, ver = parts
        nested.setdefault(rm, {}) \
              .setdefault(name, {})[ver] = code

    nested["_max"] = max_code

    # Replace the _id="ar_code" doc
    codes_col.replace_one(
        {"_id": "ar_code"},
        {"_id": "ar_code", **nested},
        upsert=True
    )
    # Replace the _id="sequence" doc
    codes_col.replace_one(
        {"_id": "sequence"},
        {"_id": "sequence", "seq": SEQ},
        upsert=True
    )
            
# ═════════ main ═══════════════════════════
def main():
    global alloc_code, cfg_global
    c  = cfg()
    ca = certifi.where()
    cfg_global = c
    alloc_code = make_allocator("") 

    # ───── optional clean / reset steps (unchanged) ─────
    if c.get("clean_collections", False):
        tgt = MongoClient(c["target"]["connection_string"], tlsCAFile=ca)
        db  = tgt[c["target"]["database_name"]]
        logging.info("🧹 dropping target collections")
        for n in ("compositions_collection",
                  "codes_collection"):
            db[c["target"][n]].drop()
        tgt.close()

    if c.get("reset_used_flags", False):
        src = MongoClient(c["source"]["connection_string"], tlsCAFile=ca)
        logging.info("🧹 resetting source.used flags")
        reset_used(
            src[c["source"]["database_name"]][c["source"]["collection_name"]],
            c.get("batch_size_reset", 2500),
        )
        src.close()

    # ───── bootstrap the three seed documents once ─────
    tgt = MongoClient(c["target"]["connection_string"], tlsCAFile=ca)
    bootstrap_codes(
        tgt[c["target"]["database_name"]], c["target"]["codes_collection"]
    )
    tgt.close()

    # ───── ‹S E Q U E N T I A L› ingestion – no workers ─────
    init_worker(c, ca)                       # we still reuse the worker‑init

    load_codes_from_db(TGT.database[c["target"]["codes_collection"]])
    logging.info("Loaded %d existing ar_codes before restart",
             len(CODE_BOOK.get("ar_code", {})))
    role = c.get("role", "primary").lower()
    cfg_global["role"] = role         
    if role == "primary":
        threading.Thread(
            target=_primary_flush_loop,
            args=(TGT, c),
            daemon=True
        ).start()
    else:
        # load once immediately, then refresh
        load_codes_from_db(TGT.database[c["target"]["codes_collection"]])
        threading.Thread(
            target=_secondary_reload_loop,
            args=(TGT, c),
            daemon=True
        ).start()
    logging.info("🔖 Started in %s role; codes will refresh every %d seconds",
                 role, c["codes_refresh_interval"])
                 
    src_coll = SRC                           # set by init_worker
    plimit   = c.get("patient_limit", 500)

    logging.info("🏃 single‑worker run, up to %d patients", plimit)

    done = fail = total_read = total_ins = 0
    for count, eid in enumerate(next_patient_id(SRC, stop_after=plimit), start=1):
        eid, read, ins, errs = proc(eid, c)
        total_read += read
        total_ins  += ins
        if errs:
            fail += 1
            logging.error("✖ %s failed\n%s", eid, errs[0])
        else:
            done += 1

        if count % 25 == 0:
            logging.info("… processed %d / %d", count, plimit)

    logging.info("✓ done %d  ✖ failed %d   source %d  inserted %d",
                 done, fail, total_read, total_ins)

    tgt_cli = MongoClient(c["target"]["connection_string"], tlsCAFile=ca)
    db      = tgt_cli[c["target"]["database_name"]]
    flush_globals_into_db(db, c)
    logging.info("ar_code next value will be %d", SEQ["ar_code"] + 1)
    tgt_cli.close()
    
if __name__=="__main__":
    freeze_support(); main()