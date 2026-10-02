"""Orders and store-credit wallets over HTTP (FastAPI + Postgres).

    POST /wallets/{customer}/deposit   {"amount_cents": int}        -> 200 {"customer", "balance_cents"}
    GET  /wallets/{customer}                                         -> 200 {"customer", "balance_cents"}
    POST /orders  {"customer": str, "amount_cents": int}            -> 201 {"id", "customer", "amount_cents", "status"}
         header Idempotency-Key: retrying with the same key returns the original order (200)
         instead of creating another one, whatever has happened to the order since.
    GET  /orders/{id}                                                -> 200 order | 404
    POST /orders/{id}/pay                                            -> 200 order (status "paid") | 404
         402 if the wallet holds less than the amount | 409 if the order is not pending.
         Paying debits the customer's wallet by the order amount exactly once.

The database URL comes from DATABASE_URL. Amounts are positive integer cents (422 otherwise).
"""
from __future__ import annotations

import os

import psycopg
from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

app = FastAPI(title="shop")

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (customer TEXT PRIMARY KEY, balance_cents BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY, customer TEXT NOT NULL, amount_cents BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', idempotency_key TEXT
);
"""


def db() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


@app.on_event("startup")
def migrate() -> None:
    with db() as c:
        c.execute(SCHEMA)


class Deposit(BaseModel):
    amount_cents: int = Field(gt=0)


class NewOrder(BaseModel):
    customer: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)


def _order(row) -> dict:
    return {"id": row[0], "customer": row[1], "amount_cents": row[2], "status": row[3]}


def _balance(c: psycopg.Connection, customer: str) -> int:
    row = c.execute("SELECT balance_cents FROM wallets WHERE customer = %s", (customer,)).fetchone()
    return row[0] if row else 0


@app.post("/wallets/{customer}/deposit")
def deposit(customer: str, body: Deposit) -> dict:
    with db() as c:
        c.execute(
            "INSERT INTO wallets (customer, balance_cents) VALUES (%s, %s) "
            "ON CONFLICT (customer) DO UPDATE SET balance_cents = wallets.balance_cents + EXCLUDED.balance_cents",
            (customer, body.amount_cents),
        )
        return {"customer": customer, "balance_cents": _balance(c, customer)}


@app.get("/wallets/{customer}")
def wallet(customer: str) -> dict:
    with db() as c:
        return {"customer": customer, "balance_cents": _balance(c, customer)}


@app.post("/orders", status_code=201)
def create_order(body: NewOrder, response: Response, idempotency_key: str | None = Header(default=None)) -> dict:
    with db() as c:
        if idempotency_key:
            row = c.execute(
                "SELECT id, customer, amount_cents, status FROM orders WHERE idempotency_key = %s AND status = 'pending'",
                (idempotency_key,),
            ).fetchone()
            if row:
                response.status_code = 200
                return _order(row)
        row = c.execute(
            "INSERT INTO orders (customer, amount_cents, idempotency_key) VALUES (%s, %s, %s) "
            "RETURNING id, customer, amount_cents, status",
            (body.customer, body.amount_cents, idempotency_key),
        ).fetchone()
        return _order(row)


@app.get("/orders/{order_id}")
def get_order(order_id: int) -> dict:
    with db() as c:
        row = c.execute("SELECT id, customer, amount_cents, status FROM orders WHERE id = %s", (order_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such order")
    return _order(row)


@app.post("/orders/{order_id}/pay")
def pay(order_id: int) -> dict:
    with db() as c:
        row = c.execute("SELECT id, customer, amount_cents, status FROM orders WHERE id = %s", (order_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such order")
        order = _order(row)
        if _balance(c, order["customer"]) < order["amount_cents"]:
            raise HTTPException(402, "insufficient store credit")
        c.execute("UPDATE wallets SET balance_cents = balance_cents - %s WHERE customer = %s",
                  (order["amount_cents"], order["customer"]))
        if order["status"] != "pending":
            raise HTTPException(409, "order is not pending")
        c.execute("UPDATE orders SET status = 'paid' WHERE id = %s", (order_id,))
        order["status"] = "paid"
        return order
