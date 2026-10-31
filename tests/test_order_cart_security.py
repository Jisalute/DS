from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from threading import RLock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.order import cart, order


class CommerceDB:
    """Transactional SQL double; real InnoDB isolation still requires integration validation."""

    def __init__(self):
        self.skus = {
            11: {"id": 11, "product_id": 1, "price": Decimal("20"), "stock": 5},
            12: {"id": 12, "product_id": 1, "price": Decimal("30"), "stock": 5},
            21: {"id": 21, "product_id": 2, "price": Decimal("40"), "stock": 5},
        }
        self.cart = {}
        self.orders = []
        self.items = []
        self.lock = RLock()
        self.statements = []

    @contextmanager
    def connection(self):
        with self.lock:
            snapshot = deepcopy((self.skus, self.cart, self.orders, self.items))
            try:
                yield self
            except Exception:
                self.skus, self.cart, self.orders, self.items = snapshot
                raise

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def commit(self):
        pass

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.statements.append((sql, params))
        self.rows = []
        self.rowcount = 0
        if sql.startswith("SELECT 1 FROM users") or sql.startswith("SELECT 1 FROM products"):
            self.rows = [{"1": 1}]
        elif sql.startswith("SELECT id FROM product_skus WHERE product_id"):
            self.rows = [{"id": sku["id"]} for sku in self.skus.values() if sku["product_id"] == params[0]][:2]
        elif sql.startswith("SELECT id FROM product_skus WHERE id"):
            sku = self.skus.get(params[0])
            self.rows = [{"id": sku["id"]}] if sku and sku["product_id"] == params[1] else []
        elif sql.startswith("SELECT stock FROM product_skus"):
            sku = self.skus.get(params[0])
            self.rows = [deepcopy(sku)] if sku and sku["product_id"] == params[1] else []
        elif sql.startswith("INSERT INTO cart"):
            user, product, sku, qty, specs = params
            key = (user, product, sku)
            self.cart.setdefault(key, {"id": sku, "quantity": 0, "specifications": specs})
            self.cart[key]["quantity"] += qty
        elif sql.startswith("SELECT id, quantity FROM cart"):
            row = self.cart.get(params)
            self.rows = [deepcopy(row)] if row else []
        elif sql.startswith("DELETE FROM cart WHERE user_id"):
            if "id IN" in sql:
                self.cart = {k: v for k, v in self.cart.items()
                             if k[0] != params[0] or v["id"] not in params[1:]}
            else:
                self.cart.pop(params, None)
        elif sql.startswith("DELETE FROM cart WHERE id"):
            self.cart = {k: v for k, v in self.cart.items() if v["id"] != params[0]}
        elif sql.startswith("UPDATE cart SET quantity"):
            for row in self.cart.values():
                if row["id"] == params[1]:
                    row["quantity"] = params[0]
        elif sql.startswith("SELECT is_member_product"):
            self.rows = [{"is_member_product": 0, "user_id": 0, "cash_only": 0}]
        elif sql.startswith("SELECT c.id AS cart_id"):
            self.rows = [dict(cart_id=v["id"], product_id=k[1], sku_id=k[2], quantity=v["quantity"], price=None,
                              is_vip=0, merchant_id=0, cash_only=0) for k, v in self.cart.items() if k[0] == params[0]]
        elif sql.startswith("SELECT price, stock"):
            sku = self.skus.get(params[0])
            self.rows = [deepcopy(sku)] if sku and sku["product_id"] == params[1] else []
        elif sql.startswith("UPDATE product_skus SET stock = stock -"):
            qty, sku_id, product_id, guard = params
            sku = self.skus[sku_id]
            if sku["product_id"] == product_id and sku["stock"] >= guard:
                sku["stock"] -= qty
                self.rowcount = 1
        elif sql.startswith("INSERT INTO orders"):
            self.orders.append(params)
            self.lastrowid = len(self.orders)
        elif sql.startswith("INSERT INTO order_items"):
            self.items.append(params)
        elif sql.startswith("SELECT order_number, status"):
            pass
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")


