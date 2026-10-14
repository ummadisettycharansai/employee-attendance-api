"""Insert sample documents without replacing or deleting existing attendance data.

Usage: python sample_seed.py (reads MONGO_URI / MONGO_DB from the environment or .env)

Existing documents with the same natural key are left unchanged. Remove sample
documents manually only when an intentional reset is required.
"""
import os
import pathlib

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient


def main() -> None:
    load_dotenv()
    here = pathlib.Path(__file__).parent / "sample_data"
    options = json_util.JSONOptions(tz_aware=True)
    client = MongoClient(os.getenv("MONGO_URI"), tz_aware=True, serverSelectionTimeoutMS=5000)
    db = client[os.getenv("MONGO_DB", "attendance_db")]
    try:
        db.employees.create_index("emp_code", unique=True)
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        for name, keys in (
            ("employees", ("emp_code",)),
            ("attendance_logs", ("emp_code", "date")),
        ):
            docs = json_util.loads((here / f"{name}.json").read_text(encoding="utf-8"), json_options=options)
            inserted = 0
            unchanged = 0
            for doc in docs:
                filter_doc = {key: doc[key] for key in keys}
                result = db[name].update_one(filter_doc, {"$setOnInsert": doc}, upsert=True)
                if result.upserted_id is None:
                    unchanged += 1
                else:
                    inserted += 1
            print(f"{name}: inserted {inserted}, left existing unchanged {unchanged} (source documents: {len(docs)})")
    finally:
        client.close()


if __name__ == "__main__":
    main()
