import json
import re

import frappe
import requests
from frappe import _


def get_token(company=None):
    """Return the Facturapi secret key for a company.

    Server-side only (not whitelisted): the key must never be sent to the browser.
    Uses the company's row in Company Keys, falling back to the Default Secret Key.
    """
    from frappe.utils.password import get_decrypted_password

    e_invoice_setting = frappe.get_doc("E Invoice Setting", "E Invoice Setting")

    if company:
        for row in e_invoice_setting.company_keys or []:
            if row.company == company:
                return get_decrypted_password(row.doctype, row.name, "secret_key", raise_exception=False)

    token = e_invoice_setting.get_password(fieldname="secret_key", raise_exception=False)
    if not token:
        frappe.throw(
            _("No Facturapi secret key configured for company {0}. Add it in E Invoice Setting.").format(
                company or "-"
            )
        )
    return token


@frappe.whitelist()
def download_einvoice(invoice_name: str, e_invoice_id: str):
    """Stream the stamped CFDI (XML + PDF zip) from Facturapi without exposing the key."""
    si = frappe.get_doc("Sales Invoice", invoice_name)
    si.check_permission("read")

    allowed_ids = {si.e_invoice_id} | {row.id for row in si.get("e_invoice_payments") or []}
    if e_invoice_id not in allowed_ids:
        frappe.throw(_("This e-invoice does not belong to Sales Invoice {0}").format(invoice_name))

    token = get_token(si.company)
    url = f"https://www.facturapi.io/v2/invoices/{e_invoice_id}/zip"
    response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if response.status_code != 200:
        frappe.throw(_("Could not download e-invoice from Facturapi"))

    frappe.local.response.filename = f"{e_invoice_id}.zip"
    frappe.local.response.filecontent = response.content
    frappe.local.response.type = "download"


def generate_einvoice(doc, method):
    e_invoice_setting = frappe.get_doc("E Invoice Setting", "E Invoice Setting")
    if e_invoice_setting.generate_e_invoice:
        token = get_token(doc.company)
        url = "https://www.facturapi.io/v2/invoices"
        header = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        customer = get_customer_details(doc)
        items = get_items(doc)

        data = {
            "customer": customer,
            "items": items,
            "use": "G01",
            "payment_form": "99",
            "payment_method": "PPD",
        }

        # validate advance payment
        validate_advance_payment(data, doc)

        data = json.dumps(data)
        response = requests.post(url, headers=header, data=data)
        if response.status_code == 200:
            response = response.json()
            stamp = response.get("stamp") or {}
            doc.e_invoice_id = response.get("id")
            doc.uuid = response.get("uuid")
            doc.sat_signature = stamp.get("sat_signature")
            doc.signature = stamp.get("signature")
            doc.invoice_status = response.get("status")
            doc.sat_cert_number = stamp.get("sat_cert_number")
            doc.cfdi_version = response.get("cfdi_version")
            doc.verification_url = response.get("verification_url")

            validate_partial_payment(doc, response)
        else:
            response = response.json()
            frappe.throw(_(response.get("message")))


def validate_advance_payment(data, doc):
    if doc.outstanding_amount == 0:
        data.update({"payment_form": "30", "payment_method": "PUE"})


def validate_partial_payment(doc, response):
    if (doc.total_advance and doc.outstanding_amount) > 0:
        update_partial_payment(doc, response)


def get_customer_details(doc):
    customer_name, tax_id, tax_system = frappe.db.get_value(
        "Customer", doc.customer, ["customer_name", "tax_id", "tax_system"]
    )

    address = frappe.db.sql(
        """
            SELECT email_id, pincode
            FROM `tabAddress`
            WHERE name = %s
        """,
        (doc.customer_address,),
        as_dict=1,
    )

    customer = {
        "legal_name": customer_name,
        "email": address[0]["email_id"],
        "tax_id": tax_id,
        "tax_system": tax_system,
        "address": {"zip": address[0]["pincode"]},
    }
    return customer


