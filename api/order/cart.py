from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel, Field, BeforeValidator
from core.auth import get_current_user
from core.database import get_conn
from typing import List, Dict, Any, Optional, Annotated
import json

router = APIRouter()

def parse_bigint_id(value):
    if type(value) is int:
        if not 1 <= value <= 9007199254740991:
            raise ValueError("数字 ID 必须在 JavaScript 安全整数范围内")
        return value
    if isinstance(value, str) and len(value) <= 20 and value.isascii() and value.isdigit() and value[0] != "0":
        result = int(value)
        if result <= 18446744073709551615:
            return result
    raise ValueError("ID 必须是有效的正十进制 ID")


SafeBigintId = Annotated[int, BeforeValidator(
    parse_bigint_id,
    json_schema_input_type=Annotated[int, Field(ge=1, le=9007199254740991)] |
                           Annotated[str, Field(pattern=r"^[1-9][0-9]*$", max_length=20)],
)]

def validate_quantity(quantity: int) -> None:
    if type(quantity) is not int or not 1 <= quantity <= 2147483647:
        raise HTTPException(status_code=422, detail="quantity 必须是 1..2147483647 的整数")


def resolve_sku(cur, product_id: int, sku_id: Optional[int]) -> int:
    if sku_id is None:
        cur.execute("SELECT id FROM product_skus WHERE product_id = %s ORDER BY id LIMIT 2", (product_id,))
        rows = cur.fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="商品无可用 SKU")
        if len(rows) != 1:
            raise HTTPException(status_code=422, detail="多 SKU 商品必须提供 sku_id")
        return rows[0]["id"]
    cur.execute("SELECT id FROM product_skus WHERE id = %s AND product_id = %s", (sku_id, product_id))
    if not cur.fetchone():
        raise HTTPException(status_code=422, detail="sku_id 不存在或不属于该商品")
    return sku_id

