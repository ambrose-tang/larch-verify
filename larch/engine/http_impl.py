"""The service under test, seen from the test driver: the same new/invoke/observe
interface as a class instance (see impl_client.ImplClient), over HTTP.

`new` resets the service (empties its database, calls its reset hook), which plays the
role of constructing a fresh object. `invoke` sends one request built from the
operation's HTTP mapping and returns the status code and the selected response fields
as one tuple, the shape the model's operation returns."""
from __future__ import annotations

import urllib.parse

from ..services import ServiceError, ServiceHandle


class HttpImpl:
    def __init__(self, job: dict):
        spec = job["impl"]
        self.svc = ServiceHandle.from_json(spec["service"])
        self.ops: dict = spec["operations"]
        self.client = self

    # the Impl interface used by the driver
    def start(self) -> None:
        pass

    def load(self, source: str | None = None) -> None:
        pass  # the service was started by the session (mutants run on their own copy)

    def close(self) -> None:
        pass

    # the ImplClient object interface used by seqdriver
    def new(self, args: list, timeout: float = 10.0) -> dict:
        try:
            self.svc.reset()
        except (ServiceError, OSError) as e:
            return {"status": "exception", "exc": f"ResetFailed: {e}"}
        return {"status": "ok"}

    def invoke(self, method: str, args: list, timeout: float) -> dict:
        op = self.ops[method]
        path, query, headers, body = op["path"], {}, {}, None
        for p, value in zip(op["params"], args):
            where, key = p["in"], p.get("key") or p["name"]
            if value is None:
                continue  # an absent optional parameter
            if where == "path":
                path = path.replace("{" + key + "}", urllib.parse.quote(str(value), safe=""))
            elif where == "query":
                query[key] = "true" if value is True else "false" if value is False else str(value)
            elif where == "header":
                headers[key] = str(value)
            else:
                body = body or {}
                body[key] = list(value) if isinstance(value, tuple) else value
        if query:
            path += "?" + urllib.parse.urlencode(query)
        try:
            status, resp = self.svc.request(op["verb"], path, body=body if body is not None or op.get("has_body") else None,
                                            headers=headers, timeout=max(5.0, timeout * 5))
        except OSError as e:
            return {"status": "exception", "exc": f"ConnectionError: {e}"}
        fields = [_get(resp, f["key"]) if 200 <= status < 300 or f.get("on_error") else None for f in op.get("fields", [])]
        value = (status, *fields) if fields else status
        return {"status": "ok", "value": value, "raw": {"status": status, "body": resp}}

    def observe(self, observers: list[dict], timeout: float) -> dict:
        return {"status": "ok", "observers": {}}


def _get(doc, key: str):
    """`a.b.0.c` into a JSON document; None if absent."""
    cur = doc
    for part in key.split(".") if key else []:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur
