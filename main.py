import os
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional, Dict, Any
import requests

from database import create_document, get_documents, db
from schemas import Order, OrderItem

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Simple catalog: single product ----
PRODUCT = {
    "id": "kalrem-b6",
    "title": "Kalerm B6 Home Coffee Machine",
    "short": "Premium automatic coffee machine for home baristas",
    "description": (
        "Make cafe-quality espresso, cappuccino, and latte at home with the Kalerm B6. "
        "One-touch drinks, integrated grinder, and sleek compact design."
    ),
    "price": 299.0,  # KWD
    "currency": "KWD",
    "in_stock": True,
    "images": [
        "https://images.unsplash.com/photo-1517705008128-361805f42e86?q=80&w=1200&auto=format&fit=crop",
        "https://images.unsplash.com/photo-1498804103079-a6351b050096?q=80&w=1200&auto=format&fit=crop"
    ],
    "specs": [
        "One-touch espresso, cappuccino, latte",
        "Integrated conical burr grinder",
        "Adjustable milk frother",
        "Compact, modern design",
    ],
}

@app.get("/")
def read_root():
    return {"message": "Coffee Shop Backend is running"}

@app.get("/api/product")
def get_product():
    return PRODUCT

class CheckoutRequest(BaseModel):
    customer_name: str
    customer_email: Optional[EmailStr] = None
    customer_mobile: Optional[str] = None

class CheckoutResponse(BaseModel):
    order_id: str
    payment_url: Optional[str] = None
    message: str

# ----- MyFatoorah integration helpers -----

def get_myfatoorah_headers() -> Dict[str, str]:
    token = os.getenv("MYFATOORAH_TOKEN")
    if not token:
        raise HTTPException(status_code=500, detail="MyFatoorah token not configured")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def myfatoorah_base_url() -> str:
    return os.getenv("MYFATOORAH_BASE_URL", "https://apitest.myfatoorah.com")


def create_myfatoorah_invoice(order_id: str, order_total: float, customer_name: str, customer_email: Optional[str], customer_mobile: Optional[str]) -> Dict[str, Any]:
    """Creates a payment invoice via MyFatoorah SendPayment API and returns dict with InvoiceId & InvoiceURL.
    Falls back with helpful error if not configured.
    """
    try:
        headers = get_myfatoorah_headers()
    except HTTPException as e:
        # Not configured: return a graceful response
        return {
            "configured": False,
            "error": e.detail,
        }

    base = myfatoorah_base_url()
    callback_url = os.getenv("PAYMENT_CALLBACK_URL", "http://localhost:8000/api/payment/callback")
    error_url = os.getenv("PAYMENT_ERROR_URL", callback_url)

    payload = {
        "CustomerName": customer_name,
        "NotificationOption": "LNK",
        "InvoiceValue": round(order_total, 3),
        "DisplayCurrencyIso": PRODUCT["currency"],
        "CustomerEmail": customer_email or "",
        "CustomerMobile": customer_mobile or "",
        "CallBackUrl": callback_url,
        "ErrorUrl": error_url,
        "Language": "en",
        "CustomerReference": order_id,
        "InvoiceItems": [
            {
                "ItemName": PRODUCT["title"],
                "Quantity": 1,
                "UnitPrice": round(PRODUCT["price"], 3),
            }
        ],
    }

    url = f"{base}/v2/SendPayment"
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=20)
        data = resp.json()
        if resp.status_code == 200 and data.get("IsSuccess"):
            return {
                "configured": True,
                "invoice_id": str(data["Data"]["InvoiceId"]),
                "invoice_url": data["Data"].get("InvoiceURL"),
            }
        else:
            return {
                "configured": True,
                "error": data.get("Message", "MyFatoorah request failed"),
                "details": data,
            }
    except Exception as e:
        return {"configured": True, "error": str(e)}


@app.post("/api/checkout", response_model=CheckoutResponse)
def checkout(req: CheckoutRequest):
    if not PRODUCT["in_stock"]:
        raise HTTPException(status_code=400, detail="Product out of stock")

    # Build order
    item = OrderItem(
        product_title=PRODUCT["title"],
        unit_price=float(PRODUCT["price"]),
        quantity=1,
        currency=PRODUCT["currency"],
    )
    order = Order(
        customer_name=req.customer_name,
        customer_email=req.customer_email,
        customer_mobile=req.customer_mobile,
        items=[item],
        total_amount=item.unit_price * item.quantity,
        currency=PRODUCT["currency"],
        status="pending",
    )

    # Persist order
    order_id = create_document("order", order)

    # Try to create MyFatoorah invoice
    mf = create_myfatoorah_invoice(order_id, order.total_amount, req.customer_name, req.customer_email, req.customer_mobile)

    payment_url = None
    message = "Order created"

    if mf.get("configured") and mf.get("invoice_url"):
        payment_url = mf["invoice_url"]
        # save invoice details
        try:
            db["order"].update_one({"_id": db["order"].find_one({"_id": db["order"].find_one({})["_id"]})}, {"$set": {"invoice_url": payment_url}})
        except Exception:
            pass
        message = "Proceed to payment"
    elif not mf.get("configured"):
        message = "Payment gateway not configured. Contact support."
    else:
        message = f"Payment creation failed: {mf.get('error', 'Unknown error')}"

    return CheckoutResponse(order_id=order_id, payment_url=payment_url, message=message)


@app.post("/api/payment/callback")
async def payment_callback(payload: Dict[str, Any]):
    """Endpoint to receive callbacks from MyFatoorah.
    Updates order status based on PaymentId/InvoiceId if provided.
    """
    try:
        invoice_id = str(payload.get("InvoiceId")) if payload.get("InvoiceId") is not None else None
        payment_id = payload.get("PaymentId")
        transaction_status = payload.get("TransactionStatus") or payload.get("InvoiceStatus")

        update: Dict[str, Any] = {}
        if payment_id:
            update["payment_id"] = payment_id
        if invoice_id:
            update["invoice_id"] = invoice_id
        if transaction_status:
            update["status"] = str(transaction_status).lower()

        if update:
            # Try to update by invoice_id or customer reference if present
            if invoice_id:
                db["order"].update_one({"invoice_id": invoice_id}, {"$set": update})
            # Store raw payload as well for auditing
            db["order_callbacks"].insert_one({"payload": payload})
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/test")
def test_database():
    """Test endpoint to check if database is available and accessible"""
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    
    try:
        # Try to import database module
        from database import db as _db
        
        if _db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = _db.name if hasattr(_db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            
            # Try to list collections to verify connectivity
            try:
                collections = _db.list_collection_names()
                response["collections"] = collections[:10]  # Show first 10 collections
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
            
    except ImportError:
        response["database"] = "❌ Database module not found (run enable-database first)"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"
    
    # Check environment variables
    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
    
    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