@pytest.fixture
def db(monkeypatch):
    database = CommerceDB()
    monkeypatch.setattr(cart, "get_conn", database.connection)
    monkeypatch.setattr(order, "get_conn", database.connection)
    monkeypatch.setattr(order, "redis_client", None)
    return database


def create(items, **kwargs):
    return order.OrderManager.create(7, None, None, buy_now=True, buy_now_items=items,
                                     delivery_way="pickup", **kwargs)


@pytest.mark.parametrize("quantity", [0, -1, True, 1.5, "2", 2147483648])
@pytest.mark.parametrize("model", [cart.CartAdd, cart.CartDecrease, order.BuyNowItem])
def test_quantity_is_strict_positive_integer(model, quantity):
    with pytest.raises(ValidationError):
        model(user_id=7, product_id=1, sku_id=11, quantity=quantity)

def test_cart_snapshot_and_user_id_contract():
    item = {"product_id": "9007199254740993", "sku_id": "18446744073709551615", "quantity": 1}
    request = order.OrderCreate(user_id="9007199254740993", cart_snapshot=[item])
    assert request.user_id == 9007199254740993
    assert cart.CartAdd(user_id="9007199254740993", product_id=1).user_id == request.user_id
    with pytest.raises(ValidationError):
        order.OrderCreate(user_id=9007199254740993, cart_snapshot=[item])
    for invalid in ({**item, "sku_id": "18446744073709551616"},
                    {**item, "product_id": 1}, {**item, "quantity": True},
                    {**item, "price": "0.01"}):
        with pytest.raises(ValidationError):
            order.OrderCreate(user_id="1", cart_snapshot=[invalid])
    with pytest.raises(ValidationError):
        order.OrderCreate(user_id="1", cart_snapshot=[item, item])
    with pytest.raises(ValidationError):
        order.OrderCreate(user_id="1")


def test_cart_response_preserves_numeric_fields_and_adds_exact_strings(monkeypatch):
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params): pass
        def fetchall(self):
            return [{"user_id": 9007199254740993, "product_id": 9007199254740995,
                     "sku_id": 9007199254740997, "unit_price": Decimal("1"),
                     "total_price": Decimal("1"), "specifications": None}]
    class Connection:
        def cursor(self): return Cursor()
    @contextmanager
    def connection():
        yield Connection()
    monkeypatch.setattr(cart, "get_conn", connection)
    item = cart.CartManager.list_items(9007199254740993)[0]
    assert item["user_id"] == 9007199254740993 and item["user_id_str"] == "9007199254740993"
    assert item["product_id"] == 9007199254740995 and item["product_id_str"] == "9007199254740995"
    assert item["sku_id"] == 9007199254740997 and item["sku_id_str"] == "9007199254740997"


@pytest.mark.parametrize("bad_id", [True, False, 1.0, "1.0", "01", "0", "-1",
                                    9007199254740992, "18446744073709551616"])
def test_buy_now_id_rejects_unsafe_values(bad_id):
    for field in ("product_id", "sku_id"):
        values = {"product_id": 1, "sku_id": 11, "quantity": 1, field: bad_id}
        with pytest.raises(ValidationError):
            order.BuyNowItem(**values)


def test_buy_now_id_accepts_exact_string_and_safe_integer():
    item = order.BuyNowItem(product_id="9007199254740993",
                            sku_id="18446744073709551615", quantity=1)
    assert item.product_id == 9007199254740993
    assert item.sku_id == 18446744073709551615
    assert order.BuyNowItem(product_id=1, sku_id=11, quantity=1).sku_id == 11


def test_cart_models_accept_precise_ids_and_reject_unsafe_numbers():
    for model in (cart.CartAdd, cart.CartDecrease):
        body = model(user_id="9007199254740993", product_id="9007199254740995",
                     sku_id="9007199254740997")
        assert (body.user_id, body.product_id, body.sku_id) == (
            9007199254740993, 9007199254740995, 9007199254740997)
        for field in ("user_id", "product_id", "sku_id"):
            values = {"user_id": "1", "product_id": "2", "sku_id": "3", field: 9007199254740993}
            with pytest.raises(ValidationError):
                model(**values)


