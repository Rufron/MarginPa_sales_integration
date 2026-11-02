"""Pull sales-order data from the Marginpar Fabric lakehouse.

Credentials come from site_config.json (frappe.conf), never from code:
  fabric_server, fabric_database, fabric_tenant_id, fabric_client_id,
  fabric_cert_path, fabric_cert_password
"""
import struct

import frappe
from frappe.utils import getdate
from azure.identity import CertificateCredential
import mssql_python as mssql

TABLE = "dbo.gld_Orders_Curated_DXP"
SCOPE = "https://database.windows.net/.default"
SQL_COPT_SS_ACCESS_TOKEN = 1256
SQL_ATTR_LOGIN_TIMEOUT = 103  # Fabric endpoints wake slowly on a cold connection


def get_connection():
    conf = frappe.conf
    credential = CertificateCredential(
        tenant_id=conf.fabric_tenant_id,
        client_id=conf.fabric_client_id,
        certificate_path=conf.fabric_cert_path,
        password=conf.fabric_cert_password,
    )
    # DDBC wants the token as a [length][UTF-16LE] struct, not a raw string.
    token = credential.get_token(SCOPE).token.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(token)}s", len(token), token)
    return mssql.connect(
        server=conf.fabric_server,
        database=conf.fabric_database,
        attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct, SQL_ATTR_LOGIN_TIMEOUT: 60},
    )


@frappe.whitelist()
def get_orders(from_date=None, to_date=None, limit=500):
    """Orders created within [from_date, to_date] (inclusive), newest first.

    No dates -> most recent `limit` orders. Dates are validated here (trust
    boundary) and passed as SQL parameters, so no injection via the filter.
    """
    limit = int(limit)
    from_date = str(getdate(from_date)) if from_date else None  # raises on bad input
    to_date = str(getdate(to_date)) if to_date else None

    conds, params = [], []
    if from_date:
        conds.append("CAST(ord.createdAt AS DATE) >= ?")
        params.append(from_date)
    if to_date:
        conds.append("CAST(ord.createdAt AS DATE) <= ?")
        params.append(to_date)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""

    # Master tables have column-level security (e.g. tenantID is denied), so we
    # list named columns explicitly instead of SELECT *.
    # shortcut: TOP caps the result at `limit` rows; raise it if a range gets denser.
    sql = f"""
        SELECT TOP {limit}
            ord.orderID,
            ord.orderNr, ord.reference, ord.orderStatus, ord.orderType,
            ord.paymentMethod, ord.currencyCode, ord.deliveryAt, ord.createdAt,
            cust.organisationName  AS customerName,
            agent.organisationName AS agentName,
            sc.salesChannelName    AS salesChannel,
            usr.name               AS createdByName
        FROM {TABLE} ord
        LEFT JOIN dbo.gld_Organisations_Master_DXP cust  ON cust.organisationID  = ord.organisationID
        LEFT JOIN dbo.gld_Organisations_Master_DXP agent ON agent.organisationID = ord.agentOrganisationID
        LEFT JOIN dbo.gld_salesChannel_Master_DXP  sc    ON sc.salesChannelID    = ord.salesChannelID
        LEFT JOIN dbo.gld_Users_Master_DXP         usr   ON usr.userID           = ord.createdByUserID
        {where}
        ORDER BY ord.createdAt DESC
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params) if params else cur.execute(sql)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


@frappe.whitelist()
def get_order_lines(order_id):
    """Line items for one order, with product / packing / farm names resolved."""
    if not order_id:
        frappe.throw("order_id is required")
    sql = """
        SELECT
            p.productName,
            g.gradeCode               AS grade,
            pk.PackingType            AS packing,
            ln.stems,
            ln.productPackingQuantity AS packages,
            ln.stemSalesPrice         AS stemPrice,
            ROUND(ln.stems * ln.stemSalesPrice, 2) AS lineValue,
            f.farmName,
            ln.allocationStatus
        FROM dbo.gld_OrderLines_Curated_DXP ln
        LEFT JOIN dbo.gld_Grade_Master_DXP         g  ON g.gradeID          = ln.gradingID
        LEFT JOIN dbo.gld_Product_Master_DXP       p  ON p.productId        = g.productID
        LEFT JOIN dbo.gld_ProductPacking_Master_DXP pk ON pk.productPackingID = ln.productPackingID
        LEFT JOIN dbo.gld_Farm_Master_DXP          f  ON f.farmID           = ln.farmID
        WHERE ln.orderID = ? AND ln.deletedAt IS NULL   -- deleted lines overstate qty/value
        ORDER BY p.productName
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, [order_id])
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
