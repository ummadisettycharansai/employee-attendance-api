from copy import deepcopy

import sample_seed


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.indexes = []

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))

    def update_one(self, query, update, upsert=False):
        existing = next((doc for doc in self.docs if all(doc.get(key) == value for key, value in query.items())), None)
        if existing is not None:
            return type("Result", (), {"upserted_id": None})()
        assert upsert is True
        self.docs.append(deepcopy(update["$setOnInsert"]))
        return type("Result", (), {"upserted_id": len(self.docs)})()


class FakeDatabase:
    name = "attendance_test"

    def __init__(self):
        self.employees = FakeCollection([{"emp_code": "EMP0001", "name": "Locally edited"}])
        self.attendance_logs = FakeCollection()

    def __getitem__(self, name):
        return getattr(self, name)


def test_sample_seed_is_idempotent_and_preserves_existing_records(monkeypatch):
    database = FakeDatabase()
    client = type("FakeClient", (), {"__getitem__": lambda self, name: database, "close": lambda self: None})()
    monkeypatch.setattr(sample_seed, "MongoClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(sample_seed, "load_dotenv", lambda: None)

    sample_seed.main()
    sample_seed.main()

    assert len(database.employees.docs) == 6
    assert len(database.attendance_logs.docs) == 9
    assert database.employees.docs[0]["name"] == "Locally edited"
    assert database.employees.indexes[0] == ("emp_code", {"unique": True})
    assert database.attendance_logs.indexes[0] == (
        [("emp_code", 1), ("date", 1)],
        {"unique": True},
    )