def test_product_projection_adds_precise_ids():
    from api.product.routes import build_product_dict
    product = build_product_dict({"id": 9007199254740993},
                                 [{"id": 9007199254740995, "specifications": None}])
    assert product["id"] == 9007199254740993
    assert product["product_id_str"] == "9007199254740993"
    assert product["skus"][0]["id"] == 9007199254740995
    assert product["skus"][0]["sku_id_str"] == "9007199254740995"


@pytest.mark.parametrize("endpoint", ["home", "list", "search", "detail", "merchant"])
def test_product_read_endpoints_preserve_exact_ids(monkeypatch, endpoint):
    from api.product import routes
    product_id, sku_id = 9007199254740993, 9007199254740995
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params=()): self.sql = sql
        def fetchone(self):
            if "COUNT(" in self.sql:
                return {"total": 1}
            if "FROM users" in self.sql:
                return {"id": 1, "name": "merchant", "is_merchant": 1}
            return {"id": product_id, "name": "synthetic"}
        def fetchall(self):
            if "DYNAMIC_product_skus" in self.sql:
                return [{"id": sku_id, "sku_code": "synthetic", "price": Decimal("1"),
                         "original_price": None, "stock": 2, "specifications": None}]
            if "DYNAMIC_product_attributes" in self.sql:
                return []
            return [{"id": product_id, "name": "synthetic"}]
    class Connection:
        def cursor(self): return Cursor()
    @contextmanager
    def connection():
        yield Connection()
    monkeypatch.setattr(routes, "get_conn", connection)
    monkeypatch.setattr(routes, "build_dynamic_select", lambda cur, table, **kwargs: "DYNAMIC_" + table)
    if endpoint == "home":
        response = routes.get_home_products()
    elif endpoint == "list":
        response = routes.get_all_products(category=None, status=None, is_member_product=None,
                                           user_id=None, page=1, size=10)
    elif endpoint == "search":
        response = routes.search_products(keyword="synthetic")
    elif endpoint == "detail":
        response = routes.get_product(product_id)
    else:
        response = routes.get_user_products(user_id=1, status=None, page=1, size=10)
    item = response["data"] if endpoint == "detail" else response["data"][0]
    assert item["id"] == product_id and item["product_id_str"] == str(product_id)
    assert item["skus"][0]["id"] == sku_id
    assert item["skus"][0]["sku_id_str"] == str(sku_id)


def test_validated_large_buy_now_ids_survive_manager_revalidation(db):
    product, sku = 9007199254740993, 9007199254740995
    db.skus[sku] = {"id": sku, "product_id": product, "price": Decimal("20"), "stock": 2}
    request = order.OrderCreate(user_id="7", buy_now=True,
        buy_now_items=[{"product_id": str(product), "sku_id": str(sku), "quantity": 1}])
    result = order.OrderManager.create(request.user_id, None, None, buy_now=True,
        buy_now_items=request.buy_now_items, delivery_way="pickup")
    assert result["need_pay"] is True
    assert db.items[0][1:3] == (product, sku)


def test_order_number_is_32_ascii_bytes_for_large_user_id(db):
    result = order.OrderManager.create(18446744073709551615, None, None,
        buy_now=True, buy_now_items=[{"product_id": "1", "sku_id": "11", "quantity": 1}],
        delivery_way="pickup")
    number = result["order_number"]
    assert len(number.encode("ascii")) == 32



def test_cart_distinguishes_skus_and_scopes_decrease_delete(db):
    cart.CartManager.add(7, 1, 2, sku_id=11)
    cart.CartManager.add(7, 1, 3, sku_id=12)
    cart.CartManager.add(7, 1, 1, sku_id=11)
    cart.CartManager.decrease(7, 1, 1, sku_id=12)
    assert db.cart[(7, 1, 11)]["quantity"] == 3
    assert db.cart[(7, 1, 12)]["quantity"] == 2
    cart.CartManager.remove(7, 1, sku_id=11)
    assert set(db.cart) == {(7, 1, 12)}


