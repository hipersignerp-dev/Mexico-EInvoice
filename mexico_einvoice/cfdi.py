"""CFDI options (Uso CFDI, Método de pago, Forma de pago), XML/PDF attachments
and download helpers for the Facturapi integration."""

import frappe
import requests
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

FACTURAPI = "https://www.facturapi.io/v2/invoices"

CFDI_USES = [
    "G01 - Adquisición de mercancías",
    "G02 - Devoluciones, descuentos o bonificaciones",
    "G03 - Gastos en general",
    "I01 - Construcciones",
    "I02 - Mobiliario y equipo de oficina por inversiones",
    "I03 - Equipo de transporte",
    "I04 - Equipo de cómputo y accesorios",
    "I05 - Dados, troqueles, moldes, matrices y herramental",
    "I06 - Comunicaciones telefónicas",
    "I07 - Comunicaciones satelitales",
    "I08 - Otra maquinaria y equipo",
    "D01 - Honorarios médicos, dentales y gastos hospitalarios",
    "D02 - Gastos médicos por incapacidad o discapacidad",
    "D03 - Gastos funerales",
    "D04 - Donativos",
    "D05 - Intereses reales por créditos hipotecarios (casa habitación)",
    "D06 - Aportaciones voluntarias al SAR",
    "D07 - Primas por seguros de gastos médicos",
    "D08 - Gastos de transportación escolar obligatoria",
    "D09 - Depósitos en cuentas para el ahorro / planes de pensiones",
    "D10 - Pagos por servicios educativos (colegiaturas)",
    "S01 - Sin efectos fiscales",
]
PAYMENT_METHODS = [
    "PUE - Pago en una sola exhibición",
    "PPD - Pago en parcialidades o diferido",
]
PAYMENT_FORMS = {
    "01": "Efectivo",
    "02": "Cheque nominativo",
    "03": "Transferencia electrónica de fondos",
    "04": "Tarjeta de crédito",
    "05": "Monedero electrónico",
    "06": "Dinero electrónico",
    "08": "Vales de despensa",
    "12": "Dación en pago",
    "13": "Pago por subrogación",
    "14": "Pago por consignación",
    "15": "Condonación",
    "17": "Compensación",
    "23": "Novación",
    "24": "Confusión",
    "25": "Remisión de deuda",
    "26": "Prescripción o caducidad",
    "27": "A satisfacción del acreedor",
    "28": "Tarjeta de débito",
    "29": "Tarjeta de servicios",
    "30": "Aplicación de anticipos",
    "31": "Intermediario pagos",
    "99": "Por definir",
}
DEFAULT_USE = "G03 - Gastos en general"
DEFAULT_METHOD = "PPD - Pago en parcialidades o diferido"
FILE_FORMATS = ("xml", "pdf", "zip")


def code(value):
    """'G03 - Gastos en general' -> 'G03'"""
    return (value or "").split(" ")[0].strip()


# --------------------------------------------------------------------------
# Setup (runs after every migrate; safe to re-run)
# --------------------------------------------------------------------------
def setup():
    create_custom_fields(
        {
            "Sales Invoice": [
                dict(
                    fieldname="cfdi_section",
                    label="CFDI México",
                    fieldtype="Section Break",
                    insert_after="amended_from",
                    collapsible=0,
                ),
                dict(
                    fieldname="cfdi_use",
                    label="Uso CFDI",
                    fieldtype="Select",
                    options="\n" + "\n".join(CFDI_USES),
                    insert_after="cfdi_section",
                    allow_on_submit=0,
                    print_hide=1,
                ),
                dict(
                    fieldname="cfdi_column_break",
                    fieldtype="Column Break",
                    insert_after="cfdi_use",
                ),
                dict(
                    fieldname="cfdi_payment_method",
                    label="Método de pago",
                    fieldtype="Select",
                    options="\n" + "\n".join(PAYMENT_METHODS),
                    insert_after="cfdi_column_break",
                    print_hide=1,
                ),
                dict(
                    fieldname="payment_form",
                    label="Forma de pago",
                    fieldtype="Link",
                    options="Payment Form",
                    insert_after="cfdi_payment_method",
                    description="PPD siempre se timbra con 99 - Por definir",
                    print_hide=1,
                ),
            ],
            "Customer": [
                dict(
                    fieldname="cfdi_defaults_section",
                    label="CFDI (valores por defecto)",
                    fieldtype="Section Break",
                    insert_after="tax_system",
                    collapsible=1,
                ),
                dict(
                    fieldname="cfdi_use",
                    label="Uso CFDI",
                    fieldtype="Select",
                    options="\n" + "\n".join(CFDI_USES),
                    insert_after="cfdi_defaults_section",
                ),
                dict(
                    fieldname="cfdi_payment_method",
                    label="Método de pago",
                    fieldtype="Select",
                    options="\n" + "\n".join(PAYMENT_METHODS),
                    insert_after="cfdi_use",
                ),
                dict(
                    fieldname="cfdi_payment_form",
                    label="Forma de pago",
                    fieldtype="Link",
                    options="Payment Form",
                    insert_after="cfdi_payment_method",
                ),
            ],
        },
        update=True,
    )

    for form_id, description in PAYMENT_FORMS.items():
        if not frappe.db.exists("Payment Form", form_id):
            frappe.get_doc(
                {"doctype": "Payment Form", "form_id": form_id, "description": description, "enable": 1}
            ).insert(ignore_permissions=True)


