"""Keep sales tax rows in line with the document's company.

On a multi-company site a new document opens with the default company,
loads that company's default tax template, and keeps those rows when the
user switches Company. Saving then fails with "Account IVA - M does not
belong to company ...". Before validation we replace any tax template,
tax rows or item tax templates that belong to another company with the
ones for the document's company.
"""

import json

import frappe
from frappe import _

TEMPLATE_DOCTYPE = "Sales Taxes and Charges Template"
COPY_FIELDS = (
    "charge_type",
    "row_id",
    "account_head",
    "description",
    "included_in_print_rate",
    "included_in_paid_amount",
    "cost_center",
    "rate",
    "tax_amount",
    "mexico_tax_type",
)


def _account_company(account):
    return frappe.get_cached_value("Account", account, "company") if account else None


def _template_company(template):
    return frappe.get_cached_value(TEMPLATE_DOCTYPE, template, "company") if template else None


def _default_template(company):
    return frappe.db.get_value(
        TEMPLATE_DOCTYPE, {"company": company, "is_default": 1, "disabled": 0}, "name"
    ) or frappe.db.get_value(TEMPLATE_DOCTYPE, {"company": company, "disabled": 0}, "name")


def _fix_document_taxes(doc):
    template_ok = not doc.get("taxes_and_charges") or _template_company(doc.taxes_and_charges) == doc.company
    rows_ok = all(_account_company(t.account_head) == doc.company for t in doc.get("taxes") or [])
    if template_ok and rows_ok:
        return False

    template = doc.taxes_and_charges if template_ok and doc.get("taxes_and_charges") else _default_template(doc.company)
    doc.taxes_and_charges = template
    doc.set("taxes", [])
    if template:
        for row in frappe.get_cached_doc(TEMPLATE_DOCTYPE, template).taxes:
            doc.append("taxes", {f: row.get(f) for f in COPY_FIELDS if row.get(f) is not None})
    return True


def _item_template_for_company(item_code, company):
    for row in frappe.get_cached_doc("Item", item_code).get("taxes") or []:
        if row.item_tax_template and frappe.get_cached_value("Item Tax Template", row.item_tax_template, "company") == company:
            return row.item_tax_template
    return None


def _fix_item_taxes(doc):
    changed = False
    for item in doc.get("items") or []:
        template = item.get("item_tax_template")
        if template and frappe.get_cached_value("Item Tax Template", template, "company") == doc.company:
            continue
        rates = json.loads(item.get("item_tax_rate") or "{}")
        if not template and all(_account_company(acc) == doc.company for acc in rates):
            continue

        new_template = _item_template_for_company(item.item_code, doc.company) if item.item_code else None
        item.item_tax_template = new_template
        item.item_tax_rate = json.dumps(
            {d.tax_type: d.tax_rate for d in frappe.get_cached_doc("Item Tax Template", new_template).taxes}
            if new_template
            else {}
        )
        changed = True
    return changed


def align_taxes_with_company(doc, method=None):
    if not doc.get("company"):
        return
    # drafts being saved, and documents being submitted in one go (e.g. via API)
    if doc.docstatus == 2 or (doc.docstatus == 1 and getattr(doc, "_action", None) != "submit"):
        return
    items_changed = _fix_item_taxes(doc)
    taxes_changed = _fix_document_taxes(doc)
    if taxes_changed or items_changed:
        frappe.msgprint(
            _("Taxes were updated to the templates of company {0}.").format(doc.company),
            indicator="blue",
            alert=True,
        )