def test_cart_missing_sku_rejects_ambiguity_and_foreign_sku(db):
    for sku_id in (None, 21):
        with pytest.raises(HTTPException) as error:
            cart.CartManager.add(7, 1, sku_id=sku_id)
        assert error.value.status_code == 422
    assert db.cart == {}
    cart.CartManager.add(7, 2)
    assert set(db.cart) == {(7, 2, 21)}


@pytest.mark.parametrize("client_price", [None, 0, "0.01", 999999])
def test_buy_now_ignores_client_price(db, client_price):
    item = {"product_id": 1, "sku_id": 12, "quantity": 2}
    if client_price is not None:
        item["price"] = client_price
    result = create([item])
    assert result["need_pay"] is True
    assert db.orders[0][3:5] == (Decimal("60"), Decimal("60"))
    assert db.items[0][4:] == (Decimal("30"), Decimal("60"))
    assert db.skus[12]["stock"] == 3
    assert db.skus[11]["stock"] == 5


def test_buy_now_rejects_wrong_sku_and_missing_multisku(db):
    for sku_id in (None, 21):
        with pytest.raises(HTTPException) as error:
            create([{"product_id": 1, "sku_id": sku_id, "quantity": 1}])
        assert error.value.status_code == 422
    assert db.orders == []
    assert db.skus[21]["stock"] == 5


def test_duplicate_lines_cannot_overdraw_and_transaction_rolls_back(db):
    item = {"product_id": 1, "sku_id": 11, "quantity": 3}
    with pytest.raises(HTTPException) as error:
        create([item, item])
    assert error.value.status_code == 400
    assert db.skus[11]["stock"] == 5
    assert db.orders == db.items == []


def test_cart_checkout_validates_legacy_quantity_and_sku(db):
    for sku_id, quantity in [(11, 0), (21, 1)]:
        db.cart = {(7, 1, sku_id): {"id": 1, "quantity": quantity}}
        with pytest.raises(HTTPException) as error:
            order.OrderManager.create(7, None, None, delivery_way="pickup",
                cart_snapshot=[{"product_id": "1", "sku_id": str(sku_id), "quantity": 1}])
        assert error.value.status_code == (409 if quantity == 0 else 422)
    assert db.orders == []




def test_later_validation_failure_releases_reserved_stock(db):
    with pytest.raises(HTTPException):
        order.OrderManager.create(7, None, None, buy_now=True,
                                  buy_now_items=[{"product_id": 1, "sku_id": 11, "quantity": 1}])
    assert db.skus[11]["stock"] == 5
    assert db.orders == db.items == []


@pytest.mark.parametrize("quantity", [False, "1", 0, -1, 1.5])
def test_order_request_validates_nested_quantity(quantity):
    with pytest.raises(ValidationError):
        order.OrderCreate(user_id=7, buy_now=True,
                          buy_now_items=[{"product_id": 1, "sku_id": 11, "quantity": quantity}])




def test_cart_rejects_cumulative_overstock_without_mutation(db):
    cart.CartManager.add(7, 1, 4, sku_id=11)
    with pytest.raises(HTTPException) as error:
        cart.CartManager.add(7, 1, 2, sku_id=11)
    assert error.value.status_code == 400
    assert db.cart[(7, 1, 11)]["quantity"] == 4
    assert db.skus[11]["stock"] == 5


def test_explicit_delete_removes_missing_sku_only_for_target_user(db):
    cart.CartManager.add(7, 1, 1, sku_id=11)
    cart.CartManager.add(8, 1, 1, sku_id=11)
    cart.CartManager.add(7, 1, 1, sku_id=12)
    del db.skus[11]
    cart.CartManager.remove(7, 1, sku_id=11)
    assert set(db.cart) == {(8, 1, 11), (7, 1, 12)}
    result = order.OrderManager.create(7, None, None, delivery_way="pickup",
        cart_snapshot=[{"product_id": "1", "sku_id": "12", "quantity": 1}])
    assert result["need_pay"] is True
    assert db.items[0][2] == 12