class CartManager:
    @staticmethod
    def add(
        user_id: int,
        product_id: int,
        quantity: int = 1,
        specifications: Optional[Dict[str, Any]] = None,
        sku_id: Optional[int] = None,
    ) -> bool:
        validate_quantity(quantity)
        with get_conn() as conn:
            with conn.cursor() as cur:
                # The owner row exists even when the first cart row does not.
                cur.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (user_id,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail=f"users 表中不存在 id={user_id}")

                # 2. 商品是否存在
                cur.execute("SELECT 1 FROM products WHERE id = %s", (product_id,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail=f"products 表中不存在 id={product_id}")

                sku_id = resolve_sku(cur, product_id, sku_id)
                cur.execute(
                    "SELECT id, quantity FROM cart WHERE user_id=%s AND product_id=%s AND sku_id=%s FOR UPDATE",
                    (user_id, product_id, sku_id),
                )
                existing = cur.fetchone()
                current_quantity = existing["quantity"] if existing else 0
                if existing:
                    validate_quantity(current_quantity)
                new_quantity = current_quantity + quantity
                validate_quantity(new_quantity)
                cur.execute(
                    "SELECT stock FROM product_skus WHERE id=%s AND product_id=%s FOR UPDATE",
                    (sku_id, product_id),
                )
                sku = cur.fetchone()
                if not sku or sku["stock"] is None or sku["stock"] < new_quantity:
                    raise HTTPException(status_code=400, detail="购物车累计数量超过 SKU 可用库存")
                spec_str = json.dumps(specifications, ensure_ascii=False) if specifications else None
                cur.execute(
                    "INSERT INTO cart(user_id, product_id, sku_id, quantity, specifications) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE quantity = quantity + VALUES(quantity), "
                    "specifications = VALUES(specifications)",
                    (user_id, product_id, sku_id, quantity, spec_str),
                )
                conn.commit()
                return True

    @staticmethod
    def list_items(user_id: int) -> List[Dict[str, Any]]:
        with get_conn() as conn:
            with conn.cursor() as cur:
                sql = """
                    SELECT c.*,
                        p.name                     AS product_name,
                        s.price                    AS unit_price,
                        (c.quantity * s.price)     AS total_price,
                        p.cash_only                 -- 新增字段
                    FROM cart c
                    JOIN products p  ON p.id  = c.product_id
                    JOIN product_skus s ON s.id = c.sku_id AND s.product_id = c.product_id
                    WHERE c.user_id = %s
                    ORDER BY c.added_at DESC
                """
                cur.execute(sql, (user_id,))
                rows = cur.fetchall()
                for r in rows:
                    r["user_id_str"] = str(r["user_id"])
                    r["product_id_str"] = str(r["product_id"])
                    r["sku_id_str"] = str(r["sku_id"])
                    r["unit_price"] = float(r["unit_price"])
                    r["total_price"] = float(r["total_price"])
                    r["specifications"] = json.loads(r["specifications"]) if r["specifications"] else None
                return rows

    @staticmethod
    def remove(user_id: int, product_id: int, sku_id: Optional[int] = None) -> bool:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if sku_id is None:
                    sku_id = resolve_sku(cur, product_id, None)
                cur.execute(
                    "DELETE FROM cart WHERE user_id = %s AND product_id = %s AND sku_id = %s",
                    (user_id, product_id, sku_id),
                )
                conn.commit()
                return True
            
    @staticmethod
    def decrease(
        user_id: int,
        product_id: int,
        quantity: int = 1,
        specifications: Optional[Dict[str, Any]] = None,
        sku_id: Optional[int] = None,
    ) -> bool:
        validate_quantity(quantity)

        with get_conn() as conn:
            with conn.cursor() as cur:
                sku_id = resolve_sku(cur, product_id, sku_id)
                cur.execute(
                    "SELECT id, quantity FROM cart "
                    "WHERE user_id = %s AND product_id = %s AND sku_id = %s FOR UPDATE",
                    (user_id, product_id, sku_id),
                )
                row = cur.fetchone()
                if not row:
                    return False

                new_qty = row["quantity"] - quantity
                cart_id = row["id"]

                if new_qty <= 0:
                    cur.execute("DELETE FROM cart WHERE id = %s", (cart_id,))
                else:
                    cur.execute(
                        "UPDATE cart SET quantity = %s WHERE id = %s",
                        (new_qty, cart_id)
                    )
                conn.commit()
                return True


# ----------- 请求模型 -----------
class CartAdd(BaseModel):
    user_id: SafeBigintId
    product_id: SafeBigintId
    quantity: int = Field(default=1, strict=True, ge=1, le=2147483647)
    sku_id: Optional[SafeBigintId] = None
    specifications: Optional[Dict[str, Any]] = None

class CartDecrease(BaseModel):
    user_id: SafeBigintId
    product_id: SafeBigintId
    quantity: int = Field(default=1, strict=True, ge=1, le=2147483647)
    sku_id: Optional[SafeBigintId] = None
    specifications: Optional[Dict[str, Any]] = None


# ----------- 路由 -----------
@router.post("/add", summary="添加商品到购物车")
def cart_add(body: CartAdd, current_user: dict = Depends(get_current_user)):
    if body.user_id != int(current_user['id']):
        raise HTTPException(403, '只能操作本人购物车')
    return {"ok": CartManager.add(body.user_id,
                                  body.product_id,
                                  body.quantity,
                                  body.specifications,
                                  body.sku_id)}


@router.get("/{user_id}", summary="获取购物车列表")
def get_cart(user_id: int, current_user: dict = Depends(get_current_user)):
    if user_id != int(current_user['id']):
        raise HTTPException(403, '只能查看本人购物车')
    # 去掉 spec_map_str 参数，只读库
    return CartManager.list_items(user_id)


@router.delete("/{user_id}/{product_id}", summary="从购物车移除商品")
def cart_remove(user_id: int, product_id: int, sku_id: Optional[int] = Query(default=None, gt=0), current_user: dict = Depends(get_current_user)):
    if user_id != int(current_user['id']):
        raise HTTPException(403, '只能操作本人购物车')
    return {"ok": CartManager.remove(user_id, product_id, sku_id)}

@router.post("/decrease", summary="按规格扣减/移除购物车商品")
def cart_decrease(body: CartDecrease, current_user: dict = Depends(get_current_user)):
    if body.user_id != int(current_user['id']):
        raise HTTPException(403, '只能操作本人购物车')
    ok = CartManager.decrease(
        body.user_id,
        body.product_id,
        body.quantity,
        body.specifications,
        body.sku_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="购物车中未找到该商品/规格")
    return {"ok": True}