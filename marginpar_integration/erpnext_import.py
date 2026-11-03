"""Import DXP orders from Fabric into ERPNext Sales Orders (as Draft).

Masters are created on demand and deduped by DXP id (custom fields), so the
import is idempotent. Canceled orders and deleted lines are skipped. Product is
reached via Grade.productID because the SP is denied SELECT on OrderLines.productID.
"""
import frappe
from frappe.utils import getdate
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from marginpar_integration.fabric import get_connection

UOM = "Stem"
ITEM_GROUP = "DXP Products"
PRICE_LIST = "Standard Selling"


def _setup():
    """One-time, idempotent: dedup custom fields, UOM, item group."""
    create_custom_fields({
        "Customer": [{"fieldname": "dxp_organisation_id", "label": "DXP Organisation ID", "fieldtype": "Data", "read_only": 1, "unique": 1}],
        "Item": [{"fieldname": "dxp_product_id", "label": "DXP Product ID", "fieldtype": "Data", "read_only": 1, "unique": 1}],
        "Sales Order": [{"fieldname": "dxp_order_nr", "label": "DXP Order Nr", "fieldtype": "Data", "read_only": 1, "unique": 1}],
    })
    if not frappe.db.exists("UOM", UOM):
        frappe.get_doc({"doctype": "UOM", "uom_name": UOM}).insert(ignore_permissions=True)
    if not frappe.db.exists("Item Group", ITEM_GROUP):
        frappe.get_doc({"doctype": "Item Group", "item_group_name": ITEM_GROUP,
                        "parent_item_group": "All Item Groups"}).insert(ignore_permissions=True)


def _ensure_customer(org_id, name, org_type, currency=None):
    existing = frappe.db.get_value("Customer", {"dxp_organisation_id": org_id})
    if existing:
        return existing
    # Adopt a same-name customer that has no DXP id yet (manual/legacy) instead of
    # creating a duplicate; re-imports then converge on one record.
    if name:
        match = frappe.db.get_value("Customer", {"customer_name": name},
                                    ["name", "dxp_organisation_id", "default_currency"], as_dict=True)
        if match and not match.dxp_organisation_id:
            frappe.db.set_value("Customer", match.name, "dxp_organisation_id", org_id)
            if currency and not match.default_currency:
                frappe.db.set_value("Customer", match.name, "default_currency", currency)
            return match.name
    doc = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": name or org_id,
        "customer_type": "Individual" if (org_type or "").lower() == "person" else "Company",
        "dxp_organisation_id": org_id,
    })
    if currency:
        doc.default_currency = currency  # Billing Currency
    return doc.insert(ignore_permissions=True).name


def _ensure_item(product_id, name):
    existing = frappe.db.get_value("Item", {"dxp_product_id": product_id})
    if existing:
        return existing
    return frappe.get_doc({
        "doctype": "Item",
        "item_code": (name or product_id)[:140],
        "item_name": name or product_id,
        "item_group": ITEM_GROUP,
        "stock_uom": UOM,
        "is_stock_item": 0,  # external orders; avoid stock/warehouse requirements
        "dxp_product_id": product_id,
    }).insert(ignore_permissions=True).name


def _fetch_orders(limit):
    sql = f"""
        SELECT TOP {int(limit)}
            ord.orderID, ord.orderNr, ord.organisationID,
            cust.organisationName, cust.organisationType,
            ord.currencyCode, ord.createdAt, ord.deliveryAt
        FROM dbo.gld_Orders_Curated_DXP ord
        LEFT JOIN dbo.gld_Organisations_Master_DXP cust ON cust.organisationID = ord.organisationID
        WHERE ord.orderStatus <> 'canceled'
        ORDER BY ord.createdAt DESC
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fetch_lines(conn, order_id):
    # Product via Grade.productID (OrderLines.productID is permission-denied).
    sql = """
        SELECT p.productId, p.productName, ln.stems, ln.stemSalesPrice
        FROM dbo.gld_OrderLines_Curated_DXP ln
        LEFT JOIN dbo.gld_Grade_Master_DXP   g ON g.gradeID   = ln.gradingID
        LEFT JOIN dbo.gld_Product_Master_DXP p ON p.productId = g.productID
        WHERE ln.orderID = ? AND ln.deletedAt IS NULL
    """
    cur = conn.cursor()
    cur.execute(sql, [order_id])
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@frappe.whitelist()
def import_sample(limit=5):
    """Import up to `limit` most-recent non-canceled orders as Draft Sales Orders."""
    _setup()
    company = frappe.defaults.get_global_default("company") or frappe.get_all("Company", pluck="name")[0]
    results = {"created": [], "exists": [], "skipped": []}

    target = int(limit)
    # Many recent orders are auction orders with no customer; scan extra to fill the sample.
    orders = _fetch_orders(target * 10)
    with get_connection() as conn:
        for o in orders:
            if len(results["created"]) >= target:
                break
            try:
                if frappe.db.exists("Sales Order", {"dxp_order_nr": o["orderNr"]}):
                    results["exists"].append(o["orderNr"])
                    continue
                if not o["organisationID"]:
                    results["skipped"].append(f"{o['orderNr']}: no customer organisation (auction order)")
                    continue
                lines = [l for l in _fetch_lines(conn, o["orderID"]) if l["productId"] and l["stems"]]
                if not lines:
                    results["skipped"].append(f"{o['orderNr']}: no usable lines")
                    continue

                customer = _ensure_customer(o["organisationID"], o["organisationName"], o["organisationType"])
                delivery = getdate(o["deliveryAt"]) if o["deliveryAt"] else getdate(o["createdAt"])

                so = frappe.new_doc("Sales Order")
                so.company = company
                so.customer = customer
                so.transaction_date = getdate(o["createdAt"])
                so.delivery_date = delivery
                so.currency = o["currencyCode"] or frappe.get_cached_value("Company", company, "default_currency")
                so.selling_price_list = PRICE_LIST
                so.dxp_order_nr = o["orderNr"]
                so.ignore_pricing_rule = 1
                for l in lines:
                    so.append("items", {
                        "item_code": _ensure_item(l["productId"], l["productName"]),
                        "qty": l["stems"],
                        "uom": UOM,
                        "rate": l["stemSalesPrice"] or 0,
                        "delivery_date": delivery,
                    })
                so.insert(ignore_permissions=True)
                frappe.db.commit()  # per-order: a later failure can't undo earlier orders
                results["created"].append(f"{o['orderNr']} -> {so.name} ({len(lines)} lines)")
            except Exception as e:
                frappe.db.rollback()
                results["skipped"].append(f"{o['orderNr']}: {e}")

    return results


def _fetch_customers(limit):
    # Billing currency isn't on Organisations (restricted); derive it from the
    # org's orders. shortcut: MAX picks one currency if an org ever mixed EUR/USD.
    sql = f"""
        SELECT TOP {int(limit)}
            c.organisationID, c.organisationName, c.organisationType,
            MAX(o.currencyCode) AS currencyCode
        FROM dbo.gld_Organisations_Master_DXP c
        JOIN dbo.gld_Orders_Curated_DXP o ON o.organisationID = c.organisationID
        WHERE o.currencyCode IS NOT NULL AND o.currencyCode <> ''
        GROUP BY c.organisationID, c.organisationName, c.organisationType
        ORDER BY c.organisationName
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@frappe.whitelist()
def import_customers(limit=1):
    """Import DXP organisations (that have orders) as Customers with Billing Currency
    set from their orders. Backfills billing currency on existing customers if blank."""
    _setup()
    results = {"created": [], "updated": [], "exists": [], "skipped": []}
    for r in _fetch_customers(limit):
        try:
            existing = frappe.db.get_value("Customer", {"dxp_organisation_id": r["organisationID"]})
            if existing:
                if r["currencyCode"] and not frappe.db.get_value("Customer", existing, "default_currency"):
                    frappe.db.set_value("Customer", existing, "default_currency", r["currencyCode"])
                    frappe.db.commit()
                    results["updated"].append(f"{existing} -> {r['currencyCode']}")
                else:
                    results["exists"].append(existing)
                continue
            name = _ensure_customer(r["organisationID"], r["organisationName"], r["organisationType"], r["currencyCode"])
            frappe.db.commit()
            results["created"].append(f"{r['organisationName']} -> {name} ({r['currencyCode']})")
        except Exception as e:
            frappe.db.rollback()
            results["skipped"].append(f"{r.get('organisationName')}: {e}")
    return results