# --------------------------------------------------------------------------
# Defaults and rules
# --------------------------------------------------------------------------
def set_cfdi_defaults(doc, method=None):
    """before_validate on Sales Invoice: fill CFDI options from the Customer."""
    if doc.docstatus == 2 or not doc.get("customer"):
        return
    from mexico_einvoice.utils import get_company_token

    if not get_company_token(doc.company):
        return  # company is not e-invoiced (e.g. US entity)
    cust = frappe.db.get_value(
        "Customer",
        doc.customer,
        ["cfdi_use", "cfdi_payment_method", "cfdi_payment_form"],
        as_dict=True,
    ) or {}

    if not doc.get("cfdi_use"):
        doc.cfdi_use = cust.get("cfdi_use") or DEFAULT_USE
    if not doc.get("cfdi_payment_method"):
        doc.cfdi_payment_method = cust.get("cfdi_payment_method") or DEFAULT_METHOD

    if code(doc.cfdi_payment_method) == "PPD":
        doc.payment_form = "99"
    elif not doc.get("payment_form") or doc.payment_form == "99":
        doc.payment_form = cust.get("cfdi_payment_form") if cust.get("cfdi_payment_form") != "99" else None


def get_cfdi_options(doc):
    """Return (use, payment_form, payment_method) codes for the Facturapi request."""
    use = code(doc.get("cfdi_use")) or code(DEFAULT_USE)
    method = code(doc.get("cfdi_payment_method")) or code(DEFAULT_METHOD)
    form = doc.get("payment_form")

    # Fully paid with advances: SAT requires 30 / PUE
    if (doc.get("total_advance") or 0) > 0 and not doc.get("outstanding_amount"):
        return use, "30", "PUE"

    if method == "PPD":
        form = "99"
    elif not form or form == "99":
        frappe.throw(
            _("Método de pago PUE requiere una Forma de pago distinta de 99 (por ejemplo 03 - Transferencia).")
        )
    return use, form, method


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------
def _fetch(company, e_invoice_id, file_format):
    from mexico_einvoice.utils import get_token

    if file_format not in FILE_FORMATS:
        frappe.throw(_("Invalid format {0}").format(file_format))
    response = requests.get(
        f"{FACTURAPI}/{e_invoice_id}/{file_format}",
        headers={"Authorization": f"Bearer {get_token(company)}"},
        timeout=60,
    )
    if response.status_code != 200:
        frappe.throw(_("Could not download the CFDI {0} from Facturapi").format(file_format.upper()))
    return response.content


def enqueue_attach(doctype, name, company, e_invoice_id, file_label=None):
    frappe.enqueue(
        "mexico_einvoice.cfdi.attach_cfdi_files",
        queue="short",
        enqueue_after_commit=True,
        doctype=doctype,
        name=name,
        company=company,
        e_invoice_id=e_invoice_id,
        file_label=file_label or name,
    )


def attach_cfdi_files(doctype, name, company, e_invoice_id, file_label):
    """Attach the stamped XML and PDF to the document (runs in background)."""
    for file_format in ("xml", "pdf"):
        file_name = f"{file_label}.{file_format}"
        if frappe.db.exists(
            "File", {"attached_to_doctype": doctype, "attached_to_name": name, "file_name": file_name}
        ):
            continue
        try:
            content = _fetch(company, e_invoice_id, file_format)
        except Exception:
            frappe.log_error(title=f"CFDI attach failed: {doctype} {name}")
            continue
        frappe.get_doc(
            {
                "doctype": "File",
                "file_name": file_name,
                "attached_to_doctype": doctype,
                "attached_to_name": name,
                "content": content,
                "is_private": 1,
            }
        ).insert(ignore_permissions=True)
    frappe.db.commit()


@frappe.whitelist()
def attach_now(doctype: str, name: str):
    """Button: (re)attach XML + PDF for an already stamped document."""
    doc = frappe.get_doc(doctype, name)
    doc.check_permission("read")
    if doctype == "Sales Invoice":
        if not doc.e_invoice_id:
            frappe.throw(_("This invoice has no CFDI"))
        attach_cfdi_files(doctype, name, doc.company, doc.e_invoice_id, doc.name)
        for row in doc.get("e_invoice_payments") or []:
            attach_cfdi_files(doctype, name, doc.company, row.id, f"{doc.name}-REP-{row.idx}")
    return "ok"