def get_items(doc):
    items = []

    for item in doc.items:
        # applying taxes
        taxes = []
        if item.item_tax_template:
            item_tax_doc = frappe.get_doc("Item Tax Template", item.item_tax_template)
            for tax in item_tax_doc.taxes:
                taxes.append({"type": tax.maxico_tax_type, "rate": tax.tax_rate / 100})
        elif doc.taxes_and_charges:
            item_tax_doc = frappe.get_doc("Sales Taxes and Charges Template", doc.taxes_and_charges)
            for tax in item_tax_doc.taxes:
                taxes.append({"type": tax.mexico_tax_type, "rate": tax.rate / 100})

        items.append(
            {
                "quantity": item.qty,
                "product": {
                    "description": re.sub("<[^<]+?>", "", _(f"{item.description}")),
                    "product_key": item.product_key,
                    "price": item.rate,
                    "tax_included": False,
                    "taxes": taxes,
                },
            }
        )
    return items


# def cancel_einvoice(invoice_name, e_invoice_id, motive):
@frappe.whitelist()
def cancel_einvoice(
    invoice_name: str,
    e_invoice_id: str,
    motive: str,
):
    si = frappe.get_doc("Sales Invoice", invoice_name)
    si.check_permission("cancel")
    if si.e_invoice_id != e_invoice_id:
        frappe.throw(_("E-invoice ID does not match Sales Invoice {0}").format(invoice_name))

    e_invoice_setting = frappe.get_doc("E Invoice Setting", "E Invoice Setting")
    if e_invoice_setting.cancel_e_invoice:
        token = get_token(si.company)
        url = "https://www.facturapi.io/v2/invoices/" + e_invoice_id + "?motive=" + motive
        header = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        response = requests.delete(url, headers=header)
        if response.status_code == 200:
            doc = frappe.get_doc("Sales Invoice", invoice_name)
            doc.cancel()
            response = response.json()

            # set reason
            if motive == "01":
                reason = "01 - Receipt issued with errors related to."
            elif motive == "02":
                reason = "02 - Receipt issued with unrelated errors."
            elif motive == "03":
                reason = "03 - The operation was not carried out."
            elif motive == "04":
                reason = "04 - Nominative operation related to the global invoice."

            frappe.db.set_value(
                "Sales Invoice",
                invoice_name,
                {"invoice_status": response.get("status"), "motive": reason},
            )
            # frappe.db.commit()
            return "success"
        else:
            response = response.json()
            return "fail"


def get_customer_from_payment(doc):
    customer_name, tax_id, tax_system = frappe.db.get_value(
        "Customer", doc.party, ["customer_name", "tax_id", "tax_system"]
    )

    query = (
        f"select email_id, pincode from `tabAddress` where name in "
        f'(select customer_primary_address from `tabCustomer` where name = "{doc.party}")'
    )
    address = frappe.db.sql(query, as_dict=1)

    if not address:
        frappe.throw(_("Please, update Customer Primary Address in customer Doctype"))

    customer = {
        "legal_name": customer_name,
        "email": address[0]["email_id"],
        "tax_id": tax_id,
        "tax_system": tax_system,
        "address": {"zip": address[0]["pincode"]},
    }
    return customer


def update_payment(doc, method):
    # Only customer receipts that pay stamped Sales Invoices need a payment complement (REP)
    if not doc.references or doc.party_type != "Customer" or doc.payment_type != "Receive":
        return
    if not any(
        r.reference_doctype == "Sales Invoice"
        and frappe.db.get_value("Sales Invoice", r.reference_name, "uuid")
        for r in doc.references
    ):
        return

    customer = get_customer_from_payment(doc)

    # update related documents
    related_documents = []
    for rel_doc in doc.references:
        if rel_doc.reference_doctype == "Sales Invoice":
            uuid = frappe.get_value("Sales Invoice", rel_doc.reference_name, "uuid")
            if not uuid:
                continue
            installments = linked_sales_invoice(rel_doc.reference_name)

            # update taxes
            si_doc = frappe.get_doc("Sales Invoice", rel_doc.reference_name)
            taxes = []
            if si_doc.taxes:
                for tax in si_doc.taxes:
                    taxes.append(
                        {
                            "base": rel_doc.allocated_amount / (1 + (tax.rate / 100)),
                            "type": "IVA",
                            "rate": tax.rate / 100,
                        }
                    )
            else:
                taxes.append(
                    {"base": rel_doc.allocated_amount / (1 + 0.16), "type": "IVA", "rate": 0.16}
                )

            invoice_details = {
                "uuid": uuid,
                "amount": rel_doc.allocated_amount,
                "last_balance": rel_doc.outstanding_amount,
                "installment": installments + 1,
                "taxes": taxes,
            }
            related_documents.append(invoice_details)

    complements = [
        {
            "type": "pago",
            "data": [{"payment_form": doc.payment_form, "related_documents": related_documents}],
        }
    ]

    data = {"type": "P", "customer": customer, "complements": complements}

    token = get_token(doc.company)
    url = "https://www.facturapi.io/v2/invoices"
    header = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    data = json.dumps(data)
    response = requests.post(url, headers=header, data=data)
    if response.status_code == 200:
        response = response.json()

        # updating invoice payments
        for ref in doc.references:
            if ref.reference_doctype == "Sales Invoice":
                si_doc = frappe.get_doc("Sales Invoice", ref.reference_name)
                update_einvoice_payments(si_doc, response)
                si_doc.save()
    else:
        response = response.json()
        frappe.throw(_(response.get("message")))


def linked_sales_invoice(sales_invoice):
    doc = frappe.get_doc("Sales Invoice", sales_invoice)
    installments = len(doc.e_invoice_payments)
    return installments
    # query = (
    #     "select COUNT(name) as installment from `tabPayment Entry Reference` "
    #     "where parenttype='Payment Entry' and reference_doctype='Sales Invoice' "
    #     f"and reference_name='{sales_invoice}'"
    # )
    # count = frappe.db.sql(query, pluck='installment')
    # return count[0]


def update_partial_payment(doc, response):
    customer = get_customer_details(doc)

    uuid = response.get("uuid")

    # update taxes
    taxes = []
    if doc.taxes_and_charges:
        for tax in doc.taxes:
            taxes.append(
                {
                    "base": doc.total_advance / (1 + (tax.rate / 100)),
                    "type": "IVA",
                    "rate": tax.rate / 100,
                }
            )
    else:
        taxes.append({"base": doc.total_advance / (1 + 0.16), "type": "IVA", "rate": 0.16})

    # update related documents
    related_documents = []
    # installments = linked_sales_invoice(doc.name)
    installments = len(doc.e_invoice_payments)
    invoice_details = {
        "uuid": uuid,
        "amount": doc.total_advance,
        "last_balance": doc.grand_total,
        "installment": installments + 1,
        "taxes": taxes,
    }
    related_documents.append(invoice_details)
    complements = [
        {
            "type": "pago",
            "data": [{"payment_form": str(30), "related_documents": related_documents}],
        }
    ]
    data = {"type": "P", "customer": customer, "complements": complements}

    token = get_token(doc.company)
    url = "https://www.facturapi.io/v2/invoices"
    header = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    data = json.dumps(data)
    response = requests.post(url, headers=header, data=data)
    if response.status_code == 200:
        response = response.json()

        # update E-Invoice Payments
        update_einvoice_payments(doc, response)
    else:
        response = response.json()
        frappe.throw(_(response.get("message")))


def update_einvoice_payments(doc, response):
    doc.append(
        "e_invoice_payments",
        dict(
            id=response.get("id"),
            uuid=response.get("uuid"),
            date=response.get("sat_signature"),
            verification_url=response.get("verification_url"),
            status=response.get("status"),
            folio_number=response.get("folio_number"),
            sat_signature=response.get("stamp").get("sat_signature"),
            sat_cert_number=response.get("stamp").get("sat_cert_number"),
            signature=response.get("stamp").get("signature"),
            complement_string=response.get("stamp").get("complement_string"),
        ),
    )
